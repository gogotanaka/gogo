#!/usr/bin/env python3
"""Serve the Slack 'people-reminders' list as a web page.

- Loads list rows + threads via fetch_list.py
- Optionally asks Claude (judge_done.py) to label each row as done / open / unclear
- Renders at http://localhost:8379

Query params:
  ?show=open|done|unclear|all   (default: open+unclear)
  ?refresh=1                    force re-fetch
  ?nojudge=1                    skip the Claude judgement (fast)

Usage: python3 web.py
"""
import html as html_lib
import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from collections import defaultdict
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# Import fetch_list as a module so we can reuse get_tokens / delete_record.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import fetch_list as fl  # noqa: E402

PORT = 8379
FETCH_LIST = os.path.join(SCRIPT_DIR, "fetch_list.py")
JUDGE = os.path.join(SCRIPT_DIR, "judge_done.py")
LIST_NAME = os.environ.get("PEOPLE_REMINDERS_LIST", "people-reminders")
TEAM_DOMAIN = "aisaac"

CACHE = {"rows": None, "error": None, "judged": False, "list_id": None}
LOCK = threading.Lock()

GUESS_LABEL = {
    "done": ("✅ 終わってそう", "good"),
    "open": ("⏳ まだっぽい", "open"),
    "unclear": ("❓ 判断つかず", "unclear"),
}


def _resolve_list_id():
    """Cache the Slack list_id so the delete endpoint can use it."""
    if CACHE["list_id"]:
        return CACHE["list_id"]
    fl.ensure_slack_cdp()
    xoxc, xoxd = fl.get_tokens()
    CACHE["list_id"] = fl.find_list_id(LIST_NAME, xoxc, xoxd)
    return CACHE["list_id"]


def fetch(force=False, judge=True):
    # Cheap path: cache hit, no lock needed
    if CACHE["rows"] is not None and not force and (CACHE["judged"] or not judge):
        return CACHE["rows"], CACHE["error"]
    # Slow path: serialize so concurrent refreshes don't both spawn claude
    with LOCK:
        if CACHE["rows"] is not None and not force and (CACHE["judged"] or not judge):
            return CACHE["rows"], CACHE["error"]
        return _do_fetch(force=force, judge=judge)


def _do_fetch(force, judge):
    try:
        r = subprocess.run(
            [sys.executable, FETCH_LIST, LIST_NAME],
            capture_output=True, text=True, timeout=180,
        )
        if r.returncode != 0:
            err = (r.stderr or "").strip().splitlines()[-1] if r.stderr else "non-zero exit"
            CACHE["rows"], CACHE["error"], CACHE["judged"] = [], err, False
            return CACHE["rows"], CACHE["error"]
        rows = json.loads(r.stdout)
    except Exception as e:
        CACHE["rows"], CACHE["error"], CACHE["judged"] = [], str(e), False
        return CACHE["rows"], CACHE["error"]

    if judge and rows:
        try:
            j = subprocess.run(
                [sys.executable, JUDGE],
                input=json.dumps(rows, ensure_ascii=False),
                capture_output=True, text=True, timeout=240,
            )
            if j.returncode == 0:
                rows = json.loads(j.stdout)
                CACHE["judged"] = True
            else:
                print(f"[web] judge failed: {j.stderr[:200]}", file=sys.stderr)
        except Exception as e:
            print(f"[web] judge exception: {e}", file=sys.stderr)

    CACHE["rows"], CACHE["error"] = rows, None
    return rows, None


def delete_row(row_id):
    """Delete a row from the Slack list and drop it from the cache. Thread-safe."""
    with LOCK:
        list_id = _resolve_list_id()
        xoxc, xoxd = fl.get_tokens()
        ok, err = fl.delete_record(list_id, row_id, xoxc, xoxd)
        if ok and CACHE["rows"] is not None:
            CACHE["rows"] = [r for r in CACHE["rows"] if r.get("row_id") != row_id]
        return ok, err


# --- text rendering helpers ---

URL_RE = re.compile(r"(https?://[^\s<>\"]+)")


