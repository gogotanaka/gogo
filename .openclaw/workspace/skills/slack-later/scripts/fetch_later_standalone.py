#!/usr/bin/env python3
"""Fetch Slack 'Save for later' items via CDP and output as JSON."""
import json
import sys
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

TEAM_ID = "T08KK9UCW"
TEAM_DOMAIN = "aisaac"

def get_tokens():
    """Get xoxc/xoxd from running Slack app via CDP on port 9222."""
    import websocket
    with urllib.request.urlopen("http://localhost:9222/json", timeout=3) as r:
        pages = json.loads(r.read())
    page = next(p for p in pages if p["type"] == "page")
    ws = websocket.create_connection(page["webSocketDebuggerUrl"])
    try:
        ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
        cookies = json.loads(ws.recv())["result"]["cookies"]
        xoxd = next(c["value"] for c in cookies if c["name"] == "d" and "slack.com" in c["domain"])
        ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {"expression": "localStorage.getItem('localConfig_v2')"}}))
        config = json.loads(json.loads(ws.recv())["result"]["result"]["value"])
        xoxc = config["teams"][TEAM_ID]["token"]
    finally:
        ws.close()
    return xoxc, xoxd

def api(method, body, xoxc, xoxd, form=False):
    if form:
        data = urllib.parse.urlencode({"token": xoxc, **body}).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Cookie": f"d={xoxd}"}
    else:
        data = json.dumps(body).encode()
        headers = {"Authorization": f"Bearer {xoxc}", "Content-Type": "application/json; charset=utf-8", "Cookie": f"d={xoxd}"}
    req = urllib.request.Request(f"https://slack.com/api/{method}", data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    xoxc, xoxd = get_tokens()

    # Fetch saved items
    items, cursor = [], None
    while True:
        payload = {"limit": min(limit, 50)}
        if cursor:
            payload["cursor"] = cursor
        resp = api("saved.list", payload, xoxc, xoxd)
        if not resp.get("ok"):
            print(f"Error: {resp.get('error')}", file=sys.stderr)
            sys.exit(1)
        items.extend(resp.get("saved_items", []))
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor or len(items) >= limit:
            break

    items = [i for i in items[:limit] if i.get("state") == "in_progress"]

    # Resolve channel names & messages in parallel
    def get_channel_name(ch):
        try:
            r = api("conversations.info", {"channel": ch}, xoxc, xoxd, form=True)
            return (ch, r["channel"]["name"]) if r.get("ok") else (ch, ch)
        except Exception:
            return (ch, ch)

    def get_message(item):
        ch, ts = item["item_id"], item["ts"]
        for method, params in [
            ("conversations.history", {"channel": ch, "latest": ts, "oldest": ts, "inclusive": "true", "limit": "1"}),
            ("conversations.replies", {"channel": ch, "ts": ts, "limit": "1", "inclusive": "true"}),
        ]:
            try:
                r = api(method, params, xoxc, xoxd, form=True)
                if r.get("ok") and r.get("messages"):
                    m = r["messages"][0]
                    return {"text": m.get("text", ""), "thread_ts": m.get("thread_ts", "")}
            except Exception:
                continue
        return {"text": "", "thread_ts": ""}

    with ThreadPoolExecutor(max_workers=10) as pool:
        ch_names = dict(pool.map(get_channel_name, {i["item_id"] for i in items}))
        messages = list(pool.map(get_message, items))

    # Build results
    results = []
    for item, msg in zip(items, messages):
        ch = item["item_id"]
        ts = item["ts"]
        link = f"https://{TEAM_DOMAIN}.slack.com/archives/{ch}/p{ts.replace('.', '')}"
        if msg.get("thread_ts"):
            link += f"?thread_ts={msg['thread_ts']}&cid={ch}"
        results.append({
            "saved_date": datetime.fromtimestamp(item["date_created"]).strftime("%Y-%m-%d %H:%M"),
            "channel": ch_names.get(ch, ch),
            "text": (msg["text"] or "")[:200],
            "link": link,
        })

    print(json.dumps(results, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
