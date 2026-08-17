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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import notify
import order_store
import slack_client
from config import ENV
from sbi_client import HumanInterventionRequired, SBIClient

PORT = 8381
POLL_INTERVAL_SEC = 60
# 株価取得の間隔は固定にせず、機械的なアクセスパターンにならないよう毎回この範囲でランダムに決める。
PRICE_INTERVAL_MIN_SEC = int(ENV.get("SBI_PRICE_INTERVAL_MIN_SEC", "1200"))  # 20分
PRICE_INTERVAL_MAX_SEC = int(ENV.get("SBI_PRICE_INTERVAL_MAX_SEC", "1800"))  # 30分
WATCH_TICKERS = [t.strip() for t in ENV.get("SBI_WATCH_TICKERS", "").split(",") if t.strip()]
SLACK_CHANNEL = ENV.get("SLACK_CHANNEL", "")
SLACK_MENTION_USER = ENV.get("SLACK_MENTION_USER", "")


def _now():
    return datetime.now(timezone.utc).isoformat()

# Playwrightの同期APIはgreenlet実装のため、生成したスレッド以外から触ると
# "Cannot switch to a different thread" で壊れる。そのため SBIClient（＝ブラウザ
# とのやり取り全部）は _sbi_loop という単一のスレッドの中だけで生成・使用し、
# 他のスレッド（HTTPサーバ）は queue.Queue 経由でしか関与しない。
_work_q = queue.Queue()
_login_alert_sent = False


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
    except HumanInterventionRequired as e:
        order_store.update_order(order_id, status="error", error_message=str(e))
        _alert_login_needed(str(e))
    except Exception as e:
        order_store.update_order(order_id, status="error", error_message=str(e))
        notify.notify("SBI注文: 発注に失敗しました", f"id={order_id}: {e}")


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
        except HumanInterventionRequired as e:
            _alert_login_needed(str(e))
            return  # ログインが必要なら残りの注文チェックも今回はスキップ
        except Exception as e:
            print(f"[poller] order {order['id']} の確認に失敗: {e}", file=sys.stderr)


def _poll_price(client):
    try:
        client.ensure_logged_in()
        _clear_login_alert()
        lines = [f"{t}: {client.get_price(t)}円" for t in WATCH_TICKERS]
        slack_client.post(SLACK_CHANNEL, "\n".join(lines))
    except HumanInterventionRequired as e:
        _alert_login_needed(str(e))
    except Exception as e:
        print(f"[price] 株価取得に失敗しました: {e}", file=sys.stderr)


def _sbi_loop():
    """SBI/ブラウザに触る唯一のスレッド。発注キューの処理・約定確認・株価投稿を
    このスレッドの中で順番に行う（Playwrightの同期APIはスレッドを跨げないため）。
    """
    watch_price = bool(WATCH_TICKERS and SLACK_CHANNEL)
    if WATCH_TICKERS and not SLACK_CHANNEL:
        print("[price] SLACK_CHANNEL が未設定のため株価監視は行いません", file=sys.stderr)

    client = SBIClient().start()
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

    while True:
        try:
            order_id = _work_q.get(timeout=1)
            _process_order(client, order_id)
        except queue.Empty:
            pass

        now = time.monotonic()
        if now >= next_order_poll:
            _poll_orders(client)
            next_order_poll = now + POLL_INTERVAL_SEC
        if watch_price and now >= next_price_poll:
            _poll_price(client)
            next_price_poll = now + random.uniform(PRICE_INTERVAL_MIN_SEC, PRICE_INTERVAL_MAX_SEC)


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
        _work_q.put(order_id)
        self._respond(200, _render(order_store.list_orders(), "発注をキューに入れました"))

    def _respond(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *a):
        pass


def main():
    # ブラウザは起動時に立ち上げてログインし、以後常時起動しておく（都度開かない）。
    threading.Thread(target=_sbi_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Serving at http://localhost:{PORT}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        server.server_close()


if __name__ == "__main__":
    main()
