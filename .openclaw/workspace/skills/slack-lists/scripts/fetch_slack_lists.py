#!/usr/bin/env python3
"""Fetch Slack List records via CDP and output as JSON."""

import json, sys, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

TEAM_ID = "T08KK9UCW"
TEAM_DOMAIN = "aisaac"
LIST_ID = "F07BYFCPUPK"
DONE_VALUES = {"OptM26K87YJ", "Opt3YFPZR2L"}
CHIKEN_VALUE = "Opt7I23CQYZ"
STATUS_COLS = ("Col09BFAWGRT2", "Col09F7VDC03G")


def get_tokens():
    import websocket
    with urllib.request.urlopen("http://localhost:9222/json", timeout=3) as r:
        pages = json.loads(r.read())
    ws = websocket.create_connection(next(p for p in pages if p["type"] == "page")["webSocketDebuggerUrl"])
    try:
        ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
        xoxd = next(c["value"] for c in json.loads(ws.recv())["result"]["cookies"] if c["name"] == "d" and "slack.com" in c["domain"])
        ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {"expression": "localStorage.getItem('localConfig_v2')"}}))
        xoxc = json.loads(json.loads(ws.recv())["result"]["result"]["value"])["teams"][TEAM_ID]["token"]
    finally:
        ws.close()
    return xoxc, xoxd


def api(method, params, xoxc, xoxd):
    data = urllib.parse.urlencode({"token": xoxc, **params}).encode()
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": f"d={xoxd}"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def field(record, key):
    return next((f for f in record.get("fields", []) if f["key"] == key), None)


def status(record):
    for f in record.get("fields", []):
        if f["key"] in STATUS_COLS:
            v = f.get("value", "")
            if v in DONE_VALUES:
                return "done"
            if v == CHIKEN_VALUE:
                return "知見"
    return "active"


def msg_info(record):
    f = field(record, "message") or field(record, "Col07TKQ6721K")
    if f and f.get("message"):
        m = f["message"][0]
        return m.get("channel_id", ""), m.get("ts", ""), m.get("thread_ts", "")
    return "", "", ""


def latest_reply(record, xoxc, xoxd):
    ch, ts, thread_ts = msg_info(record)
    if not ch or not ts:
        return ""
    root = thread_ts if thread_ts and thread_ts != "0000000000.000000" else ts
    try:
        r = api("conversations.replies", {"channel": ch, "ts": root, "limit": "3", "oldest": ts}, xoxc, xoxd)
        if r.get("ok") and r.get("messages"):
            return r["messages"][-1].get("text", "")[:200]
    except Exception:
        pass
    return ""


def main():
    list_id = sys.argv[1] if len(sys.argv) > 1 else LIST_ID
    xoxc, xoxd = get_tokens()
    resp = api("lists.records.list", {"list_id": list_id}, xoxc, xoxd)
    if not resp.get("ok"):
        sys.exit(f"Error: {resp.get('error')}")

    records = resp.get("records", [])
    active = [r for r in records if status(r) == "active"]
    with ThreadPoolExecutor(max_workers=10) as pool:
        replies = dict(zip(
            [r["id"] for r in active],
            pool.map(lambda r: latest_reply(r, xoxc, xoxd), active)
        ))

    results = []
    for r in records:
        ch, ts, thread_ts = msg_info(r)
        msg_link = ""
        if ch and ts:
            msg_link = f"https://{TEAM_DOMAIN}.slack.com/archives/{ch}/p{ts.replace('.', '')}"
            if thread_ts:
                msg_link += f"?thread_ts={thread_ts}&cid={ch}"

        name_f = field(r, "name")
        date_f = field(r, "Col07SR079AUC")
        next_f = field(r, "Col09AH3T509Z")

        results.append({
            "name": ((name_f.get("text", "") if name_f else "") or "")[:200],
            "status": status(r),
            "date": date_f.get("value", "") if date_f else "",
            "next_date": next_f.get("value", "") if next_f else "",
            "created": datetime.fromtimestamp(r["date_created"]).strftime("%Y-%m-%d") if r.get("date_created") else "",
            "list_link": f"https://{TEAM_DOMAIN}.slack.com/lists/{TEAM_ID}/{list_id}?record_id={r['id']}",
            "message_link": msg_link,
            "latest_reply": replies.get(r["id"], ""),
        })

    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
