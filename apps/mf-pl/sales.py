#!/usr/bin/env python3
"""全社の当月の指定項目（売上高・売上総利益・当期純利益など）を1通にまとめた
Slack メッセージを標準出力に出す。

各プロファイルの trial_balance_pl から項目を抜き出し、金額の大きい順 + 合計行で整形。
Slack のコードブロックでは全角文字の幅が半角2文字分にならず桁が崩れるため、
金額（ASCII）を左に右詰めで置き、社名を右に置くレイアウトにする。

Usage:
  sales.py                        # 全社・先月分の売上高
  sales.py --item 売上総利益
  sales.py --item 当期純利益 --month 2026-06
"""
import argparse
import sys

import auth
import fetch_pl
from format_pl import amount, norm, walk, yen

EMOJI = {
    "売上高": ":moneybag:",
    "売上総利益": ":chart_with_upwards_trend:",
    "営業利益": ":chart_with_upwards_trend:",
    "経常利益": ":chart_with_upwards_trend:",
    "当期純利益": ":yen:",
}


def pick(report, item):
    """当月発生額 = 期末残高 - 前期残高（期末残高は期首からの累計のため）"""
    for depth, row in walk(report.get("rows")):
        if norm(row.get("name")) == item:
            return amount(row, "pl")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", help="対象月 YYYY-MM（省略時は先月）")
    ap.add_argument("--item", default="売上高",
                    help="集計する項目名（売上高 / 売上総利益 / 営業利益 / 当期純利益 など）")
    args = ap.parse_args()

    month = args.month or fetch_pl.previous_month()
    start, end = fetch_pl.month_range(month)

    rows = []
    for profile in auth.list_profiles():
        token = auth.get_access_token(profile)
        office = fetch_pl.api_get("/api/v3/offices", token)
        report = fetch_pl.api_get("/api/v3/reports/trial_balance_pl", token,
                                  {"start_date": start, "end_date": end})
        rows.append((office.get("name", profile), pick(report, args.item) or 0))
        print(f"  {profile}: ok", file=sys.stderr)

    rows.sort(key=lambda r: r[1], reverse=True)
    total = sum(v for _, v in rows)

    amount_w = max(len(yen(v)) for _, v in rows + [("", total)])
    lines = [f"{yen(v):>{amount_w}}  {n}" for n, v in rows]
    lines.append("-" * amount_w)
    lines.append(f"{yen(total):>{amount_w}}  合計")

    y, m = month.split("-")
    emoji = EMOJI.get(args.item, ":bar_chart:")
    print(f"{emoji} *{int(y)}年{int(m)}月 {args.item}*\n```\n" + "\n".join(lines) + "\n```")


if __name__ == "__main__":
    main()
