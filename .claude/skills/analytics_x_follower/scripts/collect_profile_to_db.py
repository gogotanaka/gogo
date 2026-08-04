#!/usr/bin/env python3
"""各プロフィールページを Chrome アクティブタブで巡回し、
follower_count / following_count / i_follow / プロフィール情報 / 直近の投稿
を SQLite に保存。

Usage:
  python3 collect_profile_to_db.py /tmp/x_followers_gogo_tanaka.json \
      [--db /tmp/x_followers_gogo_tanaka.db] [--limit N] [--posts 10] [--skip-existing]
"""
from __future__ import annotations

import argparse
import base64
import json
import pathlib
import sqlite3
import subprocess
import sys
import time


def osa_js(js: str) -> str:
    """Run JS via AppleScript by base64-wrapping to avoid quote escape hell."""
    b64 = base64.b64encode(js.encode("utf-8")).decode("ascii")
    # atob returns a binary string (Latin-1); UTF-8 multi-byte chars (e.g. Japanese
    # regex literals) get mangled. Decode via TextDecoder to round-trip safely.
    wrapped = (
        "(function(){"
        f"var bin=atob('{b64}');"
        "var arr=new Uint8Array(bin.length);"
        "for(var i=0;i<bin.length;i++)arr[i]=bin.charCodeAt(i);"
        "var s=new TextDecoder('utf-8').decode(arr);"
        "var r=eval(s);"
        "return typeof r==='object'?JSON.stringify(r):String(r);"
        "})()"
    )
    apple = (
        'tell application "Google Chrome"\n'
        "  set theResult to execute active tab of window 1 javascript {js}\n"
        "  return theResult\n"
        "end tell"
    ).replace("{js}", json.dumps(wrapped))
    proc = subprocess.run(["osascript", "-e", apple], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"osascript failed: {proc.stderr.strip()}")
    return proc.stdout.rstrip("\n")


def osa_set_url(url: str) -> None:
    apple = (
        f'tell application "Google Chrome" to set URL of active tab of window 1 to "{url}"'
    )
    subprocess.run(["osascript", "-e", apple], check=True, capture_output=True)


