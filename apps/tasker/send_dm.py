#!/usr/bin/env python3
"""Send a Slack message to the tasker channel on aisaac workspace, reusing
xoxc/xoxd from the running Slack desktop app via CDP (port 9222)."""
import json
import os
import sys
import urllib.parse
import urllib.request

TEAM_ID = "T08KK9UCW"
TARGET_CHANNEL = os.environ.get("TASKER_CHANNEL", "C02ETSXK33J")


def get_tokens():
    import websocket
    with urllib.request.urlopen("http://localhost:9222/json", timeout=3) as r:
        pages = json.loads(r.read())
    page = next(p for p in pages if p["type"] == "page")
    ws = websocket.create_connection(page["webSocketDebuggerUrl"])
    try:
        ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
        cookies = json.loads(ws.recv())["result"]["cookies"]
        xoxd = next(c["value"] for c in cookies if c["name"] == "d" and "slack.com" in c["domain"])
        ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
                            "params": {"expression": "localStorage.getItem('localConfig_v2')"}}))
        config = json.loads(json.loads(ws.recv())["result"]["result"]["value"])
        xoxc = config["teams"][TEAM_ID]["token"]
    finally:
        ws.close()
    return xoxc, xoxd


def api(method, body, xoxc, xoxd, form=True):
    if form:
        data = urllib.parse.urlencode({"token": xoxc, **body}).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Cookie": f"d={xoxd}"}
    else:
        data = json.dumps(body).encode()
        headers = {"Authorization": f"Bearer {xoxc}",
                   "Content-Type": "application/json; charset=utf-8",
                   "Cookie": f"d={xoxd}"}
    req = urllib.request.Request(f"https://slack.com/api/{method}", data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def main():
    text = sys.stdin.read() if not sys.stdin.isatty() else (sys.argv[1] if len(sys.argv) > 1 else "")
    if not text.strip():
        print("Usage: echo 'msg' | send_dm.py   OR   send_dm.py 'msg'", file=sys.stderr)
        sys.exit(1)

    xoxc, xoxd = get_tokens()
    channel = TARGET_CHANNEL

    resp = api("chat.postMessage", {"channel": channel, "text": text}, xoxc, xoxd)
    if not resp.get("ok"):
        print(f"chat.postMessage failed: {resp}", file=sys.stderr)
        sys.exit(1)

    print(f"sent: channel={channel} ts={resp.get('ts')}")


if __name__ == "__main__":
    main()
