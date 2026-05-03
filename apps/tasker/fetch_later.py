#!/usr/bin/env python3
"""Fetch Slack 'Save for later' items via CDP from the running Slack desktop.

Restarts Slack with --remote-debugging-port=9222 if CDP isn't up.
Outputs JSON array of {saved_date, channel, text, link}.
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

TEAM_ID = "T08KK9UCW"
TEAM_DOMAIN = "aisaac"
CDP_URL = "http://localhost:9222/json"


def cdp_up():
    try:
        urllib.request.urlopen(CDP_URL, timeout=2).read()
        return True
    except Exception:
        return False


def ensure_slack_cdp():
    if cdp_up():
        return
    print("[fetch_later] restarting Slack with CDP...", file=sys.stderr)
    subprocess.run(["osascript", "-e", 'quit app "Slack"'],
                   stderr=subprocess.DEVNULL, check=False)
    for _ in range(15):
        if subprocess.run(["pgrep", "-x", "Slack"],
                          stdout=subprocess.DEVNULL).returncode != 0:
            break
        time.sleep(1)
    subprocess.Popen(["open", "-na", "Slack", "--args",
                      "--remote-debugging-port=9222",
                      "--remote-allow-origins=*"])
    for _ in range(30):
        if cdp_up():
            return
        time.sleep(1)
    raise RuntimeError("Slack CDP did not come up within 30s")


def get_tokens():
    import websocket
    pages = json.loads(urllib.request.urlopen(CDP_URL, timeout=3).read())
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
        xoxc = config["teams"][TEAM_ID]["token"]
    finally:
        ws.close()
    return xoxc, xoxd


def api(method, body, xoxc, xoxd, form=False):
    if form:
        data = urllib.parse.urlencode({"token": xoxc, **body}).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded",
                   "Cookie": f"d={xoxd}"}
    else:
        data = json.dumps(body).encode()
        headers = {"Authorization": f"Bearer {xoxc}",
                   "Content-Type": "application/json; charset=utf-8",
                   "Cookie": f"d={xoxd}"}
    req = urllib.request.Request(f"https://slack.com/api/{method}",
                                 data=data, headers=headers)
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def resolve_channel(cid, xoxc, xoxd):
    resp = api("conversations.info", {"channel": cid}, xoxc, xoxd, form=True)
    if not resp.get("ok"):
        return cid
    ch = resp.get("channel", {})
    return ch.get("name") or ch.get("user") or cid


def resolve_message(cid, ts, xoxc, xoxd):
    for method, params in [
        ("conversations.history",
         {"channel": cid, "latest": ts, "oldest": ts,
          "inclusive": "true", "limit": "1"}),
        ("conversations.replies",
         {"channel": cid, "ts": ts, "limit": "1", "inclusive": "true"}),
    ]:
        try:
            r = api(method, params, xoxc, xoxd, form=True)
            if r.get("ok") and r.get("messages"):
                m = r["messages"][0]
                return m.get("text", ""), m.get("thread_ts", "")
        except Exception:
            continue
    return "", ""


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    ensure_slack_cdp()
    xoxc, xoxd = get_tokens()

    items, cursor = [], None
    while True:
        payload = {"limit": min(limit, 50)}
        if cursor:
            payload["cursor"] = cursor
        resp = api("saved.list", payload, xoxc, xoxd)
        if not resp.get("ok"):
            print(f"saved.list error: {resp.get('error')}", file=sys.stderr)
            sys.exit(1)
        items.extend(resp.get("saved_items", []))
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor or len(items) >= limit:
            break
    items = [i for i in items[:limit] if i.get("state") == "in_progress"]

    # Each saved item has item_id like "Ctype:channel:ts". Resolve in parallel.
    def hydrate(it):
        cid = it.get("item_id", "")
        ts = it.get("ts", "")
        if not cid or not ts:
            return None
        channel = resolve_channel(cid, xoxc, xoxd)
        text, thread_ts = resolve_message(cid, ts, xoxc, xoxd)
        saved_ts = it.get("date_created") or it.get("date_due") or 0
        saved_date = (datetime.fromtimestamp(saved_ts).strftime("%Y-%m-%d %H:%M")
                      if saved_ts else "")
        ts_frag = ts.replace(".", "")
        link = f"https://{TEAM_DOMAIN}.slack.com/archives/{cid}/p{ts_frag}"
        if thread_ts:
            link += f"?thread_ts={thread_ts}&cid={cid}"
        return {"saved_date": saved_date, "channel": channel,
                "text": (text or "")[:200], "link": link}

    with ThreadPoolExecutor(max_workers=8) as ex:
        hydrated = [h for h in ex.map(hydrate, items) if h]

    print(json.dumps(hydrated, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
