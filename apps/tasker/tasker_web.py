#!/usr/bin/env python3
"""Serve Notion MAIN todos + Slack Later items as a categorized web page.

- Notion todos: grouped by heading (# / ## / ###).
- Slack Later:  grouped by channel prefix (HR / Dev / Sales / ...).

Unchecked Notion todos are shown by default; `?show=all` includes checked ones.
If Slack Later fetch fails (e.g. Slack not running), the Slack section is
hidden and the error is noted in the meta line.

Usage: python3 tasker_web.py
"""
import html as html_lib
import json
import os
import re
import subprocess
import sys
import urllib.parse
import webbrowser
from collections import OrderedDict, defaultdict
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = 8378
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FETCH_NOTION = os.path.join(SCRIPT_DIR, "fetch_notion.py")
FETCH_LATER = os.path.join(SCRIPT_DIR, "fetch_later.py")
LATER_LIMIT = "100"

UNCATEGORIZED = "未分類"

# Reused from slack-later/scripts/later_web.py.
CHANNEL_CATEGORIES = {
    "HR / 採用": ["hr-", "welcome-", "_mentors"],
    "Dev / Tech": ["dev-", "st-dev"],
    "Sales / Biz": ["sales-", "bo-", "ex-"],
    "C2C / ポケカ": ["c2c-", "z-ポケカ", "ps-"],
    "Tradejam": ["tr-"],
    "Supateam": ["st-"],
    "経営": ["_executives", "_ceos", "_aisaac"],
}


# --- Notion parsing ---

def parse_notion(text):
    """Parse fetch_notion.py output into {heading: [{text, checked, indent}]}."""
    cats = OrderedDict()
    current = UNCATEGORIZED

    heading_re = re.compile(r"^\s*(#{1,3})\s+(.*)$")
    todo_re = re.compile(r"^(\s*)-\s+\[( |x)\]\s+(.*)$")

    for raw in text.splitlines():
        if not raw.strip():
            continue
        m = heading_re.match(raw)
        if m:
            current = m.group(2).strip() or UNCATEGORIZED
            cats.setdefault(current, [])
            continue
        m = todo_re.match(raw)
        if m:
            indent = len(m.group(1))
            checked = m.group(2) == "x"
            body = m.group(3).strip()
            if not body:
                continue
            cats.setdefault(current, []).append(
                {"text": body, "checked": checked, "indent": indent}
            )

    return OrderedDict((k, v) for k, v in cats.items() if v)


# --- Slack later categorization ---

def categorize_channel(ch_name):
    for cat, prefixes in CHANNEL_CATEGORIES.items():
        for prefix in prefixes:
            if ch_name.startswith(prefix) or prefix in ch_name:
                return cat
    return "その他"


def clean_slack_text(text):
    text = html_lib.escape(text)
    text = re.sub(r'&lt;(https?://[^|&]+)\|([^&]+)&gt;',
                  r'<a href="\1" target="_blank">\2</a>', text)
    text = re.sub(r'&lt;(https?://[^&]+)&gt;',
                  r'<a href="\1" target="_blank">\1</a>', text)
    text = re.sub(r'&lt;@U[A-Z0-9]+&gt;', '@user', text)
    text = text.replace('&lt;!channel&gt;', '@channel')
    text = text.replace('&lt;!here&gt;', '@here')
    text = re.sub(r'&lt;!subteam\^[^&]+&gt;', '@team', text)
    return text.replace('\n', '<br>')


# --- Rendering ---

def slug(name):
    return re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower() or "cat"


def render_todo(t):
    cls = "todo done" if t["checked"] else "todo"
    box = "&#9745;" if t["checked"] else "&#9744;"
    indent_px = min(t["indent"], 8) * 12
    return (
        f'<div class="{cls}" style="padding-left:{indent_px}px">'
        f'<span class="box">{box}</span>'
        f'<span class="txt">{html_lib.escape(t["text"])}</span>'
        f"</div>"
    )


def render_later_card(item):
    preview = clean_slack_text(item["text"][:300]) if item["text"] else "<em>（プレビューなし）</em>"
    return f'''
        <div class="card">
          <div class="card-header">
            <span class="channel">#{html_lib.escape(item["channel"])}</span>
            <span class="date">{html_lib.escape(item["saved_date"])}</span>
          </div>
          <div class="preview">{preview}</div>
          <a href="{html_lib.escape(item["link"])}" target="_blank" class="link">Slackで開く &rarr;</a>
        </div>'''


