#!/usr/bin/env python3
"""SBI証券 指値注文フォーム + 約定監視・通知。

指定した銘柄・株数・価格で注文を出し、約定したら macOS 通知で知らせる。
「いつ・いくらで買うか」の判断は人間が行う前提で、発注と約定確認だけを自動化する。

Usage: python3 web.py
初回は config/.env が必要（config/.env.example を参照）。
"""
import html
import queue
import random
import sys
import threading
import time
import urllib.parse
from datetime import datetime, time as dt_time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import mention_listener
import notify
import order_store
import slack_client
from config import ENV
from sbi_client import HumanInterventionRequired, SBIClient, format_order_book

PORT = 8381
_BOT_USER_ID = None  # main()で解決。メンション本文から自分宛の<@ID>を取り除くのに使う。
POLL_INTERVAL_SEC = 60
# 株価取得の間隔は固定にせず、機械的なアクセスパターンにならないよう毎回この範囲でランダムに決める。
PRICE_INTERVAL_MIN_SEC = int(ENV.get("SBI_PRICE_INTERVAL_MIN_SEC", "480"))  # 8分
PRICE_INTERVAL_MAX_SEC = int(ENV.get("SBI_PRICE_INTERVAL_MAX_SEC", "720"))  # 12分
WATCH_TICKERS = [t.strip() for t in ENV.get("SBI_WATCH_TICKERS", "").split(",") if t.strip()]
SLACK_CHANNEL = ENV.get("SLACK_CHANNEL", "")
SLACK_MENTION_USER = ENV.get("SLACK_MENTION_USER", "")
# メンション経由の注文への安全弁: 見積金額がこれを超えたら発注せず拒否する。
MAX_ORDER_VALUE_YEN = float(ENV.get("SBI_MAX_ORDER_VALUE_YEN", "500000"))

# 自動リビッド: 有効時（SBI_AUTO_REBID_TICKER設定時）、ランダム間隔で未約定注文を
# 全取消し、最良買気配と同値の買い指値を入れ直す。間隔・数量を固定にしないのは
# 株価取得と同じく機械的なパターンを避けるため。
AUTO_REBID_TICKER = ENV.get("SBI_AUTO_REBID_TICKER", "")
AUTO_REBID_INTERVAL_MIN_SEC = int(ENV.get("SBI_AUTO_REBID_INTERVAL_MIN_SEC", "1200"))  # 20分
AUTO_REBID_INTERVAL_MAX_SEC = int(ENV.get("SBI_AUTO_REBID_INTERVAL_MAX_SEC", "1800"))  # 30分
AUTO_REBID_QTY_MIN = int(ENV.get("SBI_AUTO_REBID_QTY_MIN", "300"))
AUTO_REBID_QTY_MAX = int(ENV.get("SBI_AUTO_REBID_QTY_MAX", "600"))
AUTO_REBID_QTY_STEP = int(ENV.get("SBI_AUTO_REBID_QTY_STEP", "100"))  # 3930の売買単位

JST = timezone(timedelta(hours=9))


def _is_market_hours(now=None):
    """板が動いている時間帯（前場8:00-11:30, 後場12:05-15:30, 平日のみ）かどうか。
    9:00/12:30の寄り付きより前から気配（板寄せ）が動くため、その分開始を
    前倒ししている（ユーザー確認済みの実際の値）。祝日カレンダーまでは見ておらず、
    土日＋時間帯の簡易判定にとどめている。
    """
    now = now or datetime.now(JST)
    if now.weekday() >= 5:  # 5=土, 6=日
        return False
    t = now.time()
    return (dt_time(8, 0) <= t <= dt_time(11, 30)) or (dt_time(12, 5) <= t <= dt_time(15, 30))


def _now():
    return datetime.now(timezone.utc).isoformat()

