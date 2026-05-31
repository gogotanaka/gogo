#!/usr/bin/env python3
"""エンリッチ済みJSONに対して follower_count しきい値+IT系キーワードでフィルタしレポート。

Usage:
  python3 filter_report.py /tmp/x_followers_gogo_tanaka.json \
    [--min-followers 300] [--no-it] [--top N] [--category CAT]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

IT_KEYWORDS = [
    r"engineer", r"developer", r"\bCTO\b", r"\bSRE\b", r"devops", r"backend",
    r"frontend", r"full[- ]?stack", r"software", r"infrastructure", r"platform",
    r"machine learning", r"\bML\b", r"\bAI\b", r"data scientist", r"\bSWE\b",
    r"researcher", r"研究", r"エンジニア", r"開発", r"プログラマ",
    r"product manager", r"\bPM\b", r"プロダクトマネージャ", r"プロダクトマネジャ",
    r"\bCPO\b",
    r"marketing", r"marketer", r"\bCMO\b", r"growth", r"マーケティング", r"マーケター",
    r"founder", r"\bCEO\b", r"\bCOO\b", r"entrepreneur", r"起業", r"創業", r"代表取締役",
    r"経営者",
    r"designer", r"デザイナー", r"UI/UX", r"UX",
    r"investor", r"\bVC\b", r"venture", r"投資家", r"キャピタリスト",
]

CATEGORY_PATTERNS = {
    "engineer": [r"engineer", r"developer", r"\bCTO\b", r"\bSRE\b", r"devops", r"backend",
                 r"frontend", r"full[- ]?stack", r"software", r"infrastructure",
                 r"platform", r"\bSWE\b", r"エンジニア", r"開発", r"プログラマ"],
    "ai": [r"machine learning", r"\bML\b", r"\bAI\b", r"data scientist", r"researcher",
           r"research", r"研究"],
    "pm": [r"product manager", r"\bPM\b", r"プロダクトマネージャ", r"プロダクトマネジャ",
           r"\bCPO\b", r"プロダクト責任"],
    "marketing": [r"marketing", r"marketer", r"\bCMO\b", r"growth", r"マーケティング",
                  r"マーケター", r"グロース"],
    "founder": [r"founder", r"\bCEO\b", r"\bCOO\b", r"entrepreneur", r"起業", r"創業",
                r"代表取締役", r"代表 ", r"^代表", r"経営者"],
    "designer": [r"designer", r"デザイナー", r"UI/UX", r"UX"],
    "investor": [r"investor", r"\bVC\b", r"venture", r"投資家", r"キャピタリスト"],
}


def matches_it(bio: str) -> bool:
    if not bio:
        return False
    return any(re.search(p, bio, re.IGNORECASE) for p in IT_KEYWORDS)


def matches_category(bio: str, cat: str) -> bool:
    pats = CATEGORY_PATTERNS.get(cat)
    if not pats:
        return False
    return any(re.search(p, bio or "", re.IGNORECASE) for p in pats)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("json_path")
    p.add_argument("--min-followers", type=int, default=0,
                   help="フォロワー数しきい値 (要 enrich 済み)")
    p.add_argument("--no-it", action="store_true",
                   help="IT系キーワードフィルタを無効化")
    p.add_argument("--top", type=int, default=0,
                   help="上位N件のみ表示 (0=全件)")
    p.add_argument("--category",
                   choices=list(CATEGORY_PATTERNS.keys()),
                   help="特定カテゴリのみ表示")
    p.add_argument("--include-missing", action="store_true",
                   help="follower_count 取得失敗(MISS)も含める")
    p.add_argument("--sort", choices=["followers", "screen_name", "order"],
                   default="followers",
                   help="ソート (followers=多い順, order=画面表示順)")
    args = p.parse_args(argv)

    data = json.loads(pathlib.Path(args.json_path).read_text(encoding="utf-8"))
    users = data.get("all_users") or []
    enriched = sum(1 for u in users if u.get("followers_count") is not None)
    print(f"# {args.json_path}: 全 {len(users)}人 / enrich済 {enriched}人")
    print()

    filtered = []
    for u in users:
        fc = u.get("followers_count")
        if fc is None and not args.include_missing:
            if args.min_followers > 0:
                continue
        if args.min_followers > 0 and (fc is None or fc < args.min_followers):
            continue
        bio = u.get("description", "") or ""
        if args.category and not matches_category(bio, args.category):
            continue
        if not args.no_it and not args.category and not matches_it(bio):
            # IT系フィルタ default-on
            if args.min_followers == 0:
                continue
            # min-followers指定時はIT系縛りは外す（フォロワー数で絞れているため）
        filtered.append(u)

    if args.sort == "followers":
        filtered.sort(key=lambda u: u.get("followers_count") or -1, reverse=True)
    elif args.sort == "screen_name":
        filtered.sort(key=lambda u: u.get("screen_name", ""))
    # order: keep as-is (画面表示順=フォロー新しい順)

    if args.top:
        filtered = filtered[: args.top]

    label = []
    if args.min_followers:
        label.append(f"フォロワー>={args.min_followers}")
    if args.category:
        label.append(f"category={args.category}")
    elif not args.no_it:
        label.append("IT系")
    print(f"## マッチ {len(filtered)}人  [{', '.join(label) or 'no-filter'}]")
    print()

    for i, u in enumerate(filtered, 1):
        fc = u.get("followers_count")
        fc_s = f"{fc:>7,}" if fc is not None else "    ???"
        v = "✔" if u.get("verified") else " "
        bio = (u.get("description") or "").replace("\n", " ")[:140]
        name = (u.get("name") or "")[:32]
        print(f"{i:>3}. {fc_s} {v} @{u['screen_name']:24s} {name}")
        if bio:
            print(f"           {bio}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
