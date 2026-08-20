#!/usr/bin/env python3
"""SBI証券 指値注文 + watch/watch-open 自動リビッド + 約定監視・通知。

Slackメンションで buy/sell/watch/watch-open 等を受け付ける（README.md 参照）。
watch系は「その銘柄の未約定注文を取消して最良買気配に買い指値（rebid）」を
解除するまで繰り返す。約定したら Slack/macOS 通知で知らせる。

Usage: python3 web.py
初回は config/.env が必要（docs/setup.md のセットアップ手順を参照）。
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
import watch_store
import slack_client
from config import ENV
from sbi_client import (
    HumanInterventionRequired, SBIClient, format_order_book, order_row_status)

PORT = 8381
_BOT_USER_ID = None  # main()で解決。メンション本文から自分宛の<@ID>を取り除くのに使う。
POLL_INTERVAL_SEC = 60
# 板情報の定期投稿: 相場が開く日の8:00から、時計に揃えた固定間隔（既定10分毎:
# 8:00, 8:10, ...）で SLACK_MENTION_USER 宛メンション付きで投稿する。
PRICE_POST_INTERVAL_SEC = int(ENV.get("SBI_PRICE_POST_INTERVAL_SEC", "600"))
WATCH_TICKERS = [t.strip() for t in ENV.get("SBI_WATCH_TICKERS", "").split(",") if t.strip()]
SLACK_CHANNEL = ENV.get("SLACK_CHANNEL", "")
SLACK_MENTION_USER = ENV.get("SLACK_MENTION_USER", "")
# メンション経由の注文への安全弁: 見積金額がこれを超えたら発注せず拒否する。
MAX_ORDER_VALUE_YEN = float(ENV.get("SBI_MAX_ORDER_VALUE_YEN", "500000"))

# watch（常設rebid）/ watch-open（寄り付きrebid）の設定。
# 間隔・株数を±30%でランダムにするのは機械的なアクセスパターンを避けるため。
REBID_LOT_SIZE = int(ENV.get("SBI_REBID_LOT_SIZE", "100"))  # 売買単位
WATCH_JITTER = 0.3  # 平均株数・平均間隔の±30%
WATCH_MIN_INTERVAL_SEC = 60
WATCH_OPEN_START = dt_time(8, 59)
WATCH_OPEN_END = dt_time(9, 5, 59)
WATCH_OPEN_INTERVAL_SEC = int(ENV.get("SBI_WATCH_OPEN_INTERVAL_SEC", "20"))

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
# ログインが必要な状態が続く間、watch/watch-openのティックを一時的に控える
# （自動再ログインの試行が共有タブを奪い続け、人間のOTP入力を妨害しないため）。
# この時刻まではティックを見送り、期限が来たら1回だけ再試行する。
_login_backoff_until = 0.0
# 銘柄ごとの価格上限超え通知フラグ。超えている間の連投を防ぎ、上限以下に
# 戻ったらリセットして次の超過時にまた1回だけ知らせる（_sbi_loopスレッドのみが書く）。
_cap_alerted = {}
# web UI 表示用のスナップショット（ticker -> 直近の気配・アクション等）。
# _sbi_loop が書き、HTTPスレッドは読むだけ。
_loop_state = {}
# Slackメンションから来た注文は、結果をそのスレッドにも返信する。
# order_id -> (channel, thread_ts)。web UI経由の注文はここに登録されないので、
# _reply_mention_result は何もせず無視するだけになる。
_mention_reply_targets = {}


def _alert_login_needed(reason):
    """ログインが必要（想定外の画面）になったときに一度だけ知らせる。連投は避ける。"""
    global _login_alert_sent, _login_backoff_until
    _login_backoff_until = time.monotonic() + 300  # 5分はポーラー・ティックを控える
    if _login_alert_sent:
        return
    _login_alert_sent = True
    notify.notify("SBI: ログインが必要です", reason)
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
    """約定を通知する。Slack投稿に成功したら True（notified_at の確定に使う。
    失敗時は False を返し、ポーラーが次周期で再送する）。"""
    text = (
        f"約定しました: {order['ticker']} {order['side']} {order['qty']}株"
        f" @ {order['price']}円"
    )
    notify.notify("約定しました", text)
    if not SLACK_CHANNEL:
        return True
    try:
        slack_client.post(SLACK_CHANNEL, text)
        return True
    except Exception as e:
        print(f"[slack] 約定通知の投稿に失敗しました（後で再送します）: {e}",
              file=sys.stderr)
        return False


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


def _reconcile_orphan_order(client, known_ids, ticker, qty):
    """発注の受付確認に失敗した後、実は受注済みでないか注文照会と突合する。

    発注前に無かった同銘柄・同株数の新しい注文がちょうど1件あれば、それを
    自分の注文とみなして注文番号を返す（受注済み注文が誰にも追跡されず放置
    される事故の防止）。特定できなければ None。
    """
    if known_ids is None:
        return None
    try:
        rows = client.read_order_table()
    except Exception as e:
        print(f"[reconcile] 突合用の照会読み取りに失敗: {e}", file=sys.stderr)
        return None
    cands = [r for r in rows
             if r["order_id"] not in known_ids
             and r.get("ticker") == str(ticker) and r.get("qty") == qty]
    if len(cands) == 1:
        return cands[0]["order_id"]
    return None


def _process_order(client, order_id):
    try:
        client.ensure_logged_in()
        _clear_login_alert()
        order = order_store.get_order(order_id)
        # 受付確認失敗時の突合用に、発注前の注文一覧を控えておく（ベストエフォート）
        try:
            known_ids = {r["order_id"] for r in client.read_order_table()}
        except Exception:
            known_ids = None
        try:
            sbi_order_id = client.place_order(
                order["ticker"], order["side"], order["qty"], order["price"])
        except Exception as e:
            adopted = _reconcile_orphan_order(
                client, known_ids, order["ticker"], order["qty"])
            if not adopted:
                raise
            order_store.update_order(
                order_id, status="submitted", sbi_order_id=adopted,
                submitted_at=_now())
            _reply_mention_result(
                order_id,
                f"発注の受付確認に失敗しましたが、注文照会で確認できたため"
                f"追跡します: 注文番号{adopted} {order['ticker']} {order['side']}"
                f" {order['qty']}株 @ {order['price']}円（元のエラー: {e}）",
            )
            return
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
        _reply_mention_result(
            order_id,
            f"発注できませんでした（要対応）: {e}\n"
            f"※受注済みの可能性もあるため、注文照会も確認してください")
    except Exception as e:
        order_store.update_order(order_id, status="error", error_message=str(e))
        notify.notify("SBI注文: 発注に失敗しました", f"id={order_id}: {e}")
        _reply_mention_result(
            order_id,
            f"発注に失敗しました: {e}\n"
            f"※受注済みの可能性もあるため、注文照会も確認してください")


def _on_mention_command(parsed, channel, thread_ts, reply):
    """Slackメンションの受信（HTTPリクエストのスレッド）から呼ばれる。
    Playwrightには一切触らず、order_store と _work_q だけを操作する。
    """
    if (parsed["qty"] <= 0 or parsed["price"] <= 0
            or parsed["qty"] % REBID_LOT_SIZE != 0):
        # SBI側で却下される注文を発注前に弾く（却下は place_order の例外になり
        # 「ログインが必要」誤報とティック一時停止まで引き起こすため）
        reply(
            f"株数は売買単位（{REBID_LOT_SIZE}株）の正の倍数、価格は正の数で"
            f"指定してください（指定: {parsed['qty']}株 @ {parsed['price']}円）。"
        )
        return
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
    # キュー投入は返信より先に行う（返信失敗で注文が消えないように）
    _work_q.put(("order", order_id))
    reply(
        f"受け付けました: {parsed['ticker']} {parsed['side']} {parsed['qty']}株 "
        f"@ {parsed['price']}円 (見積 {value:,.0f}円)。処理します…"
    )


def _on_clear_all(channel, thread_ts, reply):
    """`clear all` メンションを受けたときの入口（HTTPリクエストのスレッドから
    呼ばれる）。Playwrightには一切触らず、キューに積むだけ。
    """
    _work_q.put(("clear_all", channel, thread_ts))  # 返信失敗で消えないよう先に積む
    reply("未約定の注文を全て取消します…")


def _max_watch_estimate(qty, price_cap):
    """watch のジッタ上振れ（+30%を売買単位に丸めた株数）での見積金額。"""
    max_qty = int(qty * (1 + WATCH_JITTER))
    max_qty -= max_qty % REBID_LOT_SIZE
    return max(max_qty, REBID_LOT_SIZE) * price_cap


def _on_watch(parsed, channel, thread_ts, reply):
    """`watch 銘柄 平均株数 上限価格 平均間隔秒` の入口（HTTPリクエストのスレッド）。
    設定ファイルだけを操作し、Playwrightには触らない。_sbi_loop が updated_at の
    変化を検知して場中なら即時に初回rebidを実行する。
    """
    if parsed["avg_qty"] < REBID_LOT_SIZE or parsed["avg_interval_sec"] <= 0:
        reply(
            f"平均株数は売買単位（{REBID_LOT_SIZE}株）以上、平均間隔秒は正の数で"
            f"指定してください。"
        )
        return
    if _max_watch_estimate(parsed["avg_qty"], parsed["price_cap"]) > MAX_ORDER_VALUE_YEN:
        reply(
            f"1回の見積金額（+30%上振れ時）が上限（{MAX_ORDER_VALUE_YEN:,.0f}円）を"
            f"超えるため設定しません。上限は SBI_MAX_ORDER_VALUE_YEN で変更できます。"
        )
        return
    prev = watch_store.set_watch(
        parsed["ticker"], parsed["avg_qty"], parsed["price_cap"],
        parsed["avg_interval_sec"])
    suppressed_note = (
        f"\n※この銘柄にはwatch-openが設定されています。watch-openがある間は"
        f"watch-openだけが実行され、このwatchは休止します"
        f"（`unwatch-open {parsed['ticker']}` で再開）。"
        if watch_store.get_watch_open(parsed["ticker"]) else "")
    reply(
        f"watch設定{'（置き換え）' if prev else ''}: {parsed['ticker']} を"
        f"平均{parsed['avg_qty']}株(±30%)・上限{parsed['price_cap']:,.0f}円・"
        f"平均{parsed['avg_interval_sec']}秒(±30%)間隔でrebid"
        f"（その銘柄の未約定注文を取消して最良買気配に買い指値）し続けます。"
        f"場中なら即時開始。解除は `unwatch {parsed['ticker']}`。{suppressed_note}"
    )


def _on_watch_open(parsed, channel, thread_ts, reply):
    """`watch-open buy 銘柄 株数 上限価格` の入口（HTTPリクエストのスレッド）。"""
    if parsed["qty"] <= 0 or parsed["qty"] % REBID_LOT_SIZE != 0:
        # 発注時に売買単位へ切り上げられて指定と食い違わないよう、設定時に弾く
        reply(
            f"株数は売買単位（{REBID_LOT_SIZE}株）の正の倍数で指定してください"
            f"（指定: {parsed['qty']}株）。"
        )
        return
    if parsed["qty"] * parsed["price_cap"] > MAX_ORDER_VALUE_YEN:
        reply(
            f"見積金額が上限（{MAX_ORDER_VALUE_YEN:,.0f}円）を超えるため設定しません。"
            f"上限は SBI_MAX_ORDER_VALUE_YEN で変更できます。"
        )
        return
    prev = watch_store.set_watch_open(
        parsed["ticker"], parsed["side"], parsed["qty"], parsed["price_cap"])
    suppressed_note = (
        f"\n※この銘柄のwatchは、watch-openがある間は休止します"
        f"（watch-openだけが実行されます）。"
        if watch_store.get_watch(parsed["ticker"]) else "")
    reply(
        f"watch-open設定{'（置き換え）' if prev else ''}: {parsed['ticker']} を"
        f"{parsed['qty']}株・上限{parsed['price_cap']:,.0f}円で、毎営業日の"
        f"8:59〜9:05に{WATCH_OPEN_INTERVAL_SEC}秒毎のrebidを実行します。"
        f"解除は `unwatch-open {parsed['ticker']}`。{suppressed_note}"
    )


def _on_unwatch(ticker, channel, thread_ts, reply):
    prev = watch_store.remove_watch(ticker)
    if prev is None:
        reply(f"{ticker} のwatchは設定されていません。")
        return
    reply(
        f"{ticker} のwatchを解除しました。未約定の注文は原則そのまま残ります"
        f"（rebid実行中の解除では、そのティックが取消済みのことがあります。"
        f"その場合は取消した注文番号を別途通知します。全て消すには `clear-all`）。"
    )


def _on_unwatch_open(ticker, channel, thread_ts, reply):
    prev = watch_store.remove_watch_open(ticker)
    if prev is None:
        reply(f"{ticker} のwatch-openは設定されていません。")
        return
    resume_note = (
        f" この銘柄のwatchが再開します（場中なら即時）。"
        if watch_store.get_watch(ticker) else "")
    reply(f"{ticker} のwatch-openを解除しました。{resume_note}")


def _on_book_request(ticker, channel, thread_ts, reply):
    """`book`/`book 3930` メンションを受けたときの入口（HTTPリクエストのスレッド
    から呼ばれる）。Playwrightには一切触らず、キューに積むだけ。
    """
    _work_q.put(("book", ticker, channel, thread_ts))  # 返信失敗で消えないよう先に積む
    reply("板情報を取得します…")


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
            slack_client.post(
                channel, "「注文中」の注文はありませんでした（待機中の注文は対象外）。",
                thread_ts=thread_ts)
            return
        lines = []
        for sbi_order_id in order_ids:
            try:
                result = _cancel_with_terminal_check(client, sbi_order_id)
                if result == "cancelled":
                    lines.append(f"注文{sbi_order_id}: 取消しました")
                else:
                    lines.append(f"注文{sbi_order_id}: 取消前に約定/取消済みでした")
            except HumanInterventionRequired as e:
                _alert_login_needed(str(e))
                lines.append(f"注文{sbi_order_id}: 取消できませんでした（要対応）")
            except Exception as e:
                lines.append(f"注文{sbi_order_id}: 取消に失敗しました ({e})")
        # 取消状態・取消間際の約定株数は照会の再読みで確定させる
        # （直接cancelledにすると部分約定分がfilled_qtyに残らない）
        try:
            _sync_orders_from_rows(client.read_order_table())
        except Exception as e:
            print(f"[clear-all] 取消後の照会再読みに失敗: {e}", file=sys.stderr)
        slack_client.post(channel, "全注文取消:\n" + "\n".join(lines), thread_ts=thread_ts)
    except HumanInterventionRequired as e:
        _alert_login_needed(str(e))
        slack_client.post(
            channel, f"取消処理を実行できませんでした（要対応）: {e}", thread_ts=thread_ts)
    except Exception as e:
        slack_client.post(channel, f"全注文取消でエラーが発生しました: {e}", thread_ts=thread_ts)


def _sync_orders_from_rows(rows):
    """注文照会のスナップショットを order_store に反映する。

    約定株数（filled_qty、部分約定含む）を先に反映してから終了状態
    （約定・取消・失効）を確定する。行が見つからない注文は日またぎ等で
    追跡対象から外れているので unknown で追跡を打ち切る（docs/adr/0011）。

    空のスナップショットは「読み取り異常の疑い」として何も反映しない
    （空読み1回で追跡中の全注文を unknown に落とすと、約定通知の喪失と
    「取消なしの新規発注=注文の積み上げ」に直結するため。日またぎで本当に
    空のケースは submitted のまま残るだけで実害はない）。
    """
    if not rows:
        if order_store.pending_watch_orders():
            print("[poller] 注文照会が空のスナップショットのため反映を見送ります"
                  "（読み取り異常の疑い）", file=sys.stderr)
        return
    by_id = {r["order_id"]: r for r in rows}
    for order in order_store.pending_watch_orders():
        if not order.get("sbi_order_id"):
            continue
        try:
            row = by_id.get(str(order["sbi_order_id"]))
            if row is None:
                order_store.update_order(order["id"], status="unknown")
                continue
            filled = row.get("filled_execs")
            if (filled is None and row.get("qty") is not None
                    and row.get("unfilled") is not None and "注文中" in row["status"]):
                filled = row["qty"] - row["unfilled"]
            if filled is not None and filled > (order.get("filled_qty") or 0):
                order_store.update_order(
                    order["id"], filled_qty=min(filled, order["qty"]))
            status = order_row_status(row)
            if status == "filled":
                order_store.update_order(
                    order["id"], status="filled", filled_qty=order["qty"],
                    filled_at=_now())
                if _announce_fill(order):
                    order_store.update_order(order["id"], notified_at=_now())
            elif status in ("cancelled", "expired"):
                order_store.update_order(order["id"], status=status)
        except Exception as e:
            print(f"[poller] order {order['id']} の反映に失敗: {e}", file=sys.stderr)


def _poll_orders(client):
    """未約定注文の状態確認。注文照会（全ての注文フィルタ）を1回だけ読み、
    追跡中の全注文をそのスナップショットで判定する（以前は注文ごとに照会を
    開いており、過度な遷移の原因かつ部分約定を拾えなかった）。
    """
    if not any(o.get("sbi_order_id") for o in order_store.pending_watch_orders()):
        # 追跡中の注文が無くても、Slack投稿に失敗した約定通知の再送だけは試みる
        _resend_unnotified_fills()
        return
    try:
        client.ensure_logged_in()
        _clear_login_alert()
        rows = client.read_order_table()
    except HumanInterventionRequired as e:
        _alert_login_needed(str(e))
        return
    except Exception as e:
        print(f"[poller] 注文照会の読み取りに失敗: {e}", file=sys.stderr)
        return
    _sync_orders_from_rows(rows)
    _resend_unnotified_fills()


def _resend_unnotified_fills():
    """約定済みなのにSlack通知に失敗したままの注文を再通知する。"""
    for order in order_store.unnotified_fills():
        if _announce_fill(order):
            order_store.update_order(order["id"], notified_at=_now())


def _poll_price(client):
    mention = f"<@{SLACK_MENTION_USER}> " if SLACK_MENTION_USER else ""
    try:
        client.ensure_logged_in()
        _clear_login_alert()
        for ticker in WATCH_TICKERS:
            book = client.get_order_book(ticker)
            slack_client.post(SLACK_CHANNEL, mention + format_order_book(ticker, book))
            print(f"[price] {ticker} の板を投稿しました", file=sys.stderr)
    except HumanInterventionRequired as e:
        _alert_login_needed(str(e))
    except Exception as e:
        print(f"[price] 板情報取得に失敗しました: {e}", file=sys.stderr)


def _watch_report(text):
    mention = f"<@{SLACK_MENTION_USER}> " if SLACK_MENTION_USER else ""
    print(f"[watch] {text}", file=sys.stderr)
    notify.notify("SBI watch", text)
    if SLACK_CHANNEL:
        try:
            slack_client.post(SLACK_CHANNEL, f"{mention}{text}")
        except Exception as e:
            print(f"[watch] Slack投稿に失敗しました: {e}", file=sys.stderr)


def _best_numeric_bid(client, ticker):
    """最良買気配の価格を float で返す。板は価格の降順なので、価格が数値で
    買数量がある最初の行が最良買気配。OVER/UNDER/成行の行は指値に使えないので
    飛ばす。読めなければ None。"""
    for row in client.get_order_book(ticker)["rows"]:
        if not row["bid_qty"]:
            continue
        try:
            # 1,000円以上の銘柄は価格セルがカンマ区切りになる
            return float(row["price"].replace(",", ""))
        except ValueError:
            continue
    return None


def _jitter(avg):
    """平均値の±30%（WATCH_JITTER）で毎回ランダムに揺らす。"""
    return avg * random.uniform(1 - WATCH_JITTER, 1 + WATCH_JITTER)


def _today_jst_start_utc():
    """JSTの今日0:00をUTCのISO文字列で返す（本日約定サマリの集計起点）。"""
    now_jst = datetime.now(JST)
    start = datetime(now_jst.year, now_jst.month, now_jst.day, tzinfo=JST)
    return start.astimezone(timezone.utc).isoformat()


def _cancel_with_terminal_check(client, sbi_order_id):
    """注文を取消す。照会スナップショットと取消の間に約定/取消されていた場合
    （TOCTOU）は正常系として扱う。

    戻り値: 'cancelled'（取消した） | 'already_terminal'（取消前に終端済み）。
    再読みしても本当にまだ注文中なら元の例外を投げ直す。
    """
    try:
        client.cancel_order(sbi_order_id)
        return "cancelled"
    except HumanInterventionRequired:
        rows = client.read_order_table()
        if not rows:
            raise  # 再読みが空 = 状態不明。終端済みと誤認して発注を続けない
        _sync_orders_from_rows(rows)
        row_now = next(
            (x for x in rows if x["order_id"] == str(sbi_order_id)), None)
        if row_now is not None and order_row_status(row_now) == "submitted":
            raise
        print(f"[watch] 注文{sbi_order_id}は取消前に約定/取消済みでした（正常続行）",
              file=sys.stderr)
        return "already_terminal"


def _rebid_tick(client, ticker, qty_target, price_cap, tag, still_valid,
                skip_if_at_bid=False):
    """1回の rebid: 最良買気配の確認 → 上限判定 → その銘柄の未約定注文を取消 →
    株数を売買単位に丸めて買い指値。

    still_valid() は発注直前に呼ばれ、False なら発注せず中断する（ティック中に
    unwatch や設定の置き換えが入った場合の安全弁。板取得や取消は数十秒かかるため
    HTTPスレッドの設定変更と重なりうる）。
    skip_if_at_bid は watch-open（20秒毎）用: 自分の注文が既に最良買気配と同値で
    並んでいるなら何もしない（取消→再発注は板の順番を失うだけ）。
    """
    state = _loop_state.setdefault(ticker, {})
    try:
        client.ensure_logged_in()
        _clear_login_alert()
        state.pop("last_error", None)  # ログインが通ったら「要対応」連投抑止を解除

        bid = _best_numeric_bid(client, ticker)
        state["last_check"] = datetime.now(JST).strftime("%m/%d %H:%M:%S")
        state["last_bid"] = bid
        if bid is None:
            _watch_report(f"{tag}: {ticker} の最良買気配が取れなかったため見送りました")
            state["last_action"] = "気配が読めず見送り"
            return
        cap_key = f"{tag}:{ticker}"  # watchとwatch-openで上限が違いうるので別々に管理
        if bid > price_cap:
            if not _cap_alerted.get(cap_key):
                _cap_alerted[cap_key] = True
                _watch_report(
                    f"{tag}: {ticker} の最良買気配が上限({price_cap:,.0f}円)を"
                    f"超えました（現在 {bid}円）。上限以下に戻るまで発注を見送ります"
                    f"（この通知は繰り返しません）")
            state["last_action"] = f"上限超え（気配 {bid}円 > {price_cap:,.0f}円）で見送り"
            return
        _cap_alerted[cap_key] = False

        rows = client.read_order_table()
        if not rows and order_store.pending_watch_orders():
            # 追跡中の注文があるのに照会が空 = フィルタ切替失敗等の読み取り異常の
            # 疑い。取消できていないまま発注すると注文が積み上がるので見送る。
            # （このガードはsyncより先に評価する。先にsyncすると空読みが全注文を
            # unknown化してpending_watch_ordersが空になり、ガードが死ぬ）
            _watch_report(
                f"{tag}: {ticker} の注文照会が空でした（追跡中の注文があるため"
                f"読み取り異常の疑い）。今回の発注は見送ります")
            state["last_action"] = "照会が読めず見送り"
            return
        _sync_orders_from_rows(rows)
        # 「注文中(一部約定)」も取り残すと二重発注になるため部分一致で拾う
        pending = [r for r in rows
                   if "注文中" in r["status"] and r.get("ticker") == ticker]

        if skip_if_at_bid and pending:
            own = [order_store.get_order_by_sbi_id(r["order_id"]) for r in pending]
            if all(o and o["side"] == "buy" and o["price"] == bid for o in own):
                state["last_action"] = f"既に最良買気配({bid}円)に注文あり（{tag}）"
                return  # 取消→再発注しても板の順番を失うだけなので何もしない

        cancelled = []
        for r in pending:
            _cancel_with_terminal_check(client, r["order_id"])
            cancelled.append(r["order_id"])
        if cancelled:
            # 取消間際に入った約定を含む確定値を order_store に反映する
            rows = client.read_order_table()
            _sync_orders_from_rows(rows)
        cancelled_note = f"注文{', '.join(cancelled)}を取消 → " if cancelled else ""

        if not still_valid():
            _watch_report(
                f"{tag}: {ticker} の設定がティック中に変更/解除されたため、"
                f"今回の発注は見送りました"
                + (f"（{cancelled_note.rstrip(' →')}済み。再発注はしません）"
                   if cancelled else ""))
            state["last_action"] = "設定変更を検知し発注中断"
            return

        qty = int(qty_target)
        qty -= qty % REBID_LOT_SIZE
        qty = max(qty, REBID_LOT_SIZE)
        # 安全弁（SBI_MAX_ORDER_VALUE_YEN）。上限内に収まるまで数量を下げるが、
        # 売買単位を割ってまでは発注しない。
        while qty > REBID_LOT_SIZE and qty * bid > MAX_ORDER_VALUE_YEN:
            qty -= REBID_LOT_SIZE
        if qty * bid > MAX_ORDER_VALUE_YEN:
            _watch_report(
                f"{tag}: {cancelled_note}最小数量({REBID_LOT_SIZE}株)でも見積が"
                f"上限({MAX_ORDER_VALUE_YEN:,.0f}円)を超えるため見送りました"
                f"（最良買気配 {bid}円）")
            state["last_action"] = "金額上限超えで見送り"
            return

        known_ids = {r["order_id"] for r in rows}
        order_id = order_store.create_order(ticker, "buy", qty, bid)
        try:
            sbi_order_id = client.place_order(ticker, "buy", qty, bid)
        except Exception as e:
            order_store.update_order(order_id, status="error", error_message=str(e))
            # 受付確認だけ失敗して実は受注済み、の可能性があるので照会と突合する
            adopted = _reconcile_orphan_order(client, known_ids, ticker, qty)
            if adopted:
                order_store.update_order(
                    order_id, status="submitted", sbi_order_id=adopted,
                    submitted_at=_now(), error_message=None)
                _watch_report(
                    f"{tag}: {cancelled_note}発注の受付確認に失敗しましたが、"
                    f"注文照会で確認できたため追跡します: {ticker} 買 {qty}株"
                    f" @ {bid}円 (注文番号{adopted})")
                state["last_action"] = f"買 {qty}株 @ {bid}円 (注文番号{adopted}, 突合で確認)"
            else:
                _watch_report(
                    f"{tag}: {cancelled_note}発注に失敗しました（受注済みの可能性も"
                    f"あるため注文照会を確認してください）: {e}")
                state["last_action"] = f"発注失敗: {e}"
            return
        order_store.update_order(
            order_id, status="submitted", sbi_order_id=sbi_order_id,
            submitted_at=_now())
        bought_today = order_store.filled_qty_since(
            ticker, "buy", _today_jst_start_utc())
        _watch_report(
            f"{tag}: {cancelled_note}{ticker} 買 {qty}株 @ {bid}円"
            f" (注文番号{sbi_order_id}) — 本日約定 {bought_today}株")
        state["last_action"] = f"{cancelled_note}買 {qty}株 @ {bid}円 (注文番号{sbi_order_id})"
    except HumanInterventionRequired as e:
        _alert_login_needed(str(e))
        # ログアウトが続く間、毎ティック同じ「要対応」をメンションで連投しない
        if state.get("last_error") != str(e):
            state["last_error"] = str(e)
            _watch_report(f"{tag}: 実行できませんでした（要対応）: {e}")
        else:
            print(f"[watch] {tag}: 要対応が継続中: {e}", file=sys.stderr)
        state["last_action"] = f"要対応: {e}"
        return
    except Exception as e:
        # watch-open窓(20秒毎)で同じ失敗が続いたときの連投を防ぐ
        if state.get("last_error") != str(e):
            state["last_error"] = str(e)
            _watch_report(f"{tag}: 実行に失敗しました: {e}")
        else:
            print(f"[watch] {tag}: 失敗が継続中: {e}", file=sys.stderr)
        state["last_action"] = f"失敗: {e}"


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

    abandoned = order_store.abandon_stale_pending()
    if abandoned:
        detail = ", ".join(
            f"{o['ticker']} {o['side']} {o['qty']}株 @ {o['price']}円"
            for o in abandoned)
        print(f"[startup] 前回のキューに残っていた注文 {len(abandoned)}件を"
              f"error（未発注）として打ち切りました: {detail}", file=sys.stderr)
        # 「受け付けました」と返信済みの注文が静かに消えないよう知らせる
        _watch_report(
            f"再起動により、受付済みで未発注の注文 {len(abandoned)}件を"
            f"取りやめました（発注されていません）: {detail}")

    client = _connect_with_retry()
    try:
        client.ensure_logged_in()
        _clear_login_alert()
    except HumanInterventionRequired as e:
        _alert_login_needed(str(e))
    except Exception as e:
        print(f"[startup] 起動時ログインに失敗しました: {e}", file=sys.stderr)

    next_order_poll = time.monotonic() + POLL_INTERVAL_SEC
    # 板情報の定期投稿は時計に揃えたスロット（既定600秒 = 8:00, 8:10, ...）。
    # 起動直後の場中は前のスロット扱いで1回すぐ投稿される。スロットは場外でも
    # 進める（進めないと昼休み明けにグリッド外の12:05投稿が入る）。
    last_price_slot = None
    # watch のスケジュール（銘柄ごと）。seen_watch_updated は設定の新規・置き換えを
    # 検知して即時ティックするための「最後に見た updated_at」。起動時に既存設定で
    # プライムし、再起動を「設定変更」と誤認して即ティック（=生きている注文を
    # 取消して板の待ち順を失う）しないようにする。既存watchは60秒後から再開。
    _init_watches = watch_store.load()
    seen_watch_updated = {
        t: w["updated_at"] for t, w in _init_watches["watches"].items()}
    next_watch_tick = {
        t: time.monotonic() + WATCH_MIN_INTERVAL_SEC
        for t in _init_watches["watches"]}
    seen_open_updated = {
        t: w["updated_at"] for t, w in _init_watches["watch_opens"].items()}
    next_open_tick = {}

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
            # ブラウザごと再起動された場合はCDP接続が死ぬ。検知したら張り直す
            # （タブ単位のクラッシュは ensure_logged_in 内の復旧が担当）。
            try:
                if client._browser and not client._browser.is_connected():
                    print("[sbi_loop] ブラウザとのCDP接続が切れたため再接続します",
                          file=sys.stderr)
                    try:
                        client.stop()
                    except Exception:
                        pass
                    client = _connect_with_retry()
            except Exception as e:
                print(f"[sbi_loop] 接続状態の確認に失敗: {e}", file=sys.stderr)

            if _login_alert_sent and now < _login_backoff_until:
                # ログイン待ちの間はポーラーもrebidも控える（人間のOTP入力の
                # 邪魔をしない: ensure_logged_in が共有タブをログイン画面へ
                # 奪ってしまうため）。期限が来たら通常の周期処理が1回だけ
                # 再試行し、成功すれば解除、失敗すればまた5分控える。
                continue

            data = watch_store.load()
            now_jst = datetime.now(JST)
            in_open_window = (
                now_jst.weekday() < 5
                and WATCH_OPEN_START <= now_jst.time() <= WATCH_OPEN_END)
            open_tick_due = in_open_window and any(
                now >= next_open_tick.get(t, 0) for t in data["watch_opens"])

            if now >= next_order_poll:
                _poll_orders(client)
                next_order_poll = now + POLL_INTERVAL_SEC
            if watch_price:
                slot = int(time.time() // PRICE_POST_INTERVAL_SEC)
                # watch-openのティック期日中は板投稿を後回しにする（スロットを
                # 消費しないので、ティック後の次の周回で同じスロット分が投稿される）
                if slot != last_price_slot and not open_tick_due:
                    last_price_slot = slot
                    if _is_market_hours():
                        _poll_price(client)

            if in_open_window:
                for ticker, wo in data["watch_opens"].items():
                    if seen_open_updated.get(ticker) != wo["updated_at"]:
                        seen_open_updated[ticker] = wo["updated_at"]
                        _cap_alerted.pop(f"watch-open:{ticker}", None)
                    if now < next_open_tick.get(ticker, 0):
                        continue
                    _rebid_tick(
                        client, ticker, wo["qty"], wo["price_cap"], "watch-open",
                        still_valid=lambda t=ticker, w=wo:
                            watch_store.get_watch_open(t) == w,
                        skip_if_at_bid=True)
                    next_open_tick[ticker] = time.monotonic() + WATCH_OPEN_INTERVAL_SEC

            if _is_market_hours(now_jst):
                for ticker, w in data["watches"].items():
                    if ticker in data["watch_opens"]:
                        # 同一銘柄に watch-open がある間は watch-open だけを実行する
                        # （watch は設定を残したまま休止。unwatch-open で再開）
                        continue
                    if seen_watch_updated.get(ticker) != w["updated_at"]:
                        # 新規設定・置き換えは即時に初回ティック。
                        # 上限超え通知の抑止も新設定でリセットする
                        seen_watch_updated[ticker] = w["updated_at"]
                        next_watch_tick[ticker] = 0
                        _cap_alerted.pop(f"watch:{ticker}", None)
                    if now < next_watch_tick.get(ticker, 0):
                        continue
                    _rebid_tick(
                        client, ticker, _jitter(w["avg_qty"]), w["price_cap"],
                        "watch",
                        still_valid=lambda t=ticker, w0=w:
                            watch_store.get_watch(t) == w0)
                    next_watch_tick[ticker] = time.monotonic() + max(
                        WATCH_MIN_INTERVAL_SEC, _jitter(w["avg_interval_sec"]))
        except Exception as e:
            print(f"[sbi_loop] ポーリングで想定外のエラー: {e}", file=sys.stderr)


# --- HTML ---

def _render(orders, message=""):
    data = watch_store.load()
    today_start = _today_jst_start_utc()

    watch_rows = ""
    for ticker, w in sorted(data["watches"].items()):
        st = _loop_state.get(ticker, {})
        bought = order_store.filled_qty_since(ticker, "buy", today_start)
        last_bid = st.get("last_bid")
        suppressed = ticker in data["watch_opens"]
        action = ("watch-open優先のため休止中（unwatch-openで再開）"
                  if suppressed else str(st.get("last_action") or "—"))
        watch_rows += (
            f"<tr><td>{html.escape(ticker)}</td>"
            f"<td>平均{w['avg_qty']}株 (±30%)</td>"
            f"<td>{w['price_cap']:,.0f}円</td>"
            f"<td>平均{w['avg_interval_sec']}秒 (±30%)</td>"
            f"<td>{html.escape(st.get('last_check') or '—')}</td>"
            f"<td>{html.escape(str(last_bid) + '円' if last_bid is not None else '—')}</td>"
            f"<td>{html.escape(action)}</td>"
            f"<td>{bought}株</td></tr>"
        )
    if not watch_rows:
        watch_rows = '<tr><td colspan="8" class="empty">（watchなし）</td></tr>'

    open_rows = ""
    for ticker, wo in sorted(data["watch_opens"].items()):
        st = _loop_state.get(ticker, {})
        open_rows += (
            f"<tr><td>{html.escape(ticker)}</td>"
            f"<td>{wo['qty']}株</td>"
            f"<td>{wo['price_cap']:,.0f}円</td>"
            f"<td>毎営業日 8:59〜9:05 / {WATCH_OPEN_INTERVAL_SEC}秒毎</td>"
            f"<td>{html.escape(str(st.get('last_action') or '—'))}</td></tr>"
        )
    if not open_rows:
        open_rows = '<tr><td colspan="5" class="empty">（watch-openなし）</td></tr>'

    rows = "".join(
        f"<tr><td>{o['id']}</td><td>{html.escape(o['ticker'])}</td>"
        f"<td>{html.escape(o['side'])}</td><td>{o['qty']}</td><td>{o['price']}</td>"
        f"<td>{html.escape(o['status'])}</td>"
        f"<td>{o.get('filled_qty') if o.get('filled_qty') is not None else ''}</td>"
        f"<td>{html.escape(o.get('error_message') or '')}</td></tr>"
        for o in orders
    )
    msg_html = f'<p class="msg">{html.escape(message)}</p>' if message else ""
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<title>SBI 発注 / watch状況</title>
<style>
body{{font-family:-apple-system,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem}}
table{{border-collapse:collapse;width:100%;margin:.5rem 0 1.5rem}}
td,th{{border:1px solid #ddd;padding:.4rem .6rem;text-align:left;font-size:.9rem}}
form{{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}}
input,select{{padding:.4rem}}
.msg{{color:#c00}}
.empty{{color:#999}}
h2{{margin:1.5rem 0 0}}
.note{{color:#666;font-size:.85rem}}
</style></head><body>
<h1>SBI 発注 / watch状況</h1>
{msg_html}
<h2>watch（常設rebid）</h2>
<p class="note">場中、平均間隔(±30%)ごとに「その銘柄の未約定注文を取消 → 最良買気配に平均株数(±30%)の買い指値」。設定・解除はSlackメンション（<code>watch 3930 400 760 900</code> / <code>unwatch 3930</code>）。</p>
<table>
<tr><th>銘柄</th><th>株数</th><th>上限価格</th><th>間隔</th><th>最終チェック</th><th>最良買気配</th><th>直近アクション</th><th>本日約定</th></tr>
{watch_rows}
</table>
<h2>watch-open（寄り付きrebid）</h2>
<table>
<tr><th>銘柄</th><th>株数</th><th>上限価格</th><th>実行タイミング</th><th>直近アクション</th></tr>
{open_rows}
</table>
<h2>手動発注</h2>
<form method="post" action="/orders">
  <input name="ticker" placeholder="銘柄コード" required>
  <select name="side"><option value="buy">買</option><option value="sell">売</option></select>
  <input name="qty" type="number" placeholder="株数" required>
  <input name="price" type="number" step="0.1" placeholder="指値価格" required>
  <button type="submit">発注</button>
</form>
<h2>注文履歴</h2>
<table>
<tr><th>ID</th><th>銘柄</th><th>売買</th><th>株数</th><th>価格</th><th>状態</th><th>約定株数</th><th>エラー</th></tr>
{rows}
</table>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _is_local_request(self):
        """web UI・手動発注をローカルからのアクセスに限定する。

        サーバは127.0.0.1にバインドしているが、cloudflaredの公開トンネルが
        /slack/events のためにこのポートへ向いているので、トンネル経由の
        リクエストも127.0.0.1から来る。トンネル経由はCloudflareのヘッダ
        （Cf-Ray等）が付き、Hostが公開ホスト名になるので、それで見分ける。
        これが無いと第三者が公開URLから実発注できてしまう。
        """
        if self.headers.get("Cf-Ray") or self.headers.get("Cf-Connecting-Ip"):
            return False
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("localhost", "127.0.0.1")

    def do_GET(self):
        if self.path != "/" or not self._is_local_request():
            self.send_response(404)
            self.end_headers()
            return
        self._respond(200, _render(order_store.list_orders()))

    def do_POST(self):
        if self.path == "/slack/events":
            self._handle_slack_event()
            return
        if self.path != "/orders" or not self._is_local_request():
            self.send_response(404)
            self.end_headers()
            return
        # ブラウザ発のクロスサイトPOST（CSRF）対策: Originが付いていて
        # ローカル以外なら拒否する（curl等Origin無しのローカル操作は許可）。
        origin = self.headers.get("Origin", "")
        if origin and not origin.startswith(("http://localhost", "http://127.0.0.1")):
            self._respond(403, _render(order_store.list_orders(), "拒否しました"))
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
        if qty * price > MAX_ORDER_VALUE_YEN:
            self._respond(400, _render(
                order_store.list_orders(),
                f"見積金額が上限（{MAX_ORDER_VALUE_YEN:,.0f}円）を超えるため発注しません"))
            return
        order_id = order_store.create_order(ticker, side, qty, price)
        _work_q.put(("order", order_id))
        self._respond(200, _render(order_store.list_orders(), "発注をキューに入れました"))

    def _handle_slack_event(self):
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)
        if _BOT_USER_ID is None:
            # 起動時に auth.test が失敗したままイベントを処理すると
            # re.escape(None) で落ちる。200で受けて処理はしない（Slackの再送
            # ループを避ける）。復旧には web.py の再起動が必要。
            print("[slack-event] _BOT_USER_ID 未解決のためイベントを無視しました"
                  "（web.py を再起動してください）", file=sys.stderr)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok (bot id unresolved)")
            return
        print(f"[slack-event] received {length} bytes: {raw_body[:300]!r}", file=sys.stderr)
        status, body = mention_listener.handle_event(
            self.headers, raw_body, _BOT_USER_ID, {
                "order": _on_mention_command,
                "clear_all": _on_clear_all,
                "book": _on_book_request,
                "watch": _on_watch,
                "watch_open": _on_watch_open,
                "unwatch": _on_unwatch,
                "unwatch_open": _on_unwatch_open,
            })
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
