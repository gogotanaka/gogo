#!/usr/bin/env python3
"""Drive everyday Chrome via AppleScript to extract X followers.

Prereq: Chrome の View > Developer > Allow JavaScript from Apple Events を有効化。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
JS_PATH = SCRIPT_DIR / "extract_followers.js"


def osa_js(js: str) -> str:
    """Run JS in Chrome's active tab via AppleScript. Returns stdout text."""
    # AppleScript の literal にJSをそのまま埋めると quote escape が地獄なので、
    # JSをbase64でラップして atob() で復元する。
    import base64
    b64 = base64.b64encode(js.encode("utf-8")).decode("ascii")
    wrapped = (
        "(function(){"
        f"var s=atob('{b64}');"
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
    proc = subprocess.run(
        ["osascript", "-e", apple],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"osascript failed: {proc.stderr.strip()}")
    return proc.stdout.rstrip("\n")


def find_or_open_tab(url: str) -> None:
    """Activate (or open) a Chrome tab pointing at `url`."""
    apple = f'''
    tell application "Google Chrome"
      activate
      set targetURL to "{url}"
      set foundTab to false
      repeat with w in windows
        set i to 0
        repeat with t in tabs of w
          set i to i + 1
          if (URL of t) contains "{url}" then
            set active tab index of w to i
            set index of w to 1
            set foundTab to true
            exit repeat
          end if
        end repeat
        if foundTab then exit repeat
      end repeat
      if not foundTab then
        if (count of windows) = 0 then
          make new window
        end if
        tell window 1 to make new tab with properties {{URL:targetURL}}
      end if
    end tell
    '''
    subprocess.run(["osascript", "-e", apple], check=True, capture_output=True)


def wait_for_url_contains(needle: str, timeout: float = 20.0) -> str:
    end = time.time() + timeout
    last = ""
    while time.time() < end:
        try:
            last = osa_js("location.href")
        except Exception:
            last = ""
        if needle in last:
            return last
        time.sleep(0.5)
    raise TimeoutError(f"URL did not become {needle!r}, last={last!r}")


def run(username: str, min_followers: int, max_scrolls: int, limit: int,
        include_it: bool, out_path: pathlib.Path, kind: str = "followers") -> dict:
    assert kind in ("followers", "following")
    url = f"https://x.com/{username}/{kind}"
    print(f"[x-follower] opening {url}", file=sys.stderr)
    find_or_open_tab(url)
    wait_for_url_contains(f"/{kind}", timeout=30)
    # X はクライアント描画なので追加で少し待つ
    time.sleep(3.0)

    js = JS_PATH.read_text(encoding="utf-8")
    js = (
        js.replace("MIN_FOLLOWERS_PLACEHOLDER", str(int(min_followers)))
          .replace("MAX_SCROLLS_PLACEHOLDER", str(int(max_scrolls)))
          .replace("LIMIT_PLACEHOLDER", str(int(limit)))
          .replace("INCLUDE_IT_PLACEHOLDER", "true" if include_it else "false")
    )

    print("[x-follower] starting extraction", file=sys.stderr)
    started = osa_js(js)
    print(f"[x-follower] kickoff: {started!r}", file=sys.stderr)

    # Poll for completion
    deadline = time.time() + 240  # 4 min
    last_progress = ""
    while time.time() < deadline:
        try:
            done = osa_js("String(!!window.__x_followers_done)")
            prog = osa_js(
                "JSON.stringify(window.__x_followers_progress||{})"
            )
        except Exception as e:
            print(f"[x-follower] poll error: {e}", file=sys.stderr)
            time.sleep(1.0)
            continue
        if prog != last_progress:
            print(f"[x-follower] progress {prog}", file=sys.stderr)
            last_progress = prog
        if done == "true":
            break
        time.sleep(1.5)
    else:
        raise TimeoutError("extraction did not finish in time")

    raw = osa_js("JSON.stringify(window.__x_followers_result||{})")
    data = json.loads(raw)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[x-follower] wrote {out_path}", file=sys.stderr)
    return data


def format_report(data: dict, top_all: int = 0) -> str:
    if data.get("error"):
        return f"ERROR: {data['error']}"
    users = data.get("users", [])
    lines = []
    lines.append(
        f"# 取得 {data['total_collected']}人 / マッチ {data['matched_count']}人 "
        f"(params: {data['params']})"
    )
    if data.get("note"):
        lines.append(f"# note: {data['note']}")
    lines.append("")
    for i, u in enumerate(users, 1):
        verified = " ✔" if u.get("verified") else ""
        bio = (u.get("description") or "").replace("\n", " ")[:160]
        reason = u.get("reason") or "-"
        lines.append(
            f"{i:>3}. @{u['screen_name']}（{u.get('name','')}{verified}） [{reason}]"
        )
        if bio:
            lines.append(f"     bio: {bio}")
        lines.append(f"     {u.get('url', '')}")
    if top_all:
        lines.append("")
        lines.append(f"--- 全収集ユーザー先頭{top_all}件 (画面表示順=最近フォローした順) ---")
        for i, u in enumerate(data.get("all_users", [])[:top_all], 1):
            v = " ✔" if u.get("verified") else ""
            lines.append(
                f"{i:>3}. @{u['screen_name']}{v} - "
                f"{(u.get('description') or '')[:90]}"
            )
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("username", help="X username (without @)")
    p.add_argument("--kind", choices=["followers", "following"], default="followers")
    p.add_argument("--min-followers", type=int, default=300)
    p.add_argument("--max-scrolls", type=int, default=30)
    p.add_argument("--limit", type=int, default=0,
                   help="直近何人まで収集対象にするか (0=無制限)")
    p.add_argument("--no-it", action="store_true",
                   help="IT系キーワード判定を無効化")
    p.add_argument("--out", default="",
                   help="JSON出力先 (default: /tmp/x_<kind>_<user>.json)")
    p.add_argument("--top-all", type=int, default=0,
                   help="全収集ユーザー中フォロワー上位N人をレポート末尾に追加")
    args = p.parse_args(argv)

    out_path = pathlib.Path(
        args.out or f"/tmp/x_{args.kind}_{args.username}.json"
    )
    data = run(
        username=args.username,
        min_followers=args.min_followers,
        max_scrolls=args.max_scrolls,
        limit=args.limit,
        include_it=not args.no_it,
        out_path=out_path,
        kind=args.kind,
    )
    print(format_report(data, top_all=args.top_all))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
