#!/usr/bin/env python3
"""Send a Slack message (stdin) for zozo-sales.

優先順:
1. Bot トークン (config/slack_bot_token または SLACK_BOT_TOKEN) —
   投稿先は ZOZO_SALES_CHANNEL または config/slack_channel
2. CDP (localhost:9222) 経由の xoxc/xoxd — 投稿先 workspace は
   config/slack_team_id、チャンネルは config/slack_channel。
   Slack が CDP 付きで起動していない場合はエラーにするだけで、再起動はしない。
"""
import json
import os
import sys
import urllib.parse
import urllib.request

CONF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
BOT_TOKEN_PATH = os.path.join(CONF_DIR, "slack_bot_token")
CHANNEL_PATH = os.path.join(CONF_DIR, "slack_channel")
TEAM_ID_PATH = os.path.join(CONF_DIR, "slack_team_id")


def read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def post_with_bot(token, channel, text):
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps({"channel": channel, "text": text}).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.loads(r.read())
    if not resp.get("ok"):
        sys.exit(f"[zozo-sales] chat.postMessage failed: {resp.get('error')}")
    print(f"sent to {channel} (bot)", file=sys.stderr)


def get_cdp_tokens(team_id):
    import websocket
    try:
        with urllib.request.urlopen("http://localhost:9222/json", timeout=3) as r:
            pages = json.loads(r.read())
    except OSError:
        sys.exit(
            "[zozo-sales] Bot トークン設定がなく、Slack の CDP (localhost:9222) にも接続できません。\n"
            f"Bot トークンを {BOT_TOKEN_PATH} に置くか、Slack を\n"
            "--remote-debugging-port=9222 --remote-allow-origins=* 付きで起動してください"
            "（自動再起動はしません）。"
        )
    page = next(p for p in pages if p["type"] == "page")
    ws = websocket.create_connection(page["webSocketDebuggerUrl"])
    try:
        ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
        cookies = json.loads(ws.recv())["result"]["cookies"]
        xoxd = next(c["value"] for c in cookies
                    if c["name"] == "d" and "slack.com" in c["domain"])
        ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
                            "params": {"expression": "localStorage.getItem('localConfig_v2')"}}))
        config = json.loads(json.loads(ws.recv())["result"]["result"]["value"])
        xoxc = config["teams"][team_id]["token"]
    finally:
        ws.close()
    return xoxc, xoxd


def cdp_api(method, body, xoxc, xoxd):
    data = urllib.parse.urlencode({"token": xoxc, **body}).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "Cookie": f"d={xoxd}"}
    req = urllib.request.Request(f"https://slack.com/api/{method}",
                                 data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def post_with_cdp(text):
    team_id = os.environ.get("ZOZO_SALES_TEAM_ID") or read_file(TEAM_ID_PATH)
    if not team_id:
        sys.exit(f"[zozo-sales] CDP 投稿には workspace の TEAM_ID が必要です。{TEAM_ID_PATH} に置いてください。")
    xoxc, xoxd = get_cdp_tokens(team_id)
    channel = os.environ.get("ZOZO_SALES_CHANNEL") or read_file(CHANNEL_PATH)
    if not channel:
        me = cdp_api("auth.test", {}, xoxc, xoxd)
        if not me.get("ok"):
            sys.exit(f"[zozo-sales] auth.test failed: {me}")
        dm = cdp_api("conversations.open", {"users": me["user_id"]}, xoxc, xoxd)
        if not dm.get("ok"):
            sys.exit(f"[zozo-sales] conversations.open failed: {dm}")
        channel = dm["channel"]["id"]
    resp = cdp_api("chat.postMessage", {"channel": channel, "text": text}, xoxc, xoxd)
    if not resp.get("ok"):
        sys.exit(f"[zozo-sales] chat.postMessage failed: {resp}")
    print(f"sent to {channel} (cdp)", file=sys.stderr)


def main():
    text = sys.stdin.read() if not sys.stdin.isatty() else (
        sys.argv[1] if len(sys.argv) > 1 else "")
    if not text.strip():
        print("Usage: echo 'msg' | send_slack.py", file=sys.stderr)
        sys.exit(1)

    bot_token = os.environ.get("SLACK_BOT_TOKEN") or read_file(BOT_TOKEN_PATH)
    if bot_token:
        channel = os.environ.get("ZOZO_SALES_CHANNEL") or read_file(CHANNEL_PATH)
        if not channel:
            sys.exit(f"[zozo-sales] 投稿先チャンネルが未設定です。ZOZO_SALES_CHANNEL か {CHANNEL_PATH} で指定してください。")
        post_with_bot(bot_token, channel, text)
    else:
        post_with_cdp(text)


if __name__ == "__main__":
    main()