# Playwrightの同期APIはgreenlet実装のため、生成したスレッド以外から触ると
# "Cannot switch to a different thread" で壊れる。そのため SBIClient（＝ブラウザ
# とのやり取り全部）は _sbi_loop という単一のスレッドの中だけで生成・使用し、
# 他のスレッド（HTTPサーバ）は queue.Queue 経由でしか関与しない。
_work_q = queue.Queue()
_login_alert_sent = False
# Slackメンションから来た注文は、結果をそのスレッドにも返信する。
# order_id -> (channel, thread_ts)。web UI経由の注文はここに登録されないので、
# _reply_mention_result は何もせず無視するだけになる。
_mention_reply_targets = {}


def _alert_login_needed(reason):
    """ログインが必要（想定外の画面）になったときに一度だけ知らせる。連投は避ける。"""
    global _login_alert_sent
    notify.notify("SBI: ログインが必要です", reason)
    if _login_alert_sent:
        return
    _login_alert_sent = True
    if SLACK_CHANNEL and SLACK_MENTION_USER:
        try:
            slack_client.post(
                SLACK_CHANNEL,
                f"<@{SLACK_MENTION_USER}> SBIのログインが必要です。ブラウザ画面を確認してください。\n{reason}",
            )
        except Exception as e:
            print(f"[slack] ログイン依頼の投稿に失敗しました: {e}", file=sys.stderr)


def _clear_login_alert():
    global _login_alert_sent
    _login_alert_sent = False


def _announce_fill(order):
    text = (
        f"約定しました: {order['ticker']} {order['side']} {order['qty']}株"
        f" @ {order['price']}円"
    )
    notify.notify("約定しました", text)
    if SLACK_CHANNEL:
        try:
            slack_client.post(SLACK_CHANNEL, text)
        except Exception as e:
            print(f"[slack] 約定通知の投稿に失敗しました: {e}", file=sys.stderr)


def _reply_mention_result(order_id, text):
    """このorder_idがメンション経由なら、そのスレッドにも結果を返信する。"""
    target = _mention_reply_targets.pop(order_id, None)
    if not target:
        return
    channel, thread_ts = target
    try:
        slack_client.post(channel, text, thread_ts=thread_ts)
    except Exception as e:
        print(f"[mention] 結果の返信に失敗しました: {e}", file=sys.stderr)


def _process_order(client, order_id):
    try:
        client.ensure_logged_in()
        _clear_login_alert()
        order = order_store.get_order(order_id)
        sbi_order_id = client.place_order(
            order["ticker"], order["side"], order["qty"], order["price"])
        order_store.update_order(
            order_id, status="submitted", sbi_order_id=sbi_order_id,
            submitted_at=_now())
        _reply_mention_result(
            order_id,
            f"発注しました: 注文番号{sbi_order_id} {order['ticker']} {order['side']}"
            f" {order['qty']}株 @ {order['price']}円",
        )
    except HumanInterventionRequired as e:
        order_store.update_order(order_id, status="error", error_message=str(e))
        _alert_login_needed(str(e))
        _reply_mention_result(order_id, f"発注できませんでした（要対応）: {e}")
    except Exception as e:
        order_store.update_order(order_id, status="error", error_message=str(e))
        notify.notify("SBI注文: 発注に失敗しました", f"id={order_id}: {e}")
        _reply_mention_result(order_id, f"発注に失敗しました: {e}")


def _on_mention_command(parsed, channel, thread_ts, reply):
    """Slackメンションの受信（HTTPリクエストのスレッド）から呼ばれる。
    Playwrightには一切触らず、order_store と _work_q だけを操作する。
    """
    value = parsed["qty"] * parsed["price"]
    if value > MAX_ORDER_VALUE_YEN:
        reply(
            f"見積金額が上限（{MAX_ORDER_VALUE_YEN:,.0f}円）を超えるため発注しません: "
            f"{parsed['ticker']} {parsed['qty']}株 @ {parsed['price']}円 "
            f"(見積 {value:,.0f}円)。上限は SBI_MAX_ORDER_VALUE_YEN で変更できます。"
        )
        return
    order_id = order_store.create_order(
        parsed["ticker"], parsed["side"], parsed["qty"], parsed["price"])
    _mention_reply_targets[order_id] = (channel, thread_ts)
    reply(
        f"受け付けました: {parsed['ticker']} {parsed['side']} {parsed['qty']}株 "
        f"@ {parsed['price']}円 (見積 {value:,.0f}円)。処理します…"
    )
    _work_q.put(("order", order_id))


