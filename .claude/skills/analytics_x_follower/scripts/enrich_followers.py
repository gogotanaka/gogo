#!/usr/bin/env python3
"""各プロフィールページをChromeアクティブタブで順次開いて follower_count を取得。

Usage:
  python3 enrich_followers.py /tmp/x_followers_gogo_tanaka.json [--limit 0] [--skip-with-count]

結果は同じJSONファイルにマージして上書き保存（`followers_count` フィールドを追加）。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import time


def osa_js(js: str) -> str:
    apple = (
        'tell application "Google Chrome"\n'
        "  set theResult to execute active tab of window 1 javascript {js}\n"
        "  return theResult\n"
        "end tell"
    ).replace("{js}", json.dumps(js))
    proc = subprocess.run(["osascript", "-e", apple], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"osascript failed: {proc.stderr.strip()}")
    return proc.stdout.rstrip("\n")


def osa_set_url(url: str) -> None:
    apple = (
        f'tell application "Google Chrome" to set URL of active tab of window 1 to "{url}"'
    )
    subprocess.run(["osascript", "-e", apple], check=True, capture_output=True)


_FOLLOWERS_RE = re.compile(r"([\d,\.]+(?:[KMB]|万|千)?)\s*(?:Followers|フォロワー)", re.I)
_NUM_SUFFIX = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000, "万": 10_000, "千": 1_000}


def parse_followers_text(text: str) -> int | None:
    if not text:
        return None
    m = _FOLLOWERS_RE.search(text)
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    suffix = ""
    if raw and raw[-1] in _NUM_SUFFIX:
        suffix = raw[-1]
        raw = raw[:-1]
    try:
        val = float(raw)
    except ValueError:
        return None
    if suffix:
        val *= _NUM_SUFFIX[suffix]
    return int(val)


def fetch_follower_count(screen_name: str, max_wait: float = 6.0) -> int | None:
    osa_set_url(f"https://x.com/{screen_name}")
    deadline = time.time() + max_wait
    last_text = ""
    while time.time() < deadline:
        time.sleep(0.6)
        try:
            txt = osa_js(
                '(function(){'
                'var a=document.querySelector("a[href*=\\"verified_followers\\"]");'
                'if(!a){a=document.querySelector("a[href$=\\"/followers\\"]");}'
                'if(!a)return "";'
                'return a.innerText||"";'
                '})()'
            )
        except Exception:
            txt = ""
        if txt and txt != "none":
            last_text = txt
            n = parse_followers_text(txt)
            if n is not None:
                return n
    # final attempt parse
    return parse_followers_text(last_text)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("json_path")
    p.add_argument("--limit", type=int, default=0,
                   help="先頭N件のみエンリッチ (0=全件)")
    p.add_argument("--skip-with-count", action="store_true",
                   help="既に followers_count があるユーザーをスキップ")
    p.add_argument("--max-wait", type=float, default=6.0,
                   help="1ユーザーあたりの最大待ち秒")
    args = p.parse_args(argv)

    path = pathlib.Path(args.json_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    users = data.get("all_users") or data.get("users") or []
    if args.limit > 0:
        targets = users[: args.limit]
    else:
        targets = users

    total = len(targets)
    print(f"[enrich] target {total} users from {path}", file=sys.stderr)

    success = 0
    fail = 0
    started = time.time()
    for i, u in enumerate(targets, 1):
        if args.skip_with_count and "followers_count" in u and u["followers_count"] is not None:
            continue
        sn = u.get("screen_name")
        if not sn:
            continue
        try:
            n = fetch_follower_count(sn, max_wait=args.max_wait)
        except Exception as e:
            print(f"[enrich] {i}/{total} @{sn}: error {e}", file=sys.stderr)
            fail += 1
            u["followers_count"] = None
            continue
        u["followers_count"] = n
        if n is None:
            fail += 1
            tag = "MISS"
        else:
            success += 1
            tag = f"{n:,}"
        elapsed = time.time() - started
        eta = (elapsed / i) * (total - i) if i else 0
        print(
            f"[enrich] {i:>3}/{total} @{sn:24s} {tag:>10s}   elapsed={elapsed:.0f}s eta={eta:.0f}s",
            file=sys.stderr,
        )
        # save progress every 25
        if i % 25 == 0:
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[enrich] done. success={success} fail={fail} wrote {path}", file=sys.stderr
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