def linkify(text):
    """Escape HTML, then turn bare URLs into clickable anchors.
    Slack archives URLs get a friendlier label.
    """
    if not text:
        return ""
    escaped = html_lib.escape(text)

    def repl(m):
        url = m.group(1)
        # show Slack message links as a short label
        if "slack.com/archives/" in url:
            label = "Slackメッセージ↗"
        elif len(url) > 60:
            label = url[:55] + "…"
        else:
            label = url
        return (f'<a href="{url}" target="_blank" rel="noopener" '
                f'class="ext">{html_lib.escape(label)}</a>')

    return URL_RE.sub(repl, escaped).replace("\n", "<br>")


# --- rendering ---

def render_thread(thread, evidence_ts_set):
    if not thread:
        return '<div class="thread empty">スレッドなし</div>'
    parts = []
    for m in thread:
        who = html_lib.escape(m.get("user_name") or m.get("user") or "?")
        when = html_lib.escape(m.get("when") or "")
        is_evidence = m.get("ts") in evidence_ts_set
        cls = "msg evidence" if is_evidence else "msg"
        marker = ' <span class="ev-mark" title="判定根拠">★</span>' if is_evidence else ""
        parts.append(
            f'<div class="{cls}"><div class="msg-head"><span class="who">{who}</span>'
            f'<span class="when">{when}</span>{marker}</div>'
            f'<div class="msg-body">{linkify(m.get("text") or "")}</div></div>'
        )
    return f'<div class="thread">{"".join(parts)}</div>'


def render_columns(cols):
    if not cols:
        return ""
    parts = []
    for k, v in cols.items():
        parts.append(
            f'<span class="col"><span class="col-k">{html_lib.escape(k)}:</span> '
            f'{linkify(v)}</span>'
        )
    return f'<div class="cols">{"".join(parts)}</div>'


def evidence_link(channel, thread_ts, evi_ts):
    """Build a deep-link URL to a specific reply within a thread."""
    if not channel or not evi_ts:
        return ""
    p = evi_ts.replace(".", "")
    url = f"https://{TEAM_DOMAIN}.slack.com/archives/{channel}/p{p}"
    if thread_ts and thread_ts != evi_ts:
        url += f"?thread_ts={thread_ts}&cid={channel}"
    return url


def render_msg_block(msg, label, cls=""):
    """Render a single Slack message with speaker + time in a labeled box."""
    if not msg:
        return ""
    who = html_lib.escape(msg.get("user_name") or msg.get("user") or "?")
    when = html_lib.escape(msg.get("when") or "")
    text = linkify(msg.get("text") or "")
    return f'''
      <div class="src-block {cls}">
        <div class="src-label">{label}</div>
        <div class="msg"><div class="msg-head"><span class="who">{who}</span>
          <span class="when">{when}</span></div>
          <div class="msg-body">{text}</div></div>
      </div>'''


def render_evidence_block(row):
    """Render each evidence message in its own box with a deep link."""
    evidence = row.get("evidence_ts") or []
    if not evidence:
        return ""
    channel = row.get("channel") or ""
    thread_ts = row.get("thread_ts") or ""
    by_ts = {m.get("ts"): m for m in (row.get("thread") or [])}
    blocks = []
    for ts in evidence:
        msg = by_ts.get(ts)
        if not msg:
            continue
        who = html_lib.escape(msg.get("user_name") or msg.get("user") or "?")
        when = html_lib.escape(msg.get("when") or "")
        text = linkify(msg.get("text") or "")
        url = evidence_link(channel, thread_ts, ts)
        link_html = (
            f'<a class="ev-jump" href="{url}" target="_blank" rel="noopener">'
            f'Slackで開く ↗</a>' if url else ""
        )
        blocks.append(f'''
        <div class="msg evidence"><div class="msg-head">
          <span class="who">{who}</span>
          <span class="when">{when}</span>
          {link_html}
        </div>
        <div class="msg-body">{text}</div></div>''')
    if not blocks:
        return ""
    return f'''
      <div class="src-block evidence-block">
        <div class="src-label">終わった理由の生文章</div>
        {"".join(blocks)}
      </div>'''


