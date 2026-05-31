#!/usr/bin/env python3
"""Web viewer for X follower analysis.

Reads /tmp/x_followers_<user>.json and /tmp/x_following_<user>.json,
serves a single-page UI with filter/sort/search.

  python3 web.py                # uses gogo_tanaka by default
  python3 web.py --user fooo    # 別ユーザー
  python3 web.py --port 8377    # ポート変更
"""
from __future__ import annotations

import argparse
import html
import json
import pathlib
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8377

PAGE_TMPL = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>X follower 分析 — @__USER__</title>
<style>
  :root {
    --bg: #fafafa;
    --fg: #1f2328;
    --muted: #656d76;
    --line: #d0d7de;
    --accent: #1f6feb;
    --pink: #db2777;
    --green: #16a34a;
    --amber: #d97706;
  }
  body { font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", system-ui, sans-serif;
         margin: 0; background: var(--bg); color: var(--fg); }
  header { padding: 18px 24px; border-bottom: 1px solid var(--line); background: #fff;
           position: sticky; top: 0; z-index: 10; }
  h1 { font-size: 18px; margin: 0 0 6px; }
  .meta { color: var(--muted); font-size: 12px; }
  .meta strong { color: var(--fg); }
  .filters { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 12px; align-items: center; }
  .filters label { font-size: 13px; color: var(--muted); display: flex; align-items: center; gap: 6px; }
  .filters input[type=number], .filters input[type=search], .filters select {
    border: 1px solid var(--line); border-radius: 6px; padding: 6px 10px; font: inherit;
    font-size: 13px; background: #fff;
  }
  .filters input[type=number] { width: 90px; }
  .filters input[type=search] { width: 220px; }
  .filters .count { font-weight: 600; color: var(--accent); margin-left: auto; }
  ul.users { list-style: none; padding: 0; margin: 0; }
  li.user { display: grid; grid-template-columns: 110px 1fr; gap: 16px;
            padding: 14px 24px; border-bottom: 1px solid var(--line);
            background: #fff; }
  li.user:hover { background: #fafafa; }
  .fcount { font-variant-numeric: tabular-nums; font-weight: 600; text-align: right;
            font-size: 18px; color: var(--fg); }
  .fcount.miss { color: #ccc; font-weight: 400; font-size: 14px; }
  .head { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
  .head a.handle { color: var(--accent); text-decoration: none; font-weight: 600; }
  .head a.handle:hover { text-decoration: underline; }
  .head .name { color: var(--muted); }
  .badges { display: inline-flex; gap: 4px; flex-wrap: wrap; }
  .badge { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 11px;
           border: 1px solid var(--line); color: var(--muted); background: #fff; }
  .badge.verified { background: #dbeafe; border-color: #93c5fd; color: #1e40af; }
  .badge.not-back { background: #fce7f3; border-color: #f9a8d4; color: var(--pink); font-weight: 600; }
  .badge.mutual  { background: #dcfce7; border-color: #86efac; color: var(--green); }
  .badge.cat { background: #fef3c7; border-color: #fcd34d; color: var(--amber); }
  .bio { color: #444; font-size: 13px; margin-top: 4px; line-height: 1.5; }
  .none { padding: 40px; text-align: center; color: var(--muted); }
</style>
</head>
<body>

<header>
  <h1>X follower 分析 — @__USER__</h1>
  <div class="meta">
    followers <strong id="m-followers">0</strong>取得 (実数 __FOLLOWERS_TOTAL__) /
    following <strong id="m-following">0</strong>取得 (実数 __FOLLOWING_TOTAL__) /
    <span title="following / followers">follow比 <strong id="m-ratio">-</strong></span> /
    相互 <strong id="m-mutual">0</strong>人 /
    未フォロー <strong id="m-notback">0</strong>人 /
    enrich済 <strong id="m-enriched">0</strong>人
  </div>
  <div class="filters">
    <label>min followers <input type="number" id="minf" value="0" step="100"></label>
    <label>category
      <select id="cat">
        <option value="">(all)</option>
        <option value="engineer">engineer</option>
        <option value="ai">AI/ML/研究</option>
        <option value="pm">PM</option>
        <option value="marketing">marketing</option>
        <option value="founder">founder/CEO</option>
        <option value="designer">designer</option>
        <option value="investor">investor/VC</option>
      </select>
    </label>
    <label><input type="checkbox" id="notback"> 未フォローのみ</label>
    <label><input type="checkbox" id="vonly"> verified only</label>
    <label>sort
      <select id="sort">
        <option value="followers">followers desc</option>
        <option value="order">recent (画面表示順)</option>
        <option value="handle">@handle</option>
      </select>
    </label>
    <label>q <input type="search" id="q" placeholder="@handle, name, bio"></label>
    <span class="count"><span id="count">0</span>人表示</span>
  </div>
</header>

<ul id="list" class="users"></ul>
<div id="none" class="none" style="display:none">該当なし</div>

<script>
const DATA = __DATA_JSON__;

const CAT_PATTERNS = {
  engineer: [/engineer/i, /developer/i, /\\bCTO\\b/, /\\bSRE\\b/, /devops/i, /backend/i,
             /frontend/i, /full[- ]?stack/i, /software/i, /infrastructure/i,
             /platform/i, /\\bSWE\\b/, /エンジニア/, /開発/, /プログラマ/],
  ai: [/machine learning/i, /\\bML\\b/, /\\bAI\\b/, /data scientist/i, /researcher/i,
       /research/i, /研究/],
  pm: [/product manager/i, /\\bPM\\b/, /プロダクトマネージャ/, /プロダクトマネジャ/,
       /\\bCPO\\b/],
  marketing: [/marketing/i, /marketer/i, /\\bCMO\\b/, /growth/i, /マーケティング/,
              /マーケター/, /グロース/],
  founder: [/founder/i, /\\bCEO\\b/, /\\bCOO\\b/, /entrepreneur/i, /起業/, /創業/,
            /代表取締役/, /経営者/],
  designer: [/designer/i, /デザイナー/, /UI\\/UX/i, /\\bUX\\b/],
  investor: [/investor/i, /\\bVC\\b/, /venture/i, /投資家/, /キャピタリスト/],
};
function matchCat(bio, cat) {
  if (!cat) return true;
  const pats = CAT_PATTERNS[cat] || [];
  return pats.some(re => re.test(bio || ""));
}
function badgesForBio(bio) {
  const out = [];
  for (const c of Object.keys(CAT_PATTERNS)) {
    if (CAT_PATTERNS[c].some(re => re.test(bio || ""))) out.push(c);
  }
  return out;
}

const $list = document.getElementById("list");
const $none = document.getElementById("none");
const $count = document.getElementById("count");
const ctrls = ["minf","cat","notback","vonly","sort","q"].map(id => document.getElementById(id));
ctrls.forEach(el => el.addEventListener("input", render));

document.getElementById("m-followers").textContent = DATA.followers.length;
document.getElementById("m-following").textContent = DATA.following_count;
document.getElementById("m-mutual").textContent = DATA.followers.filter(u => DATA.following_set[u.screen_name.toLowerCase()]).length;
document.getElementById("m-notback").textContent = DATA.followers.filter(u => !DATA.following_set[u.screen_name.toLowerCase()]).length;
document.getElementById("m-enriched").textContent = DATA.followers.filter(u => u.followers_count != null).length;
(function setRatio() {
  const ft = Number(DATA.followers_total), gt = Number(DATA.following_total);
  if (Number.isFinite(ft) && Number.isFinite(gt) && ft > 0) {
    const r = gt / ft;
    document.getElementById("m-ratio").textContent =
      `${gt.toLocaleString()} / ${ft.toLocaleString()} = ${r.toFixed(3)}`;
  } else {
    document.getElementById("m-ratio").textContent = "n/a";
  }
})();

function render() {
  const minf = parseInt(document.getElementById("minf").value, 10) || 0;
  const cat = document.getElementById("cat").value;
  const notback = document.getElementById("notback").checked;
  const vonly = document.getElementById("vonly").checked;
  const sort = document.getElementById("sort").value;
  const q = document.getElementById("q").value.trim().toLowerCase();

  let users = DATA.followers.slice();
  users = users.filter(u => {
    if (vonly && !u.verified) return false;
    if (notback && DATA.following_set[u.screen_name.toLowerCase()]) return false;
    if (minf > 0) {
      if (u.followers_count == null) return false;
      if (u.followers_count < minf) return false;
    }
    if (cat && !matchCat(u.description, cat)) return false;
    if (q) {
      const hay = (u.screen_name + " " + (u.name||"") + " " + (u.description||"")).toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  if (sort === "followers") {
    users.sort((a,b) => (b.followers_count ?? -1) - (a.followers_count ?? -1));
  } else if (sort === "handle") {
    users.sort((a,b) => a.screen_name.toLowerCase().localeCompare(b.screen_name.toLowerCase()));
  }
  // order: keep DATA order (画面表示順=最近)

  $count.textContent = users.length;
  $list.innerHTML = "";
  if (!users.length) { $none.style.display = "block"; return; }
  $none.style.display = "none";

  const frag = document.createDocumentFragment();
  for (const u of users) {
    const li = document.createElement("li");
    li.className = "user";
    const fc = u.followers_count;
    const fcStr = fc == null ? '<div class="fcount miss">?</div>' :
                              `<div class="fcount">${fc.toLocaleString()}</div>`;
    const isMutual = DATA.following_set[u.screen_name.toLowerCase()];
    const stateBadge = isMutual
      ? '<span class="badge mutual">相互</span>'
      : '<span class="badge not-back">未フォロー</span>';
    const verifiedBadge = u.verified ? '<span class="badge verified">verified</span>' : '';
    const catBadges = badgesForBio(u.description||"").map(c=>`<span class="badge cat">${c}</span>`).join("");
    const bio = (u.description||"").replace(/[<>&]/g, m=>({"<":"&lt;",">":"&gt;","&":"&amp;"}[m]));
    li.innerHTML = `
      ${fcStr}
      <div>
        <div class="head">
          <a class="handle" href="https://x.com/${u.screen_name}" target="_blank">@${u.screen_name}</a>
          <span class="name">${(u.name||"").replace(/[<>&]/g, m=>({"<":"&lt;",">":"&gt;","&":"&amp;"}[m]))}</span>
          <span class="badges">${stateBadge}${verifiedBadge}${catBadges}</span>
        </div>
        ${bio ? `<div class="bio">${bio}</div>` : ""}
      </div>
    `;
    frag.appendChild(li);
  }
  $list.appendChild(frag);
}

// initial sort by followers desc
document.getElementById("sort").value = "followers";
render();
</script>
</body>
</html>
"""


def build_payload(user: str, followers_total: int, following_total: int) -> dict:
    f_path = pathlib.Path(f"/tmp/x_followers_{user}.json")
    g_path = pathlib.Path(f"/tmp/x_following_{user}.json")
    if not f_path.exists():
        raise FileNotFoundError(f"{f_path} not found — run.py で先に collect してください")
    f = json.loads(f_path.read_text(encoding="utf-8"))
    followers = f.get("all_users") or f.get("users") or []
    if g_path.exists():
        g = json.loads(g_path.read_text(encoding="utf-8"))
        following = g.get("all_users") or []
    else:
        following = []
    following_set = {u["screen_name"].lower(): True for u in following}
    return {
        "user": user,
        "followers": followers,
        "following_count": len(following),
        "following_set": following_set,
        "followers_total": followers_total,
        "following_total": following_total,
    }


def make_page(user: str, payload: dict, followers_total: str, following_total: str) -> bytes:
    html_str = (
        PAGE_TMPL
        .replace("__USER__", html.escape(user))
        .replace("__FOLLOWERS_TOTAL__", html.escape(str(followers_total)))
        .replace("__FOLLOWING_TOTAL__", html.escape(str(following_total)))
        .replace("__DATA_JSON__", json.dumps(payload, ensure_ascii=False))
    )
    return html_str.encode("utf-8")


def make_handler(user: str, followers_total: str, following_total: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            try:
                payload = build_payload(user, followers_total, following_total)
                body = make_page(user, payload, followers_total, following_total)
            except Exception as e:
                body = f"<pre>error: {html.escape(str(e))}</pre>".encode()
                self.send_response(500)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--user", default="gogo_tanaka")
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--followers-total", default="?",
                   help="表示用: X プロフィール上の followers 総数")
    p.add_argument("--following-total", default="?",
                   help="表示用: X プロフィール上の following 総数")
    args = p.parse_args(argv)

    handler = make_handler(args.user, args.followers_total, args.following_total)
    server = HTTPServer(("127.0.0.1", args.port), handler)
    url = f"http://localhost:{args.port}"
    print(f"[x-follower-web] serving {url}", file=sys.stderr)
    threading.Thread(target=lambda: (time.sleep(0.4), webbrowser.open(url)), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[x-follower-web] stopped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
