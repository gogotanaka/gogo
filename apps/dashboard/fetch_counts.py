#!/usr/bin/env python3
"""Fetch Later / Activity / Drafts counts per Slack workspace via CDP.

Enumerates every team in localConfig_v2 (i.e. every workspace the user is
signed into on the Slack desktop app) and returns counts for:
  - later   : saved-for-later items in state "in_progress"
  - activity: items in the Activity feed
  - drafts  : message drafts

Slack internal API names for activity/drafts are not fully documented, so a
list of candidate (method, params) tuples is tried in order; the first one
returning ok=true and a usable count wins. The chosen method name is
reported back so the UI can show which endpoint actually answered.

Output: JSON array of
  {team_id, name, domain, later, activity, drafts,
   later_via, activity_via, drafts_via, error}
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

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
    print("[dashboard] restarting Slack with CDP...", file=sys.stderr)
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


def get_session():
    """Return (teams_dict, sidebar_order, xoxd_cookie).
    sidebar_order is a list of team_id in the order Slack's sidebar shows
    them, or [] if it couldn't be determined."""
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
                            "params": {"expression":
                                       "localStorage.getItem('localConfig_v2')"}}))
        config = json.loads(json.loads(ws.recv())["result"]["result"]["value"])
    finally:
        ws.close()
    teams = config.get("teams", {})
    sidebar_order = _extract_sidebar_order(config, teams)
    return teams, sidebar_order, xoxd


def _extract_sidebar_order(config, teams):
    """Try several known keys for the workspace sidebar order. Returns a
    list of team_ids that are also present in `teams`, or [] if nothing
    matched."""
    candidates = [
        config.get("orderedTeamIds"),  # Slack desktop ≥ 2025: actual sidebar order
        config.get("teamSidebarOrder"),
        config.get("workspaceOrder"),
        config.get("teamOrder"),
        config.get("team_order"),
        (config.get("desktopApp") or {}).get("teamSidebarOrder"),
        (config.get("desktopApp") or {}).get("workspaceOrder"),
        (config.get("clientPreferences") or {}).get("teamSidebarOrder"),
        (config.get("preferences") or {}).get("team_sidebar_order"),
    ]
    for order in candidates:
        if isinstance(order, list) and order:
            # Some versions store dicts like [{team_id: "..."}].
            normalized = []
            for x in order:
                if isinstance(x, str):
                    normalized.append(x)
                elif isinstance(x, dict):
                    tid = x.get("team_id") or x.get("id")
                    if tid:
                        normalized.append(tid)
            kept = [t for t in normalized if t in teams]
            if kept:
                return kept
    return []


def api(method, body, xoxc, xoxd, form=True):
    """Call slack.com/api/<method>. form=True uses x-www-form-urlencoded
    with token in the body (works for most legacy + internal endpoints)."""
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
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


# --- Counters ---------------------------------------------------------------

def count_later(xoxc, xoxd):
    """Count saved.list items with state=in_progress. Also count how many
    of those have a `date_due` in the past (overdue)."""
    import time as _time
    now = _time.time()
    total, overdue, cursor = 0, 0, None
    for _ in range(20):  # hard ceiling of 1000 items
        body = {"limit": 50}
        if cursor:
            body["cursor"] = cursor
        r = api("saved.list", body, xoxc, xoxd, form=False)
        if not r.get("ok"):
            raise RuntimeError(f"saved.list: {r.get('error')}")
        for it in r.get("saved_items", []):
            if it.get("state") != "in_progress":
                continue
            total += 1
            due = it.get("date_due") or 0
            if due and due < now:
                overdue += 1
        cursor = r.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return total, overdue, "saved.list"