def _on_clear_all(channel, thread_ts, reply):
    """`clear all` メンションを受けたときの入口（HTTPリクエストのスレッドから
    呼ばれる）。Playwrightには一切触らず、キューに積むだけ。
    """
    reply("未約定の注文を全て取消します…")
    _work_q.put(("clear_all", channel, thread_ts))


def _on_book_request(ticker, channel, thread_ts, reply):
    """`book`/`book 3930` メンションを受けたときの入口（HTTPリクエストのスレッド
    から呼ばれる）。Playwrightには一切触らず、キューに積むだけ。
    """
    reply("板情報を取得します…")
    _work_q.put(("book", ticker, channel, thread_ts))


def _process_book_request(client, ticker, channel, thread_ts):
    tickers = [ticker] if ticker else WATCH_TICKERS
    if not tickers:
        slack_client.post(
            channel,
            "対象銘柄が指定されておらず、SBI_WATCH_TICKERSも未設定です。"
            "`book 3930` のように銘柄コードを指定してください。",
            thread_ts=thread_ts,
        )
        return
    try:
        client.ensure_logged_in()
        _clear_login_alert()
    except HumanInterventionRequired as e:
        _alert_login_needed(str(e))
        slack_client.post(channel, f"板情報を取得できませんでした（要対応）: {e}", thread_ts=thread_ts)
        return
    for t in tickers:
        try:
            book = client.get_order_book(t)
            slack_client.post(channel, format_order_book(t, book), thread_ts=thread_ts)
        except HumanInterventionRequired as e:
            _alert_login_needed(str(e))
            slack_client.post(
                channel, f"{t}: 板情報の取得に失敗しました（要対応）: {e}", thread_ts=thread_ts)
            return  # ログインが必要なら残りの銘柄も今回はスキップ
        except Exception as e:
            slack_client.post(channel, f"{t}: 板情報の取得に失敗しました: {e}", thread_ts=thread_ts)


def _process_clear_all(client, channel, thread_ts):
    try:
        client.ensure_logged_in()
        _clear_login_alert()
        order_ids = client.list_pending_order_ids()
        if not order_ids:
            slack_client.post(channel, "未約定の注文はありませんでした。", thread_ts=thread_ts)
            return
        lines = []
        for sbi_order_id in order_ids:
            try:
                client.cancel_order(sbi_order_id)
                order_store.update_order_by_sbi_id(sbi_order_id, status="cancelled")
                lines.append(f"注文{sbi_order_id}: 取消しました")
            except HumanInterventionRequired as e:
                _alert_login_needed(str(e))
                lines.append(f"注文{sbi_order_id}: 取消できませんでした（要対応）")
            except Exception as e:
                lines.append(f"注文{sbi_order_id}: 取消に失敗しました ({e})")
        slack_client.post(channel, "全注文取消:\n" + "\n".join(lines), thread_ts=thread_ts)
    except HumanInterventionRequired as e:
        _alert_login_needed(str(e))
        slack_client.post(
            channel, f"取消処理を実行できませんでした（要対応）: {e}", thread_ts=thread_ts)
    except Exception as e:
        slack_client.post(channel, f"全注文取消でエラーが発生しました: {e}", thread_ts=thread_ts)


def _poll_orders(client):
    for order in order_store.pending_watch_orders():
        if not order.get("sbi_order_id"):
            continue
        try:
            client.ensure_logged_in()
            _clear_login_alert()
            status = client.check_order_status(order["sbi_order_id"])
            if status == "filled":
                order_store.update_order(
                    order["id"], status="filled", filled_at=_now(), notified_at=_now())
                _announce_fill(order)
            elif status == "cancelled":
                order_store.update_order(order["id"], status="cancelled")
            elif status == "unknown":
                # 注文照会一覧に見つからない = 何らかの理由（このアプリの外で
                # 手動取消した等）で追跡対象から外れている。「submitted」の
                # まま残すと毎回チェックし続けてしまう（過度な遷移の原因になった
                # 実績あり、docs/adr/0011）ため、追跡を打ち切る。
                order_store.update_order(order["id"], status="unknown")
        except HumanInterventionRequired as e:
            _alert_login_needed(str(e))
            return  # ログインが必要なら残りの注文チェックも今回はスキップ
        except Exception as e:
            print(f"[poller] order {order['id']} の確認に失敗: {e}", file=sys.stderr)