PROFILE_JS = r"""
(function(postLimit, expectedScreen){
  var out = {ok:false};
  try{
    var path = (location.pathname||'').replace(/^\//,'').toLowerCase();
    out.path = path;
    if (expectedScreen && path.split('/')[0] !== expectedScreen.toLowerCase()){
      out.wrong_page = true;
      return JSON.stringify(out);
    }
    function parseCount(t){
      if(!t) return null;
      var m = t.match(/([\d,\.]+)\s*([KMB万千]?)/);
      if(!m) return null;
      var n = parseFloat(m[1].replace(/,/g,''));
      var s = m[2];
      if(s==='K') n*=1000;
      else if(s==='M') n*=1000000;
      else if(s==='B') n*=1000000000;
      else if(s==='万') n*=10000;
      else if(s==='千') n*=1000;
      return isNaN(n)?null:Math.round(n);
    }
    function txt(el){return el?(el.innerText||el.textContent||'').trim():'';}
    var primary = document.querySelector('[data-testid="primaryColumn"]') || document.body;
    var primaryText = (primary.innerText||'');
    var followersA = document.querySelector('a[href$="/verified_followers"]') || document.querySelector('a[href$="/followers"]');
    var followingA = document.querySelector('a[href$="/following"]');
    out.followers_count = parseCount(txt(followersA));
    out.following_count = parseCount(txt(followingA));
    if (out.followers_count == null){
      var mf = primaryText.match(/([\d,\.]+(?:K|M|B|万|千)?)\s*(?:Followers|フォロワー)/i);
      if (mf) out.followers_count = parseCount(mf[1] + ' Followers');
    }
    if (out.following_count == null){
      var mg = primaryText.match(/([\d,\.]+(?:K|M|B|万|千)?)\s*(?:Following|フォロー中)/i);
      if (mg) out.following_count = parseCount(mg[1] + ' Following');
    }
    var nameEl = document.querySelector('[data-testid="UserName"]');
    out.name = nameEl ? txt(nameEl).split('\n')[0] : '';
    var descEl = document.querySelector('[data-testid="UserDescription"]');
    out.description = txt(descEl);
    var locEl = document.querySelector('[data-testid="UserLocation"]');
    out.location = txt(locEl);
    var joinEl = document.querySelector('[data-testid="UserJoinDate"]');
    out.joined = txt(joinEl);
    var urlEl = document.querySelector('[data-testid="UserUrl"]');
    out.website = urlEl ? (urlEl.href || txt(urlEl)) : '';
    out.verified = !!document.querySelector('[data-testid="UserName"] svg[aria-label*="erified"]');
    out.protected = /ポストは非公開|These posts are protected|This account is private/i.test(primaryText);
    var unfollowBtn = document.querySelector('button[data-testid$="-unfollow"]');
    var followBtn   = document.querySelector('button[data-testid$="-follow"]');
    if (unfollowBtn) out.i_follow = true;
    else if (followBtn) out.i_follow = false;
    else out.i_follow = null;
    var posts = [];
    var arts = document.querySelectorAll('article[data-testid="tweet"]');
    for (var i=0; i<arts.length && posts.length<postLimit; i++){
      var a = arts[i];
      var t = a.querySelector('[data-testid="tweetText"]');
      var timeEl = a.querySelector('time');
      var linkEl = timeEl ? timeEl.closest('a') : null;
      var perma = linkEl ? linkEl.href : '';
      var idMatch = perma.match(/status\/(\d+)/);
      posts.push({
        post_id: idMatch ? idMatch[1] : '',
        text: t ? (t.innerText||'').trim() : '',
        posted_at: timeEl ? timeEl.getAttribute('datetime') : '',
        url: perma,
      });
    }
    out.posts = posts;
    out.ok = !!(out.name || out.followers_count != null);
  } catch(e) { out.error = String(e); }
  return JSON.stringify(out);
})(__POST_LIMIT__, __SCREEN__)
""".strip()


def fetch_profile(screen_name: str, post_limit: int, max_wait: float = 10.0) -> dict | None:
    osa_set_url(f"https://x.com/{screen_name}")
    deadline = time.time() + max_wait
    last = None
    started = time.time()
    js_tmpl = PROFILE_JS.replace("__POST_LIMIT__", str(post_limit)).replace(
        "__SCREEN__", json.dumps(screen_name)
    )
    while time.time() < deadline:
        time.sleep(0.8)
        try:
            raw = osa_js(js_tmpl)
        except Exception:
            continue
        if not raw or raw == "none":
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if data.get("wrong_page"):
            continue  # navigation hasn't completed yet
        last = data
        has_meta = data.get("name") and data.get("followers_count") is not None
        has_posts = bool(data.get("posts"))
        is_protected = bool(data.get("protected"))
        # For protected accounts, no posts will ever load → return as soon as meta present.
        # For normal accounts, wait until at least one post is captured (or hit min-wait threshold).
        if has_meta and (has_posts or is_protected):
            return data
        # Fallback: if we've spent half the budget and at least have meta, return.
        if has_meta and (time.time() - started) > (max_wait * 0.6):
            return data
    return last


SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
  screen_name      TEXT PRIMARY KEY,
  name             TEXT,
  description      TEXT,
  verified         INTEGER,
  protected        INTEGER,
  followers_count  INTEGER,
  following_count  INTEGER,
  i_follow         INTEGER,
  location         TEXT,
  joined           TEXT,
  website          TEXT,
  url              TEXT,
  collected_at     TEXT
);
CREATE TABLE IF NOT EXISTS posts (
  screen_name TEXT,
  post_id     TEXT,
  text        TEXT,
  posted_at   TEXT,
  url         TEXT,
  PRIMARY KEY (screen_name, post_id)
);
"""


def upsert(conn: sqlite3.Connection, screen_name: str, data: dict) -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    i_follow = data.get("i_follow")
    i_follow_val = 1 if i_follow is True else (0 if i_follow is False else None)
    conn.execute(
        """
        INSERT INTO profiles(screen_name, name, description, verified, protected, followers_count,
                             following_count, i_follow, location, joined, website, url, collected_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(screen_name) DO UPDATE SET
          name=excluded.name,
          description=excluded.description,
          verified=excluded.verified,
          protected=excluded.protected,
          followers_count=excluded.followers_count,
          following_count=excluded.following_count,
          i_follow=excluded.i_follow,
          location=excluded.location,
          joined=excluded.joined,
          website=excluded.website,
          url=excluded.url,
          collected_at=excluded.collected_at
        """,
        (
            screen_name,
            data.get("name") or "",
            data.get("description") or "",
            1 if data.get("verified") else 0,
            1 if data.get("protected") else 0,
            data.get("followers_count"),
            data.get("following_count"),
            i_follow_val,
            data.get("location") or "",
            data.get("joined") or "",
            data.get("website") or "",
            f"https://x.com/{screen_name}",
            now,
        ),
    )
    conn.execute("DELETE FROM posts WHERE screen_name=?", (screen_name,))
    for p in data.get("posts") or []:
        if not p.get("post_id"):
            continue
        conn.execute(
            "INSERT OR REPLACE INTO posts(screen_name, post_id, text, posted_at, url) VALUES(?, ?, ?, ?, ?)",
            (screen_name, p.get("post_id"), p.get("text") or "", p.get("posted_at") or "", p.get("url") or ""),
        )
    conn.commit()


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("json_path")
    p.add_argument("--db", default="/tmp/x_followers_gogo_tanaka.db")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--posts", type=int, default=10)
    p.add_argument("--skip-existing", action="store_true")
    p.add_argument("--max-wait", type=float, default=8.0)
    args = p.parse_args(argv)

    data = json.loads(pathlib.Path(args.json_path).read_text(encoding="utf-8"))
    users = data.get("all_users") or data.get("users") or []
    if args.limit > 0:
        users = users[: args.limit]

    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)
    if args.skip_existing:
        existing = {
            r[0] for r in conn.execute(
                "SELECT screen_name FROM profiles WHERE followers_count IS NOT NULL"
            ).fetchall()
        }
    else:
        existing = set()

    total = len(users)
    started = time.time()
    success = fail = skipped = 0
    print(f"[db] target {total} users → {args.db}", file=sys.stderr)
    for i, u in enumerate(users, 1):
        sn = u.get("screen_name")
        if not sn:
            continue
        if sn in existing:
            skipped += 1
            continue
        try:
            prof = fetch_profile(sn, args.posts, max_wait=args.max_wait)
        except Exception as e:
            print(f"[db] {i}/{total} @{sn}: ERR {e}", file=sys.stderr)
            fail += 1
            continue
        if not prof or not prof.get("ok"):
            print(f"[db] {i}/{total} @{sn}: MISS", file=sys.stderr)
            fail += 1
            continue
        upsert(conn, sn, prof)
        success += 1
        done = i - skipped
        elapsed = time.time() - started
        eta = (elapsed / done) * (total - i) if done else 0
        fc = prof.get("followers_count")
        i_f = prof.get("i_follow")
        mine = "✓" if i_f is True else ("·" if i_f is False else "?")
        n_posts = len(prof.get("posts") or [])
        print(
            f"[db] {i:>3}/{total} @{sn:24s} f={str(fc):>8s} mine={mine} posts={n_posts} eta={eta:.0f}s",
            file=sys.stderr,
        )

    print(f"[db] done. success={success} fail={fail} skipped={skipped} db={args.db}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