def fetch_unread_mentions(xoxc, xoxd, my_uid, max_items=20):
    """Return (list, total) of true unread @-mention messages, newest first.

    `search.modules.messages is:unread` is unreliable (search index is
    eventually consistent — reads already-read mentions, and total_count
    is often wrong). Instead:

      1. Pull client.counts. Each channel/mpim/im has `mention_count` and
         `last_read`. mention_count for channels/mpims = actual @-mentions
         since last_read; for IMs it counts ALL unread (including bot
         daily pings that don't @-mention you), so we cannot just sum it.
      2. For every source with mention_count > 0, page conversations.history
         since last_read and keep messages whose text contains `<@my_uid>`.
         This gives an accurate, true-unread mention list.
      3. Resolve channel/IM metadata and permalinks in parallel.

    `last_read="0000000000.000000"` (never-read channel) causes
    invalid_ts_oldest — fall back to fetching the latest page.
    """
    r = api("client.counts",
            {"thread_counts_by_channel": "true",
             "org_wide_aware": "true",
             "include_file_channels": "true"},
            xoxc, xoxd, form=True)
    if not r.get("ok"):
        raise RuntimeError(f"client.counts: {r.get('error')}")

    sources = []  # (cid, last_read)
    for c in ((r.get("channels") or [])
              + (r.get("mpims") or [])
              + (r.get("ims") or [])):
        if c.get("mention_count"):
            sources.append((c["id"], c.get("last_read") or "0"))
    if not sources:
        return [], 0

    # Resolve info up front so we can filter out archived channels (Slack's
    # Mentions panel ignores them) and so we have the names ready for the
    # final output. Cache shared across the rest of the function.
    info_cache = {}

    def get_info(cid):
        try:
            ri = api("conversations.info", {"channel": cid},
                     xoxc, xoxd, form=True)
            return cid, (ri.get("channel") or {})
        except Exception:
            return cid, {}

    with ThreadPoolExecutor(max_workers=6) as ex:
        for cid, ch in ex.map(get_info, [s[0] for s in sources]):
            info_cache[cid] = ch

    sources = [(cid, lr) for cid, lr in sources
               if not info_cache.get(cid, {}).get("is_archived")]
    if not sources:
        return [], 0

    tok = f"<@{my_uid}>"
    # System-generated messages that contain `<@uid>` but aren't real
    # mentions and don't appear in Slack's Mentions & reactions view.
    SYSTEM_SUBTYPES = {
        "channel_join", "channel_leave", "channel_topic",
        "channel_purpose", "channel_name", "channel_archive",
        "channel_unarchive", "group_join", "group_leave",
        "group_topic", "group_purpose", "group_name",
        "group_archive", "group_unarchive", "pinned_item",
    }

    def is_real_mention(m):
        if tok not in (m.get("text") or ""):
            return False
        if m.get("subtype") in SYSTEM_SUBTYPES:
            return False
        # Slackbot ("you were added/removed" etc) is bot_message from USLACKBOT.
        if m.get("user") == "USLACKBOT":
            return False
        return True

    def scan(src):
        cid, last_read = src
        hits = []
        cursor = None
        for _ in range(5):  # cap 500 msgs/channel
            p = {"channel": cid, "inclusive": "false", "limit": 100}
            if last_read and not last_read.startswith("0000000000"):
                p["oldest"] = last_read
            if cursor:
                p["cursor"] = cursor
            h = api("conversations.history", p, xoxc, xoxd, form=True)
            if not h.get("ok"):
                break
            for m in h.get("messages") or []:
                if is_real_mention(m):
                    hits.append((cid, m))
            cursor = (h.get("response_metadata") or {}).get("next_cursor")
            if not cursor or not h.get("has_more"):
                break
        return hits

    all_hits = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for chunk in ex.map(scan, sources):
            all_hits.extend(chunk)

    all_hits.sort(key=lambda x: float(x[1].get("ts") or 0), reverse=True)
    total = len(all_hits)
    top = all_hits[:max_items]
    if not top:
        return [], 0

    # info_cache already populated above (for archived filtering).

    def get_pl(item):
        cid, m = item
        try:
            p = api("chat.getPermalink",
                    {"channel": cid, "message_ts": m["ts"]},
                    xoxc, xoxd, form=True)
            if p.get("ok"):
                return p.get("permalink")
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=6) as ex:
        permalinks = list(ex.map(get_pl, top))

    out = []
    for (cid, m), pl in zip(top, permalinks):
        info = info_cache.get(cid, {})
        out.append({
            "channel_id": cid,
            "channel_name": info.get("name"),
            "channel_is_im": bool(info.get("is_im")),
            "ts": m.get("ts"),
            "user": m.get("user"),
            "username": m.get("username"),
            "text": m.get("text") or "",
            "permalink": pl,
            "thread_ts": m.get("thread_ts"),
        })
    return out, total


def count_activity(xoxc, xoxd):
    """Try several known/candidate Activity endpoints. Returns (count, method)."""
    candidates = [
        # method,                 params,                          extractor
        ("activity.list",        {"limit": 1},                     lambda r: r.get("total")),
        ("activity.list",        {"limit": 1, "is_unread": "true"},
                                                                  lambda r: r.get("total")),
        ("users.activity.list",  {"limit": 1},                     lambda r: r.get("total")),
        ("client.counts",        {"thread_counts_by_channel": "true",
                                  "org_wide_aware": "true"},
         lambda r: (r.get("threads", {}).get("mention_count", 0) or 0)
                   + (r.get("threads", {}).get("has_unreads") and 1 or 0)),
    ]
    last_err = None
    for method, params, extract in candidates:
        try:
            r = api(method, params, xoxc, xoxd, form=True)
            if r.get("ok"):
                c = extract(r)
                if c is not None:
                    return int(c), method
                last_err = f"{method}: ok but no count"
            else:
                last_err = f"{method}: {r.get('error')}"
        except Exception as e:
            last_err = f"{method}: {e}"
    raise RuntimeError(last_err or "no activity endpoint worked")


