#!/usr/bin/env python3
"""Slack メンション経由の注文コマンド受信 (Socket Mode)。

例: `@sbi-order 買い 3930 200 742` のように、このアプリのSlack botにメンションで
話しかけると発注できる。安全のため:
- 発言者が SLACK_MENTION_USER と一致しない場合は何もせず、権限が無い旨を返信するだけ
- 書式が厳密に一致しない場合は何もせず、使い方を返信するだけ（曖昧な自然文解析はしない）
- 金額の上限（SBI_MAX_ORDER_VALUE_YEN）を超える注文は拒否する（誤入力の暴走を防ぐ）

このモジュールはSlackイベントの受信・検証・返信だけを行い、実際の発注（Playwright操作）
は一切行わない。パース済みコマンドは on_command コールバック経由で web.py 側の
キューに渡し、SBI/ブラウザに触る唯一のスレッド（_sbi_loop）がそこから処理する。
"""
import os
import re
import sys
import threading

from config import CONF_DIR, ENV

BOT_TOKEN_PATH = os.path.join(CONF_DIR, "slack_bot_token")
APP_TOKEN_PATH = os.path.join(CONF_DIR, "slack_app_token")

_SIDE_MAP = {
    "買い": "buy", "買": "buy", "buy": "buy",
    "売り": "sell", "売": "sell", "sell": "sell",
}
_COMMAND_RE = re.compile(
    r"^\s*(買い|買|buy|売り|売|sell)\s+(\d+)\s+(\d+)\s+([\d.]+)\s*$",
    re.IGNORECASE,
)
USAGE = "書式が正しくありません。例: `買い 3930 200 742`（買い/売り 銘柄コード 株数 価格）"


def _read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def parse_command(text, bot_user_id):
    """メンション本文からコマンドを取り出す。書式に合わなければ None。"""
    stripped = re.sub(rf"<@{re.escape(bot_user_id)}>", "", text).strip()
    m = _COMMAND_RE.match(stripped)
    if not m:
        return None
    side_raw, ticker, qty, price = m.groups()
    return {
        "side": _SIDE_MAP[side_raw.lower()],
        "ticker": ticker,
        "qty": int(qty),
        "price": float(price),
    }


def start(on_command):
    """Socket Modeリスナーをバックグラウンドスレッドで起動する。

    on_command(parsed, channel, thread_ts, reply) が、書式・権限チェックを通った
    コマンドについて呼ばれる。reply(text) はそのスレッドに返信するための関数。
    トークンが無ければ何もせず None を返す（メンション機能は無効のまま動く）。
    """
    bot_token = os.environ.get("SLACK_BOT_TOKEN") or _read_file(BOT_TOKEN_PATH)
    app_token = os.environ.get("SLACK_APP_TOKEN") or _read_file(APP_TOKEN_PATH)
    mention_user = ENV.get("SLACK_MENTION_USER", "")
    if not (bot_token and app_token and mention_user):
        print(
            "[mention] SLACK_APP_TOKEN/SLACK_MENTION_USER が未設定のため、"
            "メンションでの発注は無効です。",
            file=sys.stderr,
        )
        return None

    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    app = App(token=bot_token)
    bot_user_id = app.client.auth_test()["user_id"]

    @app.event("app_mention")
    def handle_mention(event, say):
        user = event.get("user")
        channel = event.get("channel")
        thread_ts = event.get("thread_ts") or event.get("ts")

        def reply(text):
            say(text=text, thread_ts=thread_ts)

        if user != mention_user:
            reply("権限がありません（登録済みユーザーのみ発注できます）。")
            return
        parsed = parse_command(event.get("text", ""), bot_user_id)
        if not parsed:
            reply(USAGE)
            return
        on_command(parsed, channel, thread_ts, reply)

    handler = SocketModeHandler(app, app_token)
    threading.Thread(target=handler.start, daemon=True).start()
    print("[mention] Slackメンションの受信を開始しました。", file=sys.stderr)
    return handler