def _poll_price(client):
    try:
        client.ensure_logged_in()
        _clear_login_alert()
        for ticker in WATCH_TICKERS:
            book = client.get_order_book(ticker)
            slack_client.post(SLACK_CHANNEL, format_order_book(ticker, book))
    except HumanInterventionRequired as e:
        _alert_login_needed(str(e))
    except Exception as e:
        print(f"[price] 板情報取得に失敗しました: {e}", file=sys.stderr)


def _auto_rebid(client):
    """未約定の注文を全取消し、最良買気配と同値の買い指値を入れ直す。

    数量は AUTO_REBID_QTY_MIN〜MAX からランダム（STEP刻み）。見積が
    MAX_ORDER_VALUE_YEN を超える場合は超えない数量まで下げ、最小数量でも
    超えるなら今回は見送る。結果は毎回 SLACK_MENTION_USER 宛のメンションで報告する。
    発注はorder_storeにも記録するので、約定は既存の60秒ポーラーが検知・通知する。
    """
    mention = f"<@{SLACK_MENTION_USER}> " if SLACK_MENTION_USER else ""

    def report(text):
        notify.notify("SBI自動リビッド", text)
        if SLACK_CHANNEL:
            try:
                slack_client.post(SLACK_CHANNEL, f"{mention}自動リビッド: {text}")
            except Exception as e:
                print(f"[rebid] Slack投稿に失敗しました: {e}", file=sys.stderr)

    try:
        client.ensure_logged_in()
        _clear_login_alert()
        cancelled = []
        for sbi_order_id in client.list_pending_order_ids():
            client.cancel_order(sbi_order_id)
            order_store.update_order_by_sbi_id(sbi_order_id, status="cancelled")
            cancelled.append(sbi_order_id)
        cancelled_note = f"注文{', '.join(cancelled)}を取消 → " if cancelled else ""

        # 板は価格の降順なので、価格が数値で買数量がある最初の行が最良買気配。
        # OVER/UNDER/成行の行は指値に使えないので飛ばす。
        bid = None
        for row in client.get_order_book(AUTO_REBID_TICKER)["rows"]:
            if not row["bid_qty"]:
                continue
            try:
                bid = float(row["price"])
                break
            except ValueError:
                continue
        if bid is None:
            report(f"{cancelled_note}最良買気配が取れなかったため、今回の発注は見送りました")
            return

        qty = random.randrange(
            AUTO_REBID_QTY_MIN, AUTO_REBID_QTY_MAX + 1, AUTO_REBID_QTY_STEP)
        # 安全弁（SBI_MAX_ORDER_VALUE_YEN）は自動発注にも適用する。
        # 上限内に収まるまで数量を下げるが、下限（QTY_MIN）を割ってまでは発注しない。
        while qty > AUTO_REBID_QTY_MIN and qty * bid > MAX_ORDER_VALUE_YEN:
            qty -= AUTO_REBID_QTY_STEP
        if qty * bid > MAX_ORDER_VALUE_YEN:
            report(
                f"{cancelled_note}最小数量({AUTO_REBID_QTY_MIN}株)でも見積が"
                f"上限({MAX_ORDER_VALUE_YEN:,.0f}円)を超えるため見送りました"
                f"（最良買気配 {bid}円）")
            return

        order_id = order_store.create_order(AUTO_REBID_TICKER, "buy", qty, bid)
        try:
            sbi_order_id = client.place_order(AUTO_REBID_TICKER, "buy", qty, bid)
        except Exception:
            order_store.update_order(order_id, status="error")
            raise
        order_store.update_order(
            order_id, status="submitted", sbi_order_id=sbi_order_id,
            submitted_at=_now())
        report(
            f"{cancelled_note}{AUTO_REBID_TICKER} 買 {qty}株 @ {bid}円"
            f" (注文番号{sbi_order_id})")
    except HumanInterventionRequired as e:
        _alert_login_needed(str(e))
        report(f"実行できませんでした（要対応）: {e}")
    except Exception as e:
        report(f"実行に失敗しました: {e}")