def render_notion_section(categorized, show_all):
    total = sum(
        len([t for t in items if show_all or not t["checked"]])
        for items in categorized.values()
    )
    sorted_cats = sorted(
        categorized.items(),
        key=lambda x: (x[0] == UNCATEGORIZED, -len([t for t in x[1] if show_all or not t["checked"]])),
    )
    parts = []
    for cat, items in sorted_cats:
        visible = [t for t in items if show_all or not t["checked"]]
        if not visible:
            continue
        rendered = "".join(render_todo(t) for t in visible)
        parts.append(f'''
    <div class="category" id="n-{slug(cat)}">
      <h3 class="cat-title" onclick="this.parentElement.classList.toggle('collapsed')">
        <span class="arrow">&#9660;</span> {html_lib.escape(cat)} <span class="badge">{len(visible)}</span>
      </h3>
      <div class="todos">{rendered}</div>
    </div>''')
    body = "".join(parts) or '<p class="meta">todoが見つからなかった。</p>'
    return total, f'''
<section class="group">
  <h2 class="group-title">Notion Todos <span class="group-count">{total}</span></h2>
  {body}
</section>'''


def render_later_section(items, error):
    if error:
        return 0, f'''
<section class="group">
  <h2 class="group-title">Slack Later</h2>
  <p class="meta err">取得失敗: {html_lib.escape(error)}</p>
</section>'''
    if not items:
        return 0, ''

    grouped = defaultdict(list)
    for it in items:
        grouped[categorize_channel(it["channel"])].append(it)
    sorted_cats = sorted(grouped.items(),
                         key=lambda x: (x[0] == "その他", -len(x[1])))

    parts = []
    for cat, its in sorted_cats:
        cards = "".join(render_later_card(it) for it in its)
        parts.append(f'''
    <div class="category" id="l-{slug(cat)}">
      <h3 class="cat-title" onclick="this.parentElement.classList.toggle('collapsed')">
        <span class="arrow">&#9660;</span> {html_lib.escape(cat)} <span class="badge">{len(its)}</span>
      </h3>
      <div class="cards">{cards}</div>
    </div>''')
    return len(items), f'''
<section class="group">
  <h2 class="group-title">Slack Later <span class="group-count">{len(items)}</span></h2>
  {"".join(parts)}
</section>'''


def generate_html(categorized, later_items, later_error, show_all):
    notion_total, notion_html = render_notion_section(categorized, show_all)
    later_total, later_html = render_later_section(later_items, later_error)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    toggle_href = "/" if show_all else "/?show=all"
    toggle_label = "未完了のみ" if show_all else "完了も表示"
    refresh_href = "/?refresh=1" + ("&show=all" if show_all else "")

    return f'''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tasker — Notion Todos + Slack Later</title>
<style>
  :root {{
    --bg: #0f0f0f; --surface: #1a1a1a; --surface2: #242424;
    --border: #333; --text: #e0e0e0; --text2: #999;
    --accent: #4a9eff; --accent2: #7c3aed; --green: #22c55e; --red: #ef4444;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.6;
    padding: 24px; max-width: 960px; margin: 0 auto;
  }}
  header {{ margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }}
  h1 {{ font-size: 24px; font-weight: 600; margin-bottom: 4px; }}
  .meta {{ color: var(--text2); font-size: 13px; }}
  .meta.err {{ color: var(--red); }}
  .controls {{ display: flex; gap: 8px; margin: 16px 0 24px; flex-wrap: wrap; }}
  .controls a {{
    padding: 6px 14px; border: 1px solid var(--border); border-radius: 6px;
    background: var(--surface); color: var(--text); text-decoration: none;
    font-size: 13px; transition: all 0.15s;
  }}
  .controls a:hover {{ background: var(--surface2); border-color: var(--accent); }}
  .group {{ margin-bottom: 32px; }}
  .group-title {{
    font-size: 20px; font-weight: 700; margin-bottom: 12px;
    padding-bottom: 8px; border-bottom: 1px solid var(--border);
    display: flex; align-items: baseline; gap: 10px;
  }}
  .group-count {{ color: var(--text2); font-size: 14px; font-weight: 500; }}
  .category {{ margin-bottom: 16px; }}
  .cat-title {{
    font-size: 16px; font-weight: 600; cursor: pointer;
    padding: 8px 0; display: flex; align-items: center; gap: 8px; user-select: none;
  }}
  .cat-title:hover {{ color: var(--accent); }}
  .arrow {{ font-size: 12px; transition: transform 0.2s; }}
  .collapsed .arrow {{ transform: rotate(-90deg); }}
  .collapsed .todos, .collapsed .cards {{ display: none; }}
  .badge {{
    background: var(--accent); color: #fff; font-size: 12px;
    font-weight: 500; padding: 2px 8px; border-radius: 10px;
  }}
  .todos {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 8px 0;
  }}
  .todo {{
    display: flex; gap: 10px; padding: 6px 16px; font-size: 14px;
    border-bottom: 1px solid transparent;
  }}
  .todo:not(:last-child) {{ border-bottom-color: var(--border); }}
  .todo .box {{ color: var(--text2); flex-shrink: 0; }}
  .todo.done {{ color: var(--text2); }}
  .todo.done .txt {{ text-decoration: line-through; }}
  .txt {{ word-break: break-word; }}
  .cards {{ display: flex; flex-direction: column; gap: 8px; }}
  .card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 14px 16px; transition: border-color 0.15s;
  }}
  .card:hover {{ border-color: var(--accent); }}
  .card-header {{
    display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;
  }}
  .channel {{ font-size: 13px; font-weight: 600; color: var(--accent); }}
  .date {{ font-size: 12px; color: var(--text2); }}
  .preview {{
    font-size: 14px; color: var(--text); line-height: 1.5;
    max-height: 4.5em; overflow: hidden; word-break: break-word;
  }}
  .preview a {{ color: var(--accent); text-decoration: none; }}
  .preview a:hover {{ text-decoration: underline; }}
  .link {{
    display: inline-block; margin-top: 8px; font-size: 13px;
    color: var(--text2); text-decoration: none;
  }}
  .link:hover {{ color: var(--accent); }}
  @media (max-width: 600px) {{ body {{ padding: 16px; }} h1 {{ font-size: 20px; }} }}
</style>
</head>
<body>
<header>
  <h1>Tasker</h1>
  <div class="meta">Notion: {notion_total} / Slack Later: {later_total} / {now} 更新</div>
</header>
<div class="controls">
  <a href="{toggle_href}">{toggle_label}</a>
  <a href="{refresh_href}">再取得</a>
</div>
{notion_html}
{later_html}
</body>
</html>'''