def render_card(row):
    guess = row.get("done_guess", "unclear")
    label, cls = GUESS_LABEL.get(guess, GUESS_LABEL["unclear"])
    summary = row.get("summary") or row.get("title") or "(no title)"
    reason = row.get("done_reason") or ""
    link = row.get("link") or ""
    link_html = (
        f'<a class="open-slack" href="{html_lib.escape(link)}" '
        f'target="_blank" rel="noopener">Slackで開く &rarr;</a>'
        if link else ""
    )
    row_id = html_lib.escape(row.get("row_id") or "")
    evidence_ts_set = set(row.get("evidence_ts") or [])
    delete_btn = (
        f'<button class="del-btn" data-row="{row_id}" '
        f'onclick="confirmDelete(this)">本当に終わってる ✓</button>'
        if row_id else ""
    )
    thread = row.get("thread") or []
    first_msg = thread[0] if thread else None

    reason_html = (
        f'<div class="section reason-section">'
        f'<div class="section-label">終わった理由</div>'
        f'<div class="section-body">{html_lib.escape(reason)}</div>'
        f'</div>'
    ) if reason else ""

    return f'''
    <div class="card {cls}" data-row="{row_id}">
      <div class="card-top">
        <span class="badge-guess {cls}">{label}</span>
        <div class="summary">{linkify(summary)}</div>
      </div>
      {reason_html}
      {render_msg_block(first_msg, "TODOの生文章", "todo-block")}
      {render_evidence_block(row)}
      {render_columns(row.get("columns") or {})}
      <details><summary>全スレッド ({len(thread)})</summary>
        {render_thread(thread, evidence_ts_set)}
      </details>
      <div class="actions">
        {link_html}
        {delete_btn}
      </div>
    </div>'''


