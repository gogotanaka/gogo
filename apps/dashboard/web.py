#!/usr/bin/env python3
"""Tiny web UI for apps/dashboard.

Shows Later / Overdue / Activity / Drafts counts for every Slack workspace
the user is signed into, plus an unread @-mentions list per workspace.

Listens on http://localhost:8380 and opens the browser on startup.
"""
import html
import json
import re
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_counts  # noqa: E402

PORT = 8380


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
    fetched at {ts} &nbsp;·&nbsp; {n} workspace(s) &nbsp;·&nbsp;
    <span class="clearcount">{clear}</span> &nbsp;·&nbsp;
    <button onclick="location.reload()">reload</button>
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


def render(rows):
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
    return PAGE.format(
        ts=time.strftime("%Y-%m-%d %H:%M:%S"),
        n=len(rows),
        clear=html.escape(clear_label),
        rows=body,
        mentions=mentions_html,
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[dashboard] " + (fmt % args) + "\n")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                rows = fetch_counts.collect()
                page = render(rows)
            except Exception as e:
                page = (f"<!doctype html><body><h1>error</h1>"
                        f"<pre>{html.escape(str(e))}</pre></body>")
            data = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/api/counts.json":
            try:
                rows = fetch_counts.collect()
                data = json.dumps(rows, ensure_ascii=False).encode("utf-8")
                code = 200
            except Exception as e:
                data = json.dumps({"error": str(e)}).encode("utf-8")
                code = 500
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()


def main():
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"[dashboard] listening on {url}", file=sys.stderr)
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
