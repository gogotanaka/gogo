#!/usr/bin/env python3
"""Serve Slack 'Save for later' items as a categorized web page.

Usage: python3 later_web.py [limit]
  - Fetches items via fetch_later_standalone.py
  - Categorizes by channel prefix and displays at http://localhost:8377
"""
import json
import os
import re
import subprocess
import sys
import urllib.parse
import webbrowser
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = 8377
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FETCH_SCRIPT = os.path.join(SCRIPT_DIR, "fetch_later_standalone.py")

# --- Channel categorization ---

CHANNEL_CATEGORIES = {
    "HR / 採用": ["hr-", "welcome-", "_mentors"],
    "Dev / Tech": ["dev-", "st-dev"],
    "Sales / Biz": ["sales-", "bo-", "ex-"],
    "C2C / ポケカ": ["c2c-", "z-ポケカ", "ps-"],
    "Tradejam": ["tr-"],
    "Supateam": ["st-"],
    "経営": ["_executives", "_ceos", "_aisaac"],
}


def categorize_channel(ch_name):
    for cat, prefixes in CHANNEL_CATEGORIES.items():
        for prefix in prefixes:
            if ch_name.startswith(prefix) or prefix in ch_name:
                return cat
    return "その他"


def slug(cat_name):
    """Convert category name to a safe HTML id slug."""
    return re.sub(r'[^a-zA-Z0-9]+', '-', cat_name).strip('-').lower() or 'other'


def clean_text_html(text):
    text = re.sub(r'<(https?://[^|>]+)\|([^>]+)>', r'<a href="\1" target="_blank">\2</a>', text)
    text = re.sub(r'<(https?://[^>]+)>', r'<a href="\1" target="_blank">\1</a>', text)
    text = re.sub(r'<@U[A-Z0-9]+>', '@user', text)
    text = re.sub(r'<!channel>', '@channel', text)
    text = re.sub(r'<!here>', '@here', text)
    text = re.sub(r'<!subteam\^[^>]+>', '@team', text)
    text = text.replace('\n', '<br>')
    return text


# --- HTML rendering ---

def _render_card(item):
    preview = clean_text_html(item["text"][:300]) if item["text"] else "<em>（プレビューなし）</em>"
    cat_label = item.get("category", "")
    return f'''
        <div class="card" data-cat="{cat_label}">
          <div class="card-header">
            <span class="channel">#{item["channel"]}</span>
            <span class="date">{item["saved_date"]}</span>
          </div>
          <div class="preview">{preview}</div>
          <a href="{item["link"]}" target="_blank" class="link">Slackで開く &rarr;</a>
        </div>'''