def generate_html(rows, error, show, judged):
    if show == "all":
        visible = rows
    else:
        wanted = set(show.split(",")) if show else {"open", "unclear"}
        visible = [r for r in rows if r.get("done_guess", "unclear") in wanted]

    # counts over ALL rows (not filtered) so the header is honest
    all_counts = defaultdict(int)
    for r in rows:
        all_counts[r.get("done_guess", "unclear")] += 1

    groups = defaultdict(list)
    for r in visible:
        groups[r.get("done_guess", "unclear")].append(r)

    order = ["open", "unclear", "done"]
    section_html = []
    for g in order:
        items = groups.get(g, [])
        if not items:
            continue
        label, cls = GUESS_LABEL[g]
        cards = "".join(render_card(r) for r in items)
        section_html.append(f'''
    <section class="group">
      <h2 class="group-title {cls}">{label} <span class="count">{len(items)}</span></h2>
      <div class="cards">{cards}</div>
    </section>''')

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    meta = (
        f'全 {len(rows)} 件 · '
        f'open {all_counts["open"]} / unclear {all_counts["unclear"]} / done {all_counts["done"]} · '
        f'{"判定済" if judged else "未判定"} · {now} 更新'
    )
    if error:
        meta = f'<span class="err">取得失敗: {html_lib.escape(error)}</span>'

    body_html = "".join(section_html) or '<p class="empty">表示対象なし。</p>'

    controls = []
    for label_, q in [
        ("未完了+未判定", ""),
        ("全部", "?show=all"),
        ("未完了のみ", "?show=open"),
        ("完了のみ", "?show=done"),
        ("再取得", "?refresh=1"),
        ("再取得(judge無し)", "?refresh=1&nojudge=1"),
    ]:
        controls.append(f'<a href="/{q}">{label_}</a>')

    return f'''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>people-reminders</title>
<style>
  :root {{
    --bg:#0f0f0f; --surface:#1a1a1a; --surface2:#242424;
    --border:#333; --text:#e0e0e0; --text2:#999;
    --accent:#4a9eff; --good:#22c55e; --open:#f59e0b; --unclear:#a3a3a3; --red:#ef4444;
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    background:var(--bg); color:var(--text); line-height:1.55;
    padding:24px; max-width:960px; margin:0 auto;
  }}
  header {{ margin-bottom:20px; padding-bottom:16px; border-bottom:1px solid var(--border); }}
  h1 {{ font-size:24px; font-weight:600; margin-bottom:4px; }}
  .meta {{ color:var(--text2); font-size:13px; }}
  .err {{ color:var(--red); }}
  .controls {{ display:flex; gap:8px; margin:16px 0 24px; flex-wrap:wrap; }}
  .controls a {{
    padding:6px 14px; border:1px solid var(--border); border-radius:6px;
    background:var(--surface); color:var(--text); text-decoration:none; font-size:13px;
  }}
  .controls a:hover {{ background:var(--surface2); border-color:var(--accent); }}
  .group {{ margin-bottom:28px; }}
  .group-title {{
    font-size:18px; font-weight:700; margin-bottom:10px;
    padding:6px 12px; border-radius:6px; display:inline-flex; align-items:baseline; gap:10px;
  }}
  .group-title.good {{ background:rgba(34,197,94,0.12); color:var(--good); }}
  .group-title.open {{ background:rgba(245,158,11,0.12); color:var(--open); }}
  .group-title.unclear {{ background:rgba(163,163,163,0.12); color:var(--unclear); }}
  .count {{ font-size:13px; color:var(--text2); font-weight:500; }}
  .cards {{ display:flex; flex-direction:column; gap:10px; }}
  .card {{
    background:var(--surface); border:1px solid var(--border); border-radius:8px;
    padding:14px 16px;
  }}
  .card.good {{ border-left:3px solid var(--good); }}
  .card.open {{ border-left:3px solid var(--open); }}
  .card.unclear {{ border-left:3px solid var(--unclear); }}
  .card-top {{ display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }}
  .title {{ font-weight:600; font-size:15px; }}
  .badge-guess {{ font-size:12px; padding:2px 8px; border-radius:10px; }}
  .badge-guess.good {{ background:rgba(34,197,94,0.15); color:var(--good); }}
  .badge-guess.open {{ background:rgba(245,158,11,0.15); color:var(--open); }}
  .badge-guess.unclear {{ background:rgba(163,163,163,0.15); color:var(--unclear); }}
  .reason {{ margin-top:6px; font-size:13px; color:var(--text2); }}
  .summary {{ font-weight:600; font-size:15px; flex:1; min-width:0; }}
  .section {{ margin-top:10px; }}
  .section-label {{
    font-size:11px; font-weight:600; color:var(--text2);
    text-transform:uppercase; letter-spacing:0.05em; margin-bottom:4px;
  }}
  .section-body {{ font-size:13px; color:var(--text); }}
  .reason-section .section-body {{ color:var(--text2); font-style:italic; }}
  .src-block {{ margin-top:10px; }}
  .src-label {{
    font-size:11px; font-weight:600; color:var(--text2);
    text-transform:uppercase; letter-spacing:0.05em; margin-bottom:4px;
  }}
  .src-block .msg {{
    background:var(--surface2); border-radius:6px; padding:8px 10px;
    border:1px solid var(--border); margin-bottom:6px;
  }}
  .src-block .msg:last-child {{ margin-bottom:0; }}
  .todo-block .msg {{ border-left:3px solid var(--accent); }}
  .evidence-block .msg {{ border-left:3px solid var(--good); }}
  .ev-jump {{
    font-size:11px; color:var(--good); text-decoration:none;
    margin-left:auto; padding-left:8px;
  }}
  .ev-jump:hover {{ text-decoration:underline; }}
  .cols {{ margin-top:8px; display:flex; flex-wrap:wrap; gap:6px 14px; font-size:12px; color:var(--text2); }}
  .col-k {{ color:var(--text); font-weight:500; }}
  details {{ margin-top:10px; }}
  details summary {{
    cursor:pointer; font-size:12px; color:var(--text2); padding:4px 0; user-select:none;
  }}
  .thread {{ margin-top:6px; padding:8px 10px; background:var(--surface2); border-radius:6px; }}
  .thread.empty {{ color:var(--text2); font-style:italic; font-size:12px; }}
  .msg {{ padding:6px 0; border-bottom:1px solid var(--border); }}
  .msg:last-child {{ border-bottom:none; }}
  .msg-head {{ font-size:12px; color:var(--text2); display:flex; gap:10px; }}
  .who {{ font-weight:600; color:var(--text); }}
  .msg-body {{ font-size:13px; white-space:pre-wrap; word-break:break-word; margin-top:2px; }}
  .actions {{
    display:flex; gap:10px; align-items:center; margin-top:10px; flex-wrap:wrap;
  }}
  .open-slack {{
    font-size:12px; color:var(--text2); text-decoration:none;
  }}
  .open-slack:hover {{ color:var(--accent); }}
  .del-btn {{
    font-size:12px; padding:5px 10px; border:1px solid var(--border);
    border-radius:5px; background:var(--surface2); color:var(--text); cursor:pointer;
    margin-left:auto;
  }}
  .del-btn:hover {{ background:var(--good); border-color:var(--good); color:#fff; }}
  .del-btn:disabled {{ opacity:0.5; cursor:wait; }}
  .ev-list {{ margin-top:6px; display:flex; flex-wrap:wrap; gap:6px; }}
  .ev-link {{
    font-size:11px; padding:2px 8px; border-radius:10px;
    background:rgba(34,197,94,0.12); color:var(--good); text-decoration:none;
  }}
  .ev-link:hover {{ background:rgba(34,197,94,0.22); }}
  .ext {{ color:var(--accent); text-decoration:none; }}
  .ext:hover {{ text-decoration:underline; }}
  .msg.evidence {{ background:rgba(34,197,94,0.06); border-radius:4px; padding-left:8px; }}
  .ev-mark {{ color:var(--good); }}
  .empty {{ color:var(--text2); }}
  .card.removing {{ opacity:0.3; transition:opacity 0.3s; }}
  @media (max-width:600px) {{ body {{ padding:16px; }} h1 {{ font-size:20px; }} }}
</style>
</head>
<body>
<header>
  <h1>people-reminders</h1>
  <div class="meta">{meta}</div>
</header>
<div class="controls">{"".join(controls)}</div>
{body_html}
<script>
function confirmDelete(btn) {{
  const row = btn.dataset.row;
  if (!row) return;
  if (!confirm("Slack list からこの行を削除します。よい？")) return;
  btn.disabled = true;
  btn.textContent = "削除中…";
  fetch("/api/delete", {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{row_id: row}}),
  }}).then(r => r.json()).then(j => {{
    if (j.ok) {{
      const card = btn.closest(".card");
      card.classList.add("removing");
      setTimeout(() => card.remove(), 300);
    }} else {{
      btn.disabled = false;
      btn.textContent = "本当に終わってる ✓";
      alert("削除失敗: " + (j.error || "unknown"));
    }}
  }}).catch(e => {{
    btn.disabled = false;
    btn.textContent = "本当に終わってる ✓";
    alert("削除失敗: " + e);
  }});
}}
</script>
</body>
</html>'''


