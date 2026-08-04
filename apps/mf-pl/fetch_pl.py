#!/usr/bin/env python3
"""Fetch a monthly PL/BS (残高試算表) from Money Forward Cloud Accounting API v3.

対象月の start_date/end_date を渡して残高試算表(PL または BS)の JSON を標準出力に出す。
会社名 (_office_name)・対象月 (_month)・種別 (_report) をレスポンスに埋め込む。
認証は auth.py (OAuth 2.0、プロファイル別) 経由。

Usage:
  fetch_pl.py --profile aisaac                 # 先月分の PL
  fetch_pl.py --profile aisaac --report bs     # 先月末の BS
  fetch_pl.py --profile aisaac --month 2026-06
  fetch_pl.py --profile aisaac --check         # 認証の疎通確認（事業者情報を表示）
"""
import argparse
import calendar
import datetime
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

import auth

API_BASE = "https://api-accounting.moneyforward.com"


def api_get(path, token, params=None):
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        sys.exit(f"[mf-pl] HTTP {e.code} {url}\n{body}")


def month_range(month):
    """'2026-06' -> ('2026-06-01', '2026-06-30')"""
    y, m = (int(x) for x in month.split("-"))
    last = calendar.monthrange(y, m)[1]
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}"


def previous_month():
    first = datetime.date.today().replace(day=1)
    prev = first - datetime.timedelta(days=1)
    return f"{prev.year:04d}-{prev.month:02d}"


def resolve_profile(name):
    if name:
        return name
    profiles = auth.list_profiles()
    if len(profiles) == 1:
        return profiles[0]
    sys.exit(f"[mf-pl] --profile を指定してください。登録済み: {', '.join(profiles) or '(なし)'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", help="会社プロファイル名（1社だけ登録済みなら省略可）")
    ap.add_argument("--month", help="対象月 YYYY-MM（省略時は先月）")
    ap.add_argument("--report", choices=["pl", "bs"], default="pl",
                    help="pl: 損益計算書 / bs: 貸借対照表")
    ap.add_argument("--check", action="store_true",
                    help="認証の疎通確認（事業者情報を表示）")
    args = ap.parse_args()

    profile = resolve_profile(args.profile)
    token = auth.get_access_token(profile)
    office = api_get("/api/v3/offices", token)

    if args.check:
        print(json.dumps(office, ensure_ascii=False, indent=2))
        return

    month = args.month or previous_month()
    start, end = month_range(month)
    report = api_get(f"/api/v3/reports/trial_balance_{args.report}", token,
                     {"start_date": start, "end_date": end})
    report["_month"] = month
    report["_report"] = args.report
    report["_office_name"] = office.get("name", profile)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
