#!/usr/bin/env python3
"""Join all public Slack channels in the aisaac workspace via CDP."""
import json
import sys
import urllib.request
import urllib.parse
import time

TEAM_ID = "T08KK9UCW"
DRY_RUN = "--dry-run" in sys.argv


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
        ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
                            "params": {"expression": "localStorage.getItem('localConfig_v2')"}}))
        config = json.loads(json.loads(ws.recv())["result"]["result"]["value"])
        xoxc = config["teams"][TEAM_ID]["token"]
    finally:
        ws.close()
    return xoxc, xoxd


def api_get(method, params, xoxc, xoxd):
    qs = urllib.parse.urlencode({"token": xoxc, **params})
    url = f"https://slack.com/api/{method}?{qs}"
    req = urllib.request.Request(url, headers={"Cookie": f"d={xoxd}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def api_post(method, body, xoxc, xoxd):
    data = urllib.parse.urlencode({"token": xoxc, **body}).encode()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": f"d={xoxd}",
    }
    req = urllib.request.Request(f"https://slack.com/api/{method}", data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def list_all_public_channels(xoxc, xoxd):
    channels = []
    cursor = None
    while True:
        params = {"types": "public_channel", "limit": 200, "exclude_archived": "true"}
        if cursor:
            params["cursor"] = cursor
        resp = api_get("conversations.list", params, xoxc, xoxd)
        if not resp.get("ok"):
            print(f"[ERROR] conversations.list failed: {resp.get('error')}", file=sys.stderr)
            sys.exit(1)
        channels.extend(resp.get("channels", []))
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return channels


def main():
    print("🔑 Getting tokens via CDP...", flush=True)
    try:
        xoxc, xoxd = get_tokens()
    except Exception as e:
        print(f"[ERROR] Could not get tokens: {e}", file=sys.stderr)
        print("Slack desktop app must be running with CDP enabled on port 9222.", file=sys.stderr)
        sys.exit(1)

    print("📋 Fetching all public channels...", flush=True)
    channels = list_all_public_channels(xoxc, xoxd)
    print(f"   Found {len(channels)} public channels", flush=True)

    already_member = [c for c in channels if c.get("is_member")]
    to_join = [c for c in channels if not c.get("is_member")]

    print(f"   Already member: {len(already_member)}", flush=True)
    print(f"   Need to join:   {len(to_join)}", flush=True)

    if DRY_RUN:
        print("\n[DRY RUN] Would join:", flush=True)
        for c in to_join:
            print(f"  #{c['name']}", flush=True)
        return

    if not to_join:
        print("\n✅ Already in all public channels!", flush=True)
        return

    print(f"\n🚀 Joining {len(to_join)} channels...", flush=True)
    joined, errors = [], []
    for i, ch in enumerate(to_join, 1):
        resp = api_post("conversations.join", {"channel": ch["id"]}, xoxc, xoxd)
        if resp.get("ok"):
            print(f"  [{i}/{len(to_join)}] ✅ #{ch['name']}", flush=True)
            joined.append(ch["name"])
        else:
            err = resp.get("error", "unknown")
            print(f"  [{i}/{len(to_join)}] ❌ #{ch['name']}: {err}", flush=True)
            errors.append((ch["name"], err))
        # Rate limit: ~1 req/sec for Tier 3 to be safe
        time.sleep(1)

    print(f"\n{'='*40}")
    print(f"✅ Joined:  {len(joined)} channels")
    print(f"❌ Errors:  {len(errors)} channels")
    if errors:
        print("Errors:")
        for name, err in errors:
            print(f"  #{name}: {err}")


if __name__ == "__main__":
    main()
