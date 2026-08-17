#!/usr/bin/env python3
"""SBI証券 指値注文フォーム + 約定監視・通知。

指定した銘柄・株数・価格で注文を出し、約定したら macOS 通知で知らせる。
「いつ・いくらで買うか」の判断は人間が行う前提で、発注と約定確認だけを自動化する。

Usage: python3 web.py
初回は config/.env が必要（config/.env.example を参照）。
"""
import html
import queue
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import notify
import order_store
from sbi_client import HumanInterventionRequired, SBIClient

PORT = 8381
POLL_INTERVAL_SEC = 60


def _now():
    return datetime.now(timezone.utc).isoformat()

_work_q = queue.Queue()
_client = None
_client_lock = threading.Lock()


def _get_client():
    """SBIとのやり取りは全部1つのブラウザセッションに直列化する。"""
    global _client
    with _client_lock:
        if _client is None:
            _client = SBIClient().start()
        return _client


def _worker_loop():
    while True:
        order_id = _work_q.get()
        try:
            client = _get_client()
            client.ensure_logged_in()
            order = order_store.get_order(order_id)
            sbi_order_id = client.place_order(
                order["ticker"], order["side"], order["qty"], order["price"])
            order_store.update_order(
                order_id, status="submitted", sbi_order_id=sbi_order_id,
                submitted_at=_now())
        except HumanInterventionRequired as e:
            order_store.update_order(order_id, status="error", error_message=str(e))
            notify.notify("SBI注文: 人間の対応が必要です", str(e))
        except Exception as e:
            order_store.update_order(order_id, status="error", error_message=str(e))
            notify.notify("SBI注文: 発注に失敗しました", f"id={order_id}: {e}")


def _poller_loop():
    while True:
        time.sleep(POLL_INTERVAL_SEC)
        for order in order_store.pending_watch_orders():
            if not order.get("sbi_order_id"):
                continue
            try:
                client = _get_client()
                client.ensure_logged_in()
                status = client.check_order_status(order["sbi_order_id"])
                if status == "filled":
                    order_store.update_order(
                        order["id"], status="filled", filled_at=_now(),
                        notified_at=_now())
                    notify.notify(
                        "約定しました",
                        f"{order['ticker']} {order['side']} {order['qty']}株"
                        f" @ {order['price']}円",
                    )
                elif status == "cancelled":
                    order_store.update_order(order["id"], status="cancelled")
            except HumanInterventionRequired as e:
                notify.notify("SBI注文: 人間の対応が必要です", str(e))
            except Exception as e:
                print(f"[poller] order {order['id']} の確認に失敗: {e}", file=sys.stderr)


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
    threading.Thread(target=_worker_loop, daemon=True).start()
    threading.Thread(target=_poller_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Serving at http://localhost:{PORT}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        server.server_close()


if __name__ == "__main__":
    main()