def _connect_with_retry():
    """ブラウザへの接続を失敗しても諦めずリトライする。

    以前は SBIClient().start() の失敗（例: CDP接続タイムアウト）が _sbi_loop
    スレッド全体を無言で落としていた。その場合HTTPサーバ自体は動き続ける
    ため、メンションは「受け付けました」と返信されるのに実際には何も処理
    されない（Slack通知も一切来ない）という気づきにくい壊れ方をしていた。
    それを防ぐため、接続できるまでここでリトライし続ける。
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return SBIClient().start()
        except HumanInterventionRequired as e:
            if attempt == 1 or attempt % 5 == 0:
                _alert_login_needed(str(e))
            print(
                f"[startup] ブラウザ接続に失敗（{attempt}回目）。10秒後に再試行します: {e}",
                file=sys.stderr,
            )
        except Exception as e:
            print(
                f"[startup] ブラウザ接続で想定外のエラー（{attempt}回目）。"
                f"10秒後に再試行します: {e}",
                file=sys.stderr,
            )
        time.sleep(10)


def _sbi_loop():
    """SBI/ブラウザに触る唯一のスレッド。発注キューの処理・約定確認・株価投稿を
    このスレッドの中で順番に行う（Playwrightの同期APIはスレッドを跨げないため）。
    このスレッドが例外で落ちると何も処理されなくなる（HTTPサーバ自体は動き
    続けるため気づきにくい）ため、ループ内は必ず捕捉して継続する。
    """
    watch_price = bool(WATCH_TICKERS and SLACK_CHANNEL)
    if WATCH_TICKERS and not SLACK_CHANNEL:
        print("[price] SLACK_CHANNEL が未設定のため株価監視は行いません", file=sys.stderr)

    client = _connect_with_retry()
    try:
        client.ensure_logged_in()
        _clear_login_alert()
    except HumanInterventionRequired as e:
        _alert_login_needed(str(e))
    except Exception as e:
        print(f"[startup] 起動時ログインに失敗しました: {e}", file=sys.stderr)

    next_order_poll = time.monotonic() + POLL_INTERVAL_SEC
    next_price_poll = (
        time.monotonic() + random.uniform(PRICE_INTERVAL_MIN_SEC, PRICE_INTERVAL_MAX_SEC)
        if watch_price else None
    )
    # 自動リビッドの初回は起動直後（15秒後）。以降はランダム間隔。
    next_rebid = time.monotonic() + 15 if AUTO_REBID_TICKER else None

    while True:
        try:
            item = _work_q.get(timeout=1)
            if item[0] == "order":
                _process_order(client, item[1])
            elif item[0] == "clear_all":
                _process_clear_all(client, item[1], item[2])
            elif item[0] == "book":
                _process_book_request(client, item[1], item[2], item[3])
        except queue.Empty:
            pass
        except Exception as e:
            print(f"[sbi_loop] キュー処理で想定外のエラー: {e}", file=sys.stderr)

        try:
            now = time.monotonic()
            if now >= next_order_poll:
                _poll_orders(client)
                next_order_poll = now + POLL_INTERVAL_SEC
            if watch_price and now >= next_price_poll:
                if _is_market_hours():
                    _poll_price(client)
                next_price_poll = now + random.uniform(
                    PRICE_INTERVAL_MIN_SEC, PRICE_INTERVAL_MAX_SEC)
            if next_rebid is not None and now >= next_rebid:
                if _is_market_hours():
                    _auto_rebid(client)
                next_rebid = now + random.uniform(
                    AUTO_REBID_INTERVAL_MIN_SEC, AUTO_REBID_INTERVAL_MAX_SEC)
        except Exception as e:
            print(f"[sbi_loop] ポーリングで想定外のエラー: {e}", file=sys.stderr)


# --- HTML ---

def _render(orders, message=""):
    rows = "".join(
        f"<tr><td>{o['id']}</td><td>{html.escape(o['ticker'])}</td>"
        f"<td>{html.escape(o['side'])}</td><td>{o['qty']}</td><td>{o['price']}</td>"
        f"<td>{html.escape(o['status'])}</td>"
        f"<td>{html.escape(o.get('error_message') or '')}</td></tr>"
        for o in orders
    )
    msg_html = f'<p class="msg">{html.escape(message)}</p>' if message else ""
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<title>SBI 発注</title>
<style>
body{{font-family:-apple-system,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem}}
table{{border-collapse:collapse;width:100%;margin-top:1rem}}
td,th{{border:1px solid #ddd;padding:.4rem .6rem;text-align:left;font-size:.9rem}}
form{{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}}
input,select{{padding:.4rem}}
.msg{{color:#c00}}
</style></head><body>
<h1>SBI 発注</h1>
{msg_html}
<form method="post" action="/orders">
  <input name="ticker" placeholder="銘柄コード" required>
  <select name="side"><option value="buy">買</option><option value="sell">売</option></select>
  <input name="qty" type="number" placeholder="株数" required>
  <input name="price" type="number" step="0.1" placeholder="指値価格" required>
  <button type="submit">発注</button>
</form>
<table>
<tr><th>ID</th><th>銘柄</th><th>売買</th><th>株数</th><th>価格</th><th>状態</th><th>エラー</th></tr>
{rows}
</table>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/":
            self.send_response(404)
            self.end_headers()
            return
        self._respond(200, _render(order_store.list_orders()))

    def do_POST(self):
        if self.path == "/slack/events":
            self._handle_slack_event()
            return
        if self.path != "/orders":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        params = {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}
        try:
            ticker = params["ticker"].strip()
            side = params["side"]
            qty = int(params["qty"])
            price = float(params["price"])
            assert ticker and side in ("buy", "sell") and qty > 0 and price > 0
        except (KeyError, ValueError, AssertionError):
            self._respond(400, _render(order_store.list_orders(), "入力値が不正です"))
            return
        order_id = order_store.create_order(ticker, side, qty, price)
        _work_q.put(("order", order_id))
        self._respond(200, _render(order_store.list_orders(), "発注をキューに入れました"))

    def _handle_slack_event(self):
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)
        print(f"[slack-event] received {length} bytes: {raw_body[:300]!r}", file=sys.stderr)
        status, body = mention_listener.handle_event(
            self.headers, raw_body, _BOT_USER_ID, _on_mention_command, _on_clear_all,
            _on_book_request)
        print(f"[slack-event] handled -> status={status} body={body[:200]!r}", file=sys.stderr)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def _respond(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *a):
        pass


def main():
    global _BOT_USER_ID
    # ブラウザは起動時に立ち上げてログインし、以後常時起動しておく（都度開かない）。
    threading.Thread(target=_sbi_loop, daemon=True).start()

    if mention_listener.enabled():
        try:
            _BOT_USER_ID = slack_client.bot_user_id()
            print(
                "[mention] メンション発注が有効です。このポート宛にトンネルが向いていれば、"
                "Slackの Event Subscriptions → Request URL は "
                "https://<トンネルのホスト名>/slack/events になります。",
                file=sys.stderr,
            )
        except Exception as e:
            print(f"[mention] bot_user_id の取得に失敗しました: {e}", file=sys.stderr)
    else:
        print(
            "[mention] config/slack_signing_secret または SLACK_MENTION_USER が未設定のため、"
            "メンションでの発注は無効です。",
            file=sys.stderr,
        )

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Serving at http://localhost:{PORT}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        server.server_close()


if __name__ == "__main__":
    main()