# --- HTTP ---

class Handler(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/", ""):
            self.send_response(404); self.end_headers(); return
        qs = urllib.parse.parse_qs(parsed.query)
        show = (qs.get("show") or [""])[0]
        force = "1" in qs.get("refresh", [])
        judge = "1" not in qs.get("nojudge", [])
        rows, err = fetch(force=force, judge=judge)
        html = generate_html(rows or [], err, show, CACHE["judged"])
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/delete":
            self.send_response(404); self.end_headers(); return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b"{}"
            payload = json.loads(body or b"{}")
            row_id = (payload or {}).get("row_id", "")
        except Exception as e:
            return self._json(400, {"ok": False, "error": f"bad request: {e}"})
        if not row_id or not row_id.startswith("Rec"):
            return self._json(400, {"ok": False, "error": "invalid row_id"})
        try:
            ok, err = delete_row(row_id)
        except Exception as e:
            return self._json(500, {"ok": False, "error": str(e)})
        if ok:
            return self._json(200, {"ok": True, "row_id": row_id})
        return self._json(500, {"ok": False, "error": err or "delete failed"})

    def log_message(self, *args, **kwargs):
        pass


def main():
    print(f"Fetching list '{LIST_NAME}'...", file=sys.stderr)
    rows, err = fetch(force=True, judge=True)
    if err:
        print(f"  (failed: {err})", file=sys.stderr)
    else:
        print(f"  {len(rows)} rows ({'judged' if CACHE['judged'] else 'unjudged'})", file=sys.stderr)

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Serving at http://localhost:{PORT}", file=sys.stderr)
    webbrowser.open(f"http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        server.server_close()


if __name__ == "__main__":
    main()
