#!/usr/bin/env python3
"""Tiny web UI for apps/dashboard.

Shows Later / Overdue / Activity / Drafts counts for every Slack workspace
the user is signed into, plus an unread @-mentions list per workspace.

Listens on http://localhost:8380 and opens the browser on startup.
"""
import html
import json
import os
import re
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_counts  # noqa: E402

PORT = 8380
CACHE_FILE = Path(__file__).resolve().parent / "slack_cache.json"
# NOTE: equal to the page's <meta http-equiv="refresh"> interval, so the
# automatic reload always refetches live data; the cache only absorbs manual
# reloads / extra tabs / API polls inside the window.
CACHE_TTL = 600  # 10 minutes


def _load_cache():
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        rows, fetched_at = data["rows"], data["fetched_at"]
        if not isinstance(rows, list) or not isinstance(fetched_at, (int, float)):
            return None, None
        if not 0 <= time.time() - fetched_at <= CACHE_TTL:
            return None, None
        return rows, fetched_at
    except Exception:
        return None, None


def _save_cache(rows):
    """Best-effort: a cache-write failure must not discard a good fetch."""
    fetched_at = time.time()
    try:
        tmp = CACHE_FILE.with_name(CACHE_FILE.name + ".tmp")
        tmp.write_text(
            json.dumps({"fetched_at": fetched_at, "rows": rows},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, CACHE_FILE)  # atomic: readers never see a torn file
    except OSError as e:
        sys.stderr.write(f"[dashboard] cache write failed: {e}\n")
    return fetched_at


_fetch_lock = threading.Lock()


def get_rows(force=False):
    """Return (rows, fetched_at, from_cache). Thread-safe.

    force=True skips the cache and always fetches; the old cache file is
    left in place until the new fetch succeeds, so a failed force-sync
    doesn't destroy the last known good snapshot.
    """
    if not force:
        rows, fetched_at = _load_cache()
        if rows is not None:
            return rows, fetched_at, True
    with _fetch_lock:
        if not force:
            # Re-check after acquiring lock — another thread may have just
            # fetched.
            rows, fetched_at = _load_cache()
            if rows is not None:
                return rows, fetched_at, True
        rows = fetch_counts.collect()
        if rows and not all(r.get("error") for r in rows):
            fetched_at = _save_cache(rows)
        else:
            # Don't pin an empty / all-failed snapshot for CACHE_TTL; the
            # next plain reload should retry.
            fetched_at = time.time()
    return rows, fetched_at, False


PAGE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="600">
<title>Slack dashboard</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue",
         sans-serif; margin: 32px; color: #222; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .meta {{ color: #888; font-size: 12px; margin-bottom: 20px; }}
  .cols {{ display: flex; gap: 32px; align-items: flex-start;
           flex-wrap: wrap; }}
  .col-left  {{ flex: 0 1 720px; min-width: 0; }}
  .col-right {{ flex: 1 1 380px; min-width: 0; }}
  .col-right h2 {{ margin-top: 0; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: 10px 14px;
           border-bottom: 1px solid #eee; }}
  th {{ background: #fafafa; font-weight: 600; font-size: 13px;
        color: #555; }}
  td.num {{ font-variant-numeric: tabular-nums; font-size: 18px;
            font-weight: 500; text-align: right; }}
  td.zero {{ color: #bbb; }}
  td.hot  {{ color: #c43; }}
  td.good {{ color: #16a34a !important; }}
  tr.clear {{ background: linear-gradient(90deg,#ecfdf5 0%,#ffffff 70%); }}
  tr.clear td {{ border-bottom-color: #d1fae5; }}
  tr.clear td.num {{ color: #16a34a !important; }}
  tr.clear .badge {{ display: inline-block; margin-left: 8px;
                    background: #16a34a; color: #fff; font-weight: 600;
                    font-size: 11px; padding: 2px 8px; border-radius: 10px;
                    letter-spacing: .04em; }}
  tr.total td {{ border-bottom: 2px solid #ddd;
                 padding-bottom: 14px; font-weight: 600; color: #444; }}
  tr.total td.num {{ font-size: 20px; }}
  td.overdue {{ color: #c43; }}
  td.overdue.zero {{ color: #bbb; }}
  .clearcount {{ color: #16a34a; font-weight: 600; }}
  .ws {{ font-weight: 600; }}
  .ws small {{ font-weight: 400; color: #999; margin-left: 6px; }}
  .err {{ color: #c43; font-size: 12px; }}
  .via {{ color: #aaa; font-size: 11px; }}
  button {{ font-size: 13px; padding: 6px 12px; border: 1px solid #ddd;
           background: #fff; border-radius: 4px; cursor: pointer; }}
  button:hover {{ background: #f5f5f5; }}

  h2 {{ font-size: 16px; margin: 0 0 10px; color: #333; }}
  .mentions {{ }}
  .ws-block {{ margin-bottom: 22px; }}
  .ws-block h3 {{ font-size: 14px; margin: 0 0 8px; color: #555;
                 font-weight: 600; }}
  .ws-block h3 .count {{ background: #eef; color: #335; font-size: 11px;
                        font-weight: 600; padding: 1px 8px; border-radius: 10px;
                        margin-left: 6px; }}
  .ws-block h3 .more  {{ color: #999; font-size: 11px; margin-left: 6px; }}
  .m-item {{ border: 1px solid #eee; border-radius: 6px; padding: 10px 14px;
            margin-bottom: 6px; background: #fff; }}
  .m-head {{ font-size: 12px; color: #777; margin-bottom: 4px; }}
  .m-head .ch {{ color: #335; font-weight: 600; }}
  .m-head .user {{ color: #555; margin-left: 8px; }}
  .m-head .when {{ color: #aaa; margin-left: 8px; }}
  .m-head a {{ color: inherit; text-decoration: none; }}
  .m-head a:hover {{ text-decoration: underline; }}
  .m-text {{ font-size: 13px; color: #222; white-space: pre-wrap;
            word-break: break-word; line-height: 1.5; }}
  .m-empty {{ color: #aaa; font-size: 12px; }}
</style>
</head>
<body>
  <h1>Slack dashboard</h1>
  <div class="meta">
    fetched at {ts}{cache_badge} &nbsp;·&nbsp; {n} workspace(s) &nbsp;·&nbsp;
    <span class="clearcount">{clear}</span> &nbsp;·&nbsp;
    <button onclick="location.reload()">reload</button>
    <form method="post" action="/refresh" style="display:inline">
      <button>&#8635; force sync</button>
    </form>
  </div>
  <div class="cols">
    <div class="col-left">
      <table>
        <thead><tr>
          <th>Workspace</th>
          <th style="text-align:right">Later</th>
          <th style="text-align:right">Overdue</th>
          <th style="text-align:right">Activity</th>
          <th style="text-align:right">Drafts</th>
        </tr></thead>
        <tbody>
          {rows}
        </tbody>
      </table>
    </div>
    <div class="col-right">
      <h2>@ unread mentions</h2>
      <div class="mentions">
        {mentions}
      </div>
    </div>
  </div>
</body>
</html>
"""


# Per-workspace Later threshold used for the ✓ CLEAR badge. Identified by
# the workspace `domain` field from localConfig_v2.teams.
LATER_CLEAR_THRESHOLD = {
    "aisaac": 10,
    "entm-inc": 10,
    "riseltd": 10,
    "a2zltd": 10,
    "xtoon": 10,
    "sushiconsulting": 10,
    "awsm-inc": 10,
}
DEFAULT_LATER_CLEAR_THRESHOLD = 3


def later_threshold(row):
    return LATER_CLEAR_THRESHOLD.get(
        row.get("domain"), DEFAULT_LATER_CLEAR_THRESHOLD)


def cell(v, via, good=False):
    if v is None:
        return '<td class="num err">—</td>'
    if good:
        cls = "num good"
    elif v == 0:
        cls = "num zero"
    elif v >= 10:
        cls = "num hot"
    else:
        cls = "num"
    via_html = (f'<div class="via">via {html.escape(via)}</div>'
                if via and via not in ("saved.list",) else "")
    return f'<td class="{cls}">{v}{via_html}</td>'


def overdue_cell(v):
    if v is None:
        return '<td class="num err">—</td>'
    cls = "num overdue good" if v == 0 else "num overdue"
    return f'<td class="{cls}">{v}</td>'


def total_cell(v, good=False):
    if good:
        cls = "num good"
    elif v == 0:
        cls = "num zero"
    elif v >= 10:
        cls = "num hot"
    else:
        cls = "num"
    return f'<td class="{cls}">{v}</td>'


USER_MENTION_RE = re.compile(r"<@\ue000?([A-Z0-9]+)\ue001?(?:\|([^>]+))?>")
LINK_RE = re.compile(r"<(https?://[^>|]+)(?:\|([^>]+))?>")
CHANNEL_RE = re.compile(r"<#[A-Z0-9]+(?:\|([^>]+))?>")


def humanize_text(s, max_len=240):
    """Cheap Slack mrkdwn -> plain text for inline preview."""
    if not s:
        return ""
    s = USER_MENTION_RE.sub(lambda m: "@" + (m.group(2) or m.group(1)), s)
    s = CHANNEL_RE.sub(lambda m: "#" + (m.group(1) or "channel"), s)
    s = LINK_RE.sub(lambda m: m.group(2) or m.group(1), s)
    if len(s) > max_len:
        s = s[:max_len] + "…"
    return s


def fmt_when(ts):
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except Exception:
        return ""


def render_mentions(rows):
    blocks = []
    any_unread = False
    for r in rows:
        ms = r.get("mentions") or []
        if not ms:
            continue
        any_unread = True
        ws = html.escape(r.get("name") or r.get("team_id") or "?")
        total = r.get("mentions_total") or len(ms)
        more = (f'<span class="more">+ {total - len(ms)} more</span>'
                if total > len(ms) else "")
        items = []
        for m in ms:
            ch_name = m.get("channel_name") or m.get("channel_id") or "?"
            ch_prefix = "@" if m.get("channel_is_im") else "#"
            ch_label = ch_prefix + ch_name
            user = m.get("username") or m.get("user") or ""
            link = m.get("permalink") or "#"
            when = fmt_when(m.get("ts"))
            text = html.escape(humanize_text(m.get("text") or ""))
            items.append(
                f'<div class="m-item">'
                f'<div class="m-head">'
                f'<a href="{html.escape(link)}" target="_blank" rel="noopener">'
                f'<span class="ch">{html.escape(ch_label)}</span>'
                f'<span class="user">@{html.escape(user)}</span>'
                f'<span class="when">{html.escape(when)}</span>'
                f'</a></div>'
                f'<div class="m-text">{text}</div>'
                f'</div>'
            )
        blocks.append(
            f'<div class="ws-block">'
            f'<h3>{ws} <span class="count">{total}</span>{more}</h3>'
            f'{"".join(items)}'
            f'</div>'
        )
    if not any_unread:
        return '<div class="m-empty">未読メンション無し ✓</div>'
    return "\n".join(blocks)


def render(rows, fetched_at=None, from_cache=False):
    clear_n = 0
    if not rows:
        body = '<tr><td colspan="5">No workspaces found in localConfig_v2.</td></tr>'
        clear_label = ""
        mentions_html = ""
    else:
        row_parts = []
        totals = {"later": 0, "later_overdue": 0,
                  "activity": 0, "drafts": 0}
        for r in rows:
            name = html.escape(r.get("name") or r.get("team_id") or "?")
            domain = html.escape(r.get("domain") or "")
            err = r.get("error")
            err_html = (f'<div class="err">{html.escape(err)}</div>'
                        if err else "")
            for k in totals:
                v = r.get(k)
                if isinstance(v, int):
                    totals[k] += v
            th = later_threshold(r)
            later = r.get("later")
            is_clear = (
                later is not None and later <= th
                and r.get("later_overdue") == 0
                and r.get("activity") == 0
                and r.get("drafts") == 0
            )
            row_cls = ' class="clear"' if is_clear else ""
            badge_label = (
                f'✓ CLEAR' if later == 0
                else f'✓ ≤{th}'
            ) if is_clear else ""
            badge = (f'<span class="badge">{badge_label}</span>'
                     if is_clear else "")
            if is_clear:
                clear_n += 1
            later_good = later is not None and later <= th
            row_parts.append(
                f'<tr{row_cls}><td class="ws">{name}'
                f'<small>{domain}</small>{badge}{err_html}</td>'
                f'{cell(later, r.get("later_via"), good=later_good)}'
                f'{overdue_cell(r.get("later_overdue"))}'
                f'{cell(r.get("activity"), r.get("activity_via"), good=r.get("activity") == 0)}'
                f'{cell(r.get("drafts"), r.get("drafts_via"), good=r.get("drafts") == 0)}'
                f'</tr>'
            )
        total_all_clear = all(totals[k] == 0 for k in totals)
        total_cls = "total clear" if total_all_clear else "total"
        total_row = (
            f'<tr class="{total_cls}"><td>合計</td>'
            f'{total_cell(totals["later"], good=totals["later"] == 0)}'
            f'{total_cell(totals["later_overdue"], good=totals["later_overdue"] == 0)}'
            f'{total_cell(totals["activity"], good=totals["activity"] == 0)}'
            f'{total_cell(totals["drafts"], good=totals["drafts"] == 0)}'
            f'</tr>'
        )
        body = "\n".join([total_row] + row_parts)
        if clear_n == len(rows):
            clear_label = f"✓ ALL {len(rows)} CLEAR — お疲れ様!!"
        elif clear_n > 0:
            clear_label = f"✓ {clear_n} / {len(rows)} clear"
        else:
            clear_label = f"0 / {len(rows)} clear — 頑張ろう"
        mentions_html = render_mentions(rows)
    ts_str = datetime.fromtimestamp(
        fetched_at or time.time()).strftime("%Y-%m-%d %H:%M:%S")
    if from_cache:
        age_s = int(time.time() - fetched_at)
        label, bg, fg, bd = f"cached {age_s}s ago", "#f0f9ff", "#0369a1", "#bae6fd"
    else:
        label, bg, fg, bd = "live", "#f0fdf4", "#15803d", "#bbf7d0"
    cache_badge = (f' &nbsp;<span style="background:{bg};color:{fg};'
                   f'font-size:11px;padding:1px 8px;border-radius:10px;'
                   f'border:1px solid {bd}">{label}</span>')
    return PAGE.format(
        ts=ts_str,
        cache_badge=cache_badge,
        n=len(rows),
        clear=html.escape(clear_label),
        rows=body,
        mentions=mentions_html,
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[dashboard] " + (fmt % args) + "\n")

    def _send(self, code, ctype, data):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # The body embeds its own freshness (live / cached Ns ago); a
        # browser- or bfcache-replayed copy would lie about it.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_page(self):
        try:
            rows, fetched_at, from_cache = get_rows()
            page = render(rows, fetched_at, from_cache)
            code = 200
        except Exception as e:
            page = (f"<!doctype html><body><h1>error</h1>"
                    f"<pre>{html.escape(str(e))}</pre></body>")
            code = 500
        self._send(code, "text/html; charset=utf-8", page.encode("utf-8"))

    def _redirect_home(self):
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def do_POST(self):
        if self.path.split("?", 1)[0] == "/refresh":
            try:
                get_rows(force=True)
            except Exception as e:
                # Fall through to the redirect: "/" serves the error (or the
                # still-intact last good cache) without parking the browser
                # on the side-effecting /refresh URL.
                sys.stderr.write(f"[dashboard] force sync failed: {e}\n")
            self._redirect_home()
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send_page()
        elif path == "/refresh":
            # Force sync is POST-only (GETs can be issued speculatively by
            # browsers/link previews and must stay side-effect-free); a
            # stray GET here just goes home.
            self._redirect_home()
        elif path == "/api/counts.json":
            try:
                rows, fetched_at, from_cache = get_rows()
                data = json.dumps(
                    {"fetched_at": fetched_at, "from_cache": from_cache, "rows": rows},
                    ensure_ascii=False,
                ).encode("utf-8")
                code = 200
            except Exception as e:
                data = json.dumps({"error": str(e)}).encode("utf-8")
                code = 500
            self._send(code, "application/json; charset=utf-8", data)
        else:
            self.send_response(404)
            self.end_headers()


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"[dashboard] listening on {url}", file=sys.stderr)
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