def count_drafts(xoxc, xoxd):
    """Count *active* drafts (matches Slack sidebar's "Drafts" count).

    drafts.list quirks (verified 2026-05-29):
      - `count` is ignored; only `limit` is honored, capped at 100 per page
      - response includes historical drafts: skip is_sent=True or
        is_deleted=True
      - paging is *not* via response_metadata.next_cursor; pass
        `next_ts=<last item's last_updated_ts>` to fetch the next page
      - keep going while has_more is True
      - drafts whose destination channel is archived or deleted
        (conversations.info -> channel_not_found) ARE returned by the API
        but the sidebar hides them. Drop drafts whose every destination
        channel is archived or gone.
    """
    method = "drafts.list"
    actives = []
    next_ts = None
    for _ in range(100):  # hard ceiling: 10k drafts of history
        body = {"limit": 100}
        if next_ts:
            body["next_ts"] = next_ts
        r = api(method, body, xoxc, xoxd, form=True)
        if not r.get("ok"):
            raise RuntimeError(f"{method}: {r.get('error')}")
        drafts = r.get("drafts") or []
        for d in drafts:
            if d.get("is_sent") or d.get("is_deleted"):
                continue
            actives.append(d)
        if not r.get("has_more") or not drafts:
            break
        next_ts = drafts[-1].get("last_updated_ts")
        if not next_ts:
            break

    # Discover hidden destination channels — archived or deleted
    # (channel_not_found) — with one conversations.info call per unique cid.
    dest_cids = {x.get("channel_id")
                 for d in actives
                 for x in (d.get("destinations") or [])
                 if x.get("channel_id")}
    hidden = set()
    if dest_cids:
        def _is_hidden(cid):
            try:
                r = api("conversations.info", {"channel": cid},
                        xoxc, xoxd, form=True)
                if r.get("ok"):
                    if r.get("channel", {}).get("is_archived"):
                        return cid
                elif r.get("error") == "channel_not_found":
                    return cid
            except Exception:
                pass
            return None
        with ThreadPoolExecutor(max_workers=8) as ex:
            for cid in ex.map(_is_hidden, list(dest_cids)):
                if cid:
                    hidden.add(cid)

    def is_visible(d):
        dests = d.get("destinations") or []
        if not dests:
            return True
        return any(x.get("channel_id") not in hidden for x in dests)

    return sum(1 for d in actives if is_visible(d)), method


# --- Driver -----------------------------------------------------------------

def fetch_for_team(team_id, team_cfg, xoxd):
    xoxc = team_cfg.get("token")
    name = team_cfg.get("name") or team_cfg.get("domain") or team_id
    domain = team_cfg.get("domain") or ""
    out = {
        "team_id": team_id, "name": name, "domain": domain,
        "later": None, "later_overdue": None,
        "activity": None, "drafts": None,
        "later_via": None, "activity_via": None, "drafts_via": None,
        "mentions": [], "mentions_total": 0,
        "error": None,
    }
    if not xoxc:
        out["error"] = "no token in localConfig_v2"
        return out

    def add_err(prefix, e):
        out["error"] = ((out["error"] + "; ") if out["error"] else "") + f"{prefix}: {e}"

    try:
        out["later"], out["later_overdue"], out["later_via"] = \
            count_later(xoxc, xoxd)
    except Exception as e:
        add_err("later", e)
    try:
        out["activity"], out["activity_via"] = count_activity(xoxc, xoxd)
    except Exception as e:
        add_err("activity", e)
    try:
        out["drafts"], out["drafts_via"] = count_drafts(xoxc, xoxd)
    except Exception as e:
        add_err("drafts", e)
    try:
        me = api("auth.test", {}, xoxc, xoxd, form=True)
        if me.get("ok") and me.get("user_id"):
            out["mentions"], out["mentions_total"] = \
                fetch_unread_mentions(xoxc, xoxd, me["user_id"])
    except Exception as e:
        add_err("mentions", e)
    return out


def collect():
    ensure_slack_cdp()
    teams, sidebar_order, xoxd = get_session()
    if not teams:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(teams))) as ex:
        rows = list(ex.map(lambda kv: fetch_for_team(kv[0], kv[1], xoxd),
                           teams.items()))
    if sidebar_order:
        index = {tid: i for i, tid in enumerate(sidebar_order)}
        rows.sort(key=lambda r: (index.get(r["team_id"], 10_000),
                                 (r.get("name") or "").lower()))
    else:
        rows.sort(key=lambda r: (r.get("name") or "").lower())
    return rows


def main():
    rows = collect()
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
