#!/usr/bin/env python3
"""Fetch a Slack List (e.g. "people-reminders") + linked message threads.

Uses xoxc/xoxd extracted via CDP from the running Slack desktop (port 9222).

Slack's "Lists" feature is an *internal* product without a public API surface,
so the endpoint names here are discovered empirically. We try a bunch of
candidate (method, param) combos for both list discovery and row fetching,
and log every attempt to stderr so unsupported names can be eliminated.

Output: JSON array of {row_id, title, columns, thread, link, ...}.

Env vars:
  PEOPLE_REMINDERS_LIST_ID  — skip discovery, fetch this Slack file id directly
  PEOPLE_REMINDERS_DEBUG=1  — dump raw API responses to stderr

Usage:
  python3 fetch_list.py [list_name]      # default list_name = "people-reminders"
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
DEFAULT_LIST_NAME = "people-reminders"
DEBUG = os.environ.get("PEOPLE_REMINDERS_DEBUG") == "1"


def log(msg):
    print(f"[fetch_list] {msg}", file=sys.stderr)


def dbg(msg):
    if DEBUG:
        print(f"[fetch_list][dbg] {msg}", file=sys.stderr)


# --- CDP / token extraction ---

def cdp_up():
    try:
        urllib.request.urlopen(CDP_URL, timeout=2).read()
        return True
    except Exception:
        return False


def ensure_slack_cdp():
    if cdp_up():
        return
    log("restarting Slack with CDP...")
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


def api(method, body, xoxc, xoxd, form=True):
    if form:
        data = urllib.parse.urlencode(
            {"token": xoxc, **{k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                               for k, v in body.items()}}
        ).encode()
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


def try_api(method, body, xoxc, xoxd):
    """Call API and log result. Returns (ok, response_or_error_string)."""
    try:
        r = api(method, body, xoxc, xoxd, form=True)
    except Exception as e:
        log(f"  {method} -> EXC {e}")
        return False, str(e)
    if r.get("ok"):
        keys = list(r.keys())
        log(f"  {method} -> ok (keys={keys})")
        dbg(f"  full resp: {json.dumps(r, ensure_ascii=False)[:600]}")
        return True, r
    else:
        log(f"  {method} -> err {r.get('error')}")
        dbg(f"  full err: {json.dumps(r, ensure_ascii=False)[:400]}")
        return False, r.get("error", "unknown")


# --- list discovery ---

# (method, params_dict_or_lambda)
LIST_INDEX_ENDPOINTS = [
    # files.list with types=lists is the reliable one. Lists have name="list"
    # always, real display name is in `title`.
    ("files.list", {"types": "lists", "count": 100, "page": 1}),
]


def _list_entries_from_response(resp):
    for key in ("lists", "items", "files", "records"):
        if isinstance(resp.get(key), list):
            return resp[key]
    return []


def _candidate_names(entry):
    """All strings that could be the user-visible name of a list file."""
    return [s for s in (entry.get("title"), entry.get("name")) if s]


def find_list_id(name, xoxc, xoxd):
    if os.environ.get("PEOPLE_REMINDERS_LIST_ID"):
        lid = os.environ["PEOPLE_REMINDERS_LIST_ID"]
        log(f"using PEOPLE_REMINDERS_LIST_ID={lid}")
        return lid

    log(f"discovering list id for '{name}'...")
    all_entries = []
    for method, params in LIST_INDEX_ENDPOINTS:
        ok, resp = try_api(method, params, xoxc, xoxd)
        if not ok:
            continue
        entries = _list_entries_from_response(resp)
        log(f"    {method} returned {len(entries)} entries")
        all_entries.extend(entries)
        # paginate files.list
        if method == "files.list":
            page = 2
            while True:
                paging = resp.get("paging") or {}
                if page > (paging.get("pages") or 1):
                    break
                p2 = dict(params); p2["page"] = page
                ok2, resp = try_api(method, p2, xoxc, xoxd)
                if not ok2:
                    break
                more = _list_entries_from_response(resp)
                all_entries.extend(more)
                log(f"    {method} page {page}: +{len(more)}")
                page += 1
                if page > 20:
                    break

    # Match on title (preferred — actual display name) or name
    for e in all_entries:
        if not isinstance(e, dict):
            continue
        if name in _candidate_names(e):
            fid = e.get("id") or e.get("file_id")
            if fid:
                log(f"  -> matched '{name}': id={fid} (title={e.get('title')!r})")
                return fid

    sample = [(e.get("title"), e.get("id"))
              for e in all_entries if isinstance(e, dict)]
    log(f"  no exact match. all list titles seen: {sample}")
    raise RuntimeError(
        f"list '{name}' not found. Set PEOPLE_REMINDERS_LIST_ID=F0XXXX to fetch directly."
    )


# --- row fetching ---

# (method, id_param_name)
RECORD_ENDPOINTS = [
    ("lists.records.list", "list_id"),  # confirmed working
]

INFO_ENDPOINTS = [
    ("files.info",      "file"),       # lists are files — this is the one that works
    ("lists.info",      "file_id"),
    ("slackLists.info", "file_id"),
]


def fetch_list_info(list_id, xoxc, xoxd):
    """Best-effort fetch of list metadata (column schema)."""
    log("fetching list metadata...")
    for method, key in INFO_ENDPOINTS:
        ok, resp = try_api(method, {key: list_id}, xoxc, xoxd)
        if ok:
            return resp
    log("  list metadata unavailable")
    return None


def fetch_records(list_id, xoxc, xoxd):
    log("fetching list records...")
    last_err = None
    for method, key in RECORD_ENDPOINTS:
        rows, cursor, success = [], None, False
        page = 0
        while True:
            body = {key: list_id, "limit": 200}
            if cursor:
                body["cursor"] = cursor
            ok, resp = try_api(method, body, xoxc, xoxd)
            if not ok:
                last_err = f"{method}({key}): {resp}"
                break
            success = True
            for k in ("records", "items", "rows"):
                if isinstance(resp.get(k), list):
                    rows.extend(resp[k])
                    break
            else:
                # Got ok=True but no rows-like array; bail to next combo
                log(f"    ok response but no records/items/rows array — keys: {list(resp.keys())}")
                success = False
                last_err = f"{method}({key}): ok but no records (keys={list(resp.keys())})"
                break
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            page += 1
            if not cursor or page > 50:
                break
        if success and rows:
            log(f"  -> got {len(rows)} rows via {method}({key})")
            return rows, method, key
        if success and not rows:
            log(f"  -> {method}({key}) returned 0 rows; trying next combo")
    raise RuntimeError(f"could not fetch list rows. last_err: {last_err}")


# --- normalization ---

import re as _re

# Matches Slack message links:
#   https://*.slack.com/archives/<CID>/p<ts_no_dot>[?thread_ts=<ts>&cid=<CID>]
SLACK_LINK_RE = _re.compile(
    r"https?://[\w.-]+\.slack\.com/archives/([A-Z0-9]+)/p(\d{10})(\d{6})"
    r"(?:[^\s]*?thread_ts=([\d.]+))?",
    _re.I,
)


def parse_slack_link(text):
    """Return (channel_id, thread_ts_or_msg_ts) or None."""
    if not text:
        return None
    m = SLACK_LINK_RE.search(text)
    if not m:
        return None
    cid = m.group(1)
    ts_main = f"{m.group(2)}.{m.group(3)}"
    thread_ts = m.group(4) or ts_main
    return cid, thread_ts


def extract_rich_text(blocks):
    """Walk a list of rich_text blocks (already parsed dicts) into plain text."""
    out = []
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t in ("rich_text", "rich_text_section", "rich_text_list",
                 "rich_text_quote", "rich_text_preformatted"):
            out.append(extract_rich_text(b.get("elements") or []))
        elif t == "text":
            out.append(b.get("text", ""))
        elif t == "link":
            out.append(b.get("text") or b.get("url") or "")
        elif t == "user":
            out.append(f"<@{b.get('user_id','')}>")
        elif t == "channel":
            out.append(f"<#{b.get('channel_id','')}>")
        elif t == "emoji":
            out.append(f":{b.get('name','')}:")
        elif t == "broadcast":
            out.append(f"@{b.get('range','')}")
        elif "elements" in b:
            out.append(extract_rich_text(b["elements"]))
    return "".join(out)


def field_to_text(field):
    """Pull the best plain-text value from a record field dict."""
    if not isinstance(field, dict):
        return str(field) if field is not None else ""
    # 1. `text` is the pre-rendered plain text — preferred
    if isinstance(field.get("text"), str) and field["text"]:
        return field["text"]
    # 2. `rich_text` is structured — render it
    if isinstance(field.get("rich_text"), list):
        rendered = extract_rich_text(field["rich_text"])
        if rendered:
            return rendered
    # 3. `value` may be a JSON string of rich_text — try to parse
    v = field.get("value")
    if isinstance(v, str):
        if v.startswith("[") or v.startswith("{"):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    rendered = extract_rich_text(parsed)
                    if rendered:
                        return rendered
            except Exception:
                pass
        return v
    if isinstance(v, (int, float, bool)):
        return str(v)
    return ""


def build_column_map(info):
    """From files.info response, build column_id -> display_name."""
    if not info:
        return {}
    out = {}
    # files.info wraps it like {"file": {..., "list_metadata": {"schema": [...]}}}
    file_obj = info.get("file") if isinstance(info.get("file"), dict) else info
    for path in (
        ("list_metadata", "schema"),
        ("schema",),
        ("list_metadata", "columns"),
        ("columns",),
    ):
        cur = file_obj
        for p in path:
            if isinstance(cur, dict):
                cur = cur.get(p)
            else:
                cur = None
                break
        if isinstance(cur, list):
            for col in cur:
                if isinstance(col, dict):
                    cid = col.get("id") or col.get("key") or col.get("uuid")
                    cname = col.get("name") or col.get("title") or col.get("label")
                    if cid and cname:
                        # also include the column type if available
                        ctype = col.get("type") or col.get("kind")
                        out[cid] = {"name": cname, "type": ctype}
            if out:
                return out
    return out


# Field keys that look like assignee / user columns
USER_FIELD_HINTS = ("assign", "user", "owner", "person", "担当", "誰")
# Heuristics for slack-link columns
LINK_FIELD_HINTS = ("link", "url", "ref", "slack", "message", "thread", "メッセージ", "スレッド")


def normalize_row(row, col_map):
    """col_map is {col_id: {name, type}}."""
    fields = row.get("fields") or []
    columns = {}      # display_name -> text
    raw_by_key = {}   # internal key -> raw text (for heuristics)
    msg_ref = None
    title = None

    for f in fields:
        if not isinstance(f, dict):
            continue
        key = f.get("key") or f.get("column") or f.get("column_id") or f.get("id") or ""
        meta = col_map.get(key) if isinstance(col_map.get(key), dict) else None
        display_key = meta["name"] if meta else str(key)
        text = field_to_text(f)
        raw_by_key[key] = text

        # Title: prefer the field whose key is literally "name", or the column
        # tagged as the list's title column.
        if title is None and (key == "name" or display_key.lower() in (
                "title", "name", "task", "reminder", "item", "件名")):
            title = text

        # Look for a slack message link in the text
        if msg_ref is None:
            ref = parse_slack_link(text)
            if ref:
                msg_ref = ref

        if text:
            columns[display_key] = text

    if title is None:
        title = next((v for v in columns.values() if v), "")

    return {
        "row_id": row.get("id") or row.get("row_id") or "",
        "title": (title or "")[:500],
        "columns": columns,
        "msg_ref": msg_ref,
        "created_by": row.get("created_by", ""),
        "date_created": row.get("date_created", 0),
    }


# --- thread fetching ---

def fetch_thread(channel, ts, xoxc, xoxd, limit=50):
    try:
        r = api("conversations.replies", {
            "channel": channel, "ts": ts, "limit": limit,
        }, xoxc, xoxd, form=True)
    except Exception as e:
        return [], str(e)
    if not r.get("ok"):
        return [], r.get("error", "unknown")
    msgs = []
    for m in r.get("messages", []):
        ts_s = m.get("ts", "")
        when = ""
        try:
            when = datetime.fromtimestamp(float(ts_s)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
        msgs.append({
            "user": m.get("user", ""),
            "text": (m.get("text") or "")[:1000],
            "ts": ts_s,
            "when": when,
        })
    return msgs, None


def resolve_users(user_ids, xoxc, xoxd):
    out = {}
    def one(uid):
        if not uid:
            return uid, None
        try:
            r = api("users.info", {"user": uid}, xoxc, xoxd, form=True)
        except Exception:
            return uid, None
        if r.get("ok"):
            u = r.get("user", {})
            prof = u.get("profile", {})
            return uid, (prof.get("display_name")
                         or prof.get("real_name")
                         or u.get("name") or uid)
        return uid, None
    with ThreadPoolExecutor(max_workers=8) as ex:
        for uid, name in ex.map(one, user_ids):
            if name:
                out[uid] = name
    return out


# Internal/redundant column keys to suppress in UI output
HIDDEN_COL_KEYS = {"name", "message"}
HIDDEN_COL_PATTERNS = ("Col09AAR", "Col09BFE", "Col09F", "Col09BF")  # created/edited times


def is_hidden_col(key, col_map):
    if key in HIDDEN_COL_KEYS:
        return True
    meta = col_map.get(key) if isinstance(col_map.get(key), dict) else None
    if meta and meta.get("type") in ("created_time", "last_edited_time"):
        return True
    if any(key.startswith(p) for p in HIDDEN_COL_PATTERNS):
        return True
    return False


def substitute_mentions(text, name_map):
    """Replace <@UXXX> with @display_name and clean <URL|label> markup."""
    if not text:
        return text

    def user_repl(m):
        uid = m.group(1)
        return f"@{name_map.get(uid, uid)}"
    text = _re.sub(r"<@(U[A-Z0-9]+)>", user_repl, text)

    # <url|label> -> label (url)  ; <url> -> url
    def url_repl(m):
        url = m.group(1)
        label = m.group(2)
        if label and label != url:
            return f"{label} ({url})"
        return url
    text = _re.sub(r"<(https?://[^|>]+)(?:\|([^>]+))?>", url_repl, text)

    # &amp; etc — keep simple
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return text


# --- main ---

def delete_record(list_id, record_id, xoxc, xoxd):
    """Delete a single row from the Slack list. Returns (ok, error_str)."""
    try:
        r = api("lists.records.delete",
                {"list_id": list_id, "id": record_id},
                xoxc, xoxd, form=True)
    except Exception as e:
        return False, str(e)
    if r.get("ok"):
        return True, None
    return False, r.get("error") or json.dumps(r)[:200]


def main():
    list_name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LIST_NAME

    ensure_slack_cdp()
    xoxc, xoxd = get_tokens()

    list_id = find_list_id(list_name, xoxc, xoxd)
    log(f"list_id = {list_id}")

    info = fetch_list_info(list_id, xoxc, xoxd)
    col_map = build_column_map(info)
    if col_map:
        log(f"column map: {col_map}")

    rows_raw, used_method, used_key = fetch_records(list_id, xoxc, xoxd)
    log(f"got {len(rows_raw)} rows via {used_method}({used_key})")

    if DEBUG and rows_raw:
        dbg(f"first row raw: {json.dumps(rows_raw[0], ensure_ascii=False)[:800]}")

    rows = [normalize_row(r, col_map) for r in rows_raw]

    def with_thread(r):
        thread, err = [], None
        if r["msg_ref"]:
            cid, ts = r["msg_ref"]
            thread, err = fetch_thread(cid, ts, xoxc, xoxd)
            ts_frag = ts.replace(".", "")
            r["link"] = f"https://{TEAM_DOMAIN}.slack.com/archives/{cid}/p{ts_frag}"
            r["channel"] = cid
            r["thread_ts"] = ts
        else:
            r["link"] = ""
            r["channel"] = ""
            r["thread_ts"] = ""
        r["thread"] = thread
        r["thread_error"] = err
        return r

    with ThreadPoolExecutor(max_workers=6) as ex:
        rows = list(ex.map(with_thread, rows))

    # Collect all user ids we need to resolve: thread participants + reporter
    # columns + mention strings within titles/messages.
    uids = set()
    for r in rows:
        for m in r["thread"]:
            if m.get("user"):
                uids.add(m["user"])
        for v in (r.get("columns") or {}).values():
            uids.update(_re.findall(r"<@(U[A-Z0-9]+)>", v or ""))
            # bare user ids are stored in 'reporter' / user columns
            if v and _re.fullmatch(r"U[A-Z0-9]+", v):
                uids.add(v)
        uids.update(_re.findall(r"<@(U[A-Z0-9]+)>", r.get("title") or ""))

    if uids:
        log(f"resolving {len(uids)} users")
        names = resolve_users(uids, xoxc, xoxd)
    else:
        names = {}

    # Post-process: substitute mentions, resolve bare user_ids, hide redundant
    # columns, drop internal fields.
    for r in rows:
        r["title"] = substitute_mentions(r.get("title", ""), names)
        cleaned = {}
        for k, v in (r.get("columns") or {}).items():
            if is_hidden_col(k, col_map):
                continue
            if v and _re.fullmatch(r"U[A-Z0-9]+", v):
                cleaned[k] = f"@{names.get(v, v)}"
            else:
                cleaned[k] = substitute_mentions(v, names)
        r["columns"] = cleaned
        for m in r["thread"]:
            if m.get("user") and m["user"] in names:
                m["user_name"] = names[m["user"]]
            m["text"] = substitute_mentions(m.get("text", ""), names)
        r.pop("msg_ref", None)

    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