def generate_html(categorized):
    total = sum(len(items) for items in categorized.values())
    sorted_cats = sorted(categorized.items(), key=lambda x: (x[0] == "その他", -len(x[1])))

    cat_links = {}
    cat_html_parts = []
    for cat, items in sorted_cats:
        cards = "".join(_render_card(item) for item in items)
        cat_id = f"cat-{slug(cat)}"
        cat_links[cat_id] = [item["link"] for item in items]
        cat_html_parts.append(f'''
    <div class="category" id="{cat_id}">
      <div class="cat-header">
        <h2 class="cat-title" onclick="this.parentElement.parentElement.classList.toggle('collapsed')">
          <span class="arrow">&#9660;</span> {cat} <span class="badge">{len(items)}</span>
        </h2>
        <button class="open-all-btn" onclick="openAll('{cat_id}', this)">全部Slackで開く</button>
      </div>
      <div class="cards">{cards}
      </div>
    </div>''')

    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    html = f'''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Slack Later</title>
<style>
  :root {{
    --bg: #0f0f0f; --surface: #1a1a1a; --surface2: #242424;
    --border: #333; --text: #e0e0e0; --text2: #999;
    --accent: #4a9eff; --accent2: #7c3aed; --green: #22c55e;
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
  .controls {{
    display: flex; gap: 8px; margin-bottom: 24px; flex-wrap: wrap;
  }}
  .controls button {{
    padding: 6px 14px; border: 1px solid var(--border); border-radius: 6px;
    background: var(--surface); color: var(--text); cursor: pointer;
    font-size: 13px; transition: all 0.15s;
  }}
  .controls button:hover {{ background: var(--surface2); border-color: var(--accent); }}
  .controls button.active {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
  .category {{ margin-bottom: 24px; }}
  .cat-header {{ display: flex; justify-content: space-between; align-items: center; }}
  .cat-title {{
    font-size: 18px; font-weight: 600; cursor: pointer;
    padding: 10px 0; display: flex; align-items: center; gap: 8px; user-select: none;
  }}
  .cat-title:hover {{ color: var(--accent); }}
  .arrow {{ font-size: 12px; transition: transform 0.2s; }}
  .collapsed .arrow {{ transform: rotate(-90deg); }}
  .collapsed .cards {{ display: none; }}
  .badge {{
    background: var(--accent); color: #fff; font-size: 12px;
    font-weight: 500; padding: 2px 8px; border-radius: 10px;
  }}
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
  .open-all-btn {{
    padding: 6px 14px; border: 1px solid var(--accent2); border-radius: 6px;
    background: transparent; color: var(--accent2); cursor: pointer;
    font-size: 12px; font-weight: 500; transition: all 0.15s; white-space: nowrap;
  }}
  .open-all-btn:hover {{ background: var(--accent2); color: #fff; }}
  .open-all-btn.done {{ border-color: var(--green); color: var(--green); pointer-events: none; }}
  @media (max-width: 600px) {{ body {{ padding: 16px; }} h1 {{ font-size: 20px; }} }}
</style>
</head>
<body>
<header>
  <h1>Slack Later Items</h1>
  <div class="meta">{total} items / {now} 更新</div>
</header>
<div class="controls">
  <button class="active" onclick="filterCat('all', this)">All ({total})</button>
  {"".join('<button onclick="filterCat(' + chr(39) + 'cat-' + slug(cat) + chr(39) + ', this)">' + cat + ' (' + str(len(items)) + ')</button>' for cat, items in sorted_cats)}
</div>
{"".join(cat_html_parts)}
<script>
function filterCat(id, btn) {{
  document.querySelectorAll('.controls button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.category').forEach(el => {{
    el.style.display = (id === 'all' || el.id === id) ? '' : 'none';
  }});
}}
function openAll(catId, btn) {{
  btn.textContent = '開いています...';
  fetch('/open?cat=' + encodeURIComponent(catId))
    .then(r => r.json())
    .then(d => {{
      btn.textContent = d.count + '件 開きました';
      btn.classList.add('done');
    }})
    .catch(() => {{ btn.textContent = 'エラー'; }});
}}
</script>
</body>
</html>'''
    return html, cat_links


# --- HTTP Server ---

CAT_LINKS = {}
HTML_CONTENT = ""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "":
            self._respond(200, "text/html; charset=utf-8", HTML_CONTENT.encode("utf-8"))
        elif self.path.startswith("/open?"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            cat_id = qs.get("cat", [""])[0]
            links = CAT_LINKS.get(cat_id, [])
            for url in links:
                subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._respond_json({"ok": True, "count": len(links)})
        else:
            self.send_response(404)
            self.end_headers()

    def _respond(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def _respond_json(self, data):
        self._respond(200, "application/json", json.dumps(data).encode())

    def log_message(self, format, *args):
        pass


# --- Main ---

def main():
    global CAT_LINKS, HTML_CONTENT

    limit = sys.argv[1] if len(sys.argv) > 1 else "50"
    print("Fetching saved items...", file=sys.stderr)

    result = subprocess.run(
        [sys.executable, FETCH_SCRIPT, limit],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        print(f"Error fetching items:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    items = json.loads(result.stdout)
    print(f"Got {len(items)} items", file=sys.stderr)

    categorized = defaultdict(list)
    for item in items:
        cat = categorize_channel(item["channel"])
        item["category"] = cat
        categorized[cat].append(item)

    HTML_CONTENT, CAT_LINKS = generate_html(categorized)
    print(f"{len(items)} items in {len(categorized)} categories", file=sys.stderr)

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