# --- Fetchers ---

CACHE = {"notion": None, "later": None, "later_error": None}


def fetch_notion_text(force=False):
    if CACHE["notion"] is None or force:
        result = subprocess.run(
            [sys.executable, FETCH_NOTION],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"fetch_notion failed: {result.stderr}")
        CACHE["notion"] = result.stdout
    return CACHE["notion"]


def fetch_later_items(force=False):
    if CACHE["later"] is not None and not force:
        return CACHE["later"], CACHE["later_error"]
    try:
        result = subprocess.run(
            [sys.executable, FETCH_LATER, LATER_LIMIT],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            err = (result.stderr or "").strip().splitlines()[-1] if result.stderr else "non-zero exit"
            CACHE["later"], CACHE["later_error"] = [], err
        else:
            CACHE["later"] = json.loads(result.stdout)
            CACHE["later_error"] = None
    except Exception as e:
        CACHE["later"], CACHE["later_error"] = [], str(e)
    return CACHE["later"], CACHE["later_error"]


# --- HTTP Server ---

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/", ""):
            self.send_response(404)
            self.end_headers()
            return
        qs = urllib.parse.parse_qs(parsed.query)
        show_all = "all" in qs.get("show", [])
        force = "1" in qs.get("refresh", [])
        try:
            notion_text = fetch_notion_text(force=force)
        except Exception as e:
            body = f"<pre>Notion error: {html_lib.escape(str(e))}</pre>".encode("utf-8")
            self._respond(500, "text/html; charset=utf-8", body)
            return
        later_items, later_error = fetch_later_items(force=force)
        categorized = parse_notion(notion_text)
        html = generate_html(categorized, later_items, later_error, show_all)
        self._respond(200, "text/html; charset=utf-8", html.encode("utf-8"))

    def _respond(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args, **kwargs):
        pass


def main():
    print("Fetching Notion MAIN...", file=sys.stderr)
    fetch_notion_text(force=True)
    print("Fetching Slack Later...", file=sys.stderr)
    items, err = fetch_later_items(force=True)
    if err:
        print(f"  (Slack Later unavailable: {err})", file=sys.stderr)
    else:
        print(f"  {len(items)} Later items", file=sys.stderr)

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Serving at http://localhost:{PORT}", file=sys.stderr)
    webbrowser.open(f"http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        server.server_close()


if __name__ == "__main__":
    main()
