#!/usr/bin/env python3
"""to.zozo.jp 分析 > 売上集計 から指定日の krähe 売上を取得し Slack 投稿用テキストを出力する。

usage: ./fetch_sales.py [YYYY-MM-DD] [--json]
  日付省略時は昨日 (JST)。デフォルトは Slack 投稿用テキスト、--json で集計 JSON。

必要な config/ (gitignored):
  basic_auth   … to.zozo.jp の Basic 認証 user:pass
  portal_login … ZOZO BACK OFFICE のログイン user:pass
"""
import html.parser
import http.cookiejar
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://to.zozo.jp"
SHOP_ID = "3326"       # Avenue
SCATEGORY_PID = "40469"  # krahe
CONF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
JST = timezone(timedelta(hours=9))
WEEKDAYS = "月火水木金土日"


def read_conf(name):
    with open(os.path.join(CONF_DIR, name)) as f:
        return f.read().strip()


class TableParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self._row = []
        self._cell = []
        self._in_cell = False
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._depth += 1
        elif tag == "tr" and self._depth:
            self._row = []
        elif tag in ("td", "th") and self._depth:
            self._in_cell = True
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "table":
            self._depth -= 1
        elif tag == "tr" and self._depth:
            if self._row:
                self.rows.append(self._row[:])
        elif tag in ("td", "th") and self._in_cell:
            self._row.append(" ".join(self._cell).strip())
            self._in_cell = False

    def handle_data(self, data):
        if self._in_cell:
            t = data.strip()
            if t:
                self._cell.append(t)


class Portal:
    def __init__(self):
        user, _, pw = read_conf("basic_auth").partition(":")
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, BASE, user, pw)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPBasicAuthHandler(mgr),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def fetch(self, path, data=None):
        url = BASE + path
        headers = {"User-Agent": "Mozilla/5.0"}
        if data is not None:
            data = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, headers=headers)
        with self.opener.open(req, timeout=120) as resp:
            body = resp.read()
            for enc in ("utf-8", "shift_jis", "cp932"):
                try:
                    return body.decode(enc)
                except UnicodeDecodeError:
                    pass
            return body.decode("utf-8", errors="replace")

    def login(self):
        text = self.fetch("/to/")
        m = re.search(r'name="csrf_token"\s+value="([^"]+)"', text)
        if not m:
            sys.exit("[zozo-sales] ログインページに csrf_token が見つかりません")
        user, _, pw = read_conf("portal_login").partition(":")
        text = self.fetch("/to/Default.asp", {
            "c": "Login", "csrf_token": m.group(1), "redirect_uri": "",
            "zozo-app-os": "", "zozo-app-os-ver": "", "zozo-app-name": "",
            "zozo-app-ver": "", "LoginName": user, "Password": pw, "TerminalID": "",
        })
        if b"LoginName" in text.encode() and "main.asp" not in text:
            sys.exit("[zozo-sales] ログインに失敗しました (ID/パスワードを確認してください)")

    def search_summary(self, date):
        """date: 'YYYY/MM/DD'。売上集計検索を実行しセッションを確立する。"""
        self.fetch("/to/Sales.asp?c=SalesSummary")
        text = self.fetch("/to/Sales.asp", {
            "c": "Search", "search": "SEARCH",
            "ShopID": SHOP_ID, "SCategoryPID": SCATEGORY_PID, "SCategoryID": "0",
            "CustomerTypeID": "0", "TypeCategoryID": "0", "TypeID": "0",
            "ViewType": "1", "DailyFlag": "1", "TeikibinCheck": "0", "MallCheck": "0",
            "TermFrom": date, "TermTo": date,
            "ViewList": "1", "OldFlag": "0", "HasOrderNumber": "0",
        })
        return text

    def get_detail(self, date):
        """date: 'YYYY/MM/DD'。商品別売上詳細を返す。"""
        path = f"/to/Sales.asp?c=SalesSummary_Detail&ReportDate={date}&DetailDivision=1"
        return self.fetch(path)


def parse_summary(text, date):
    p = TableParser()
    p.feed(text)
    for row in p.rows:
        if row and row[0] == date:
            qty_str = row[1].replace(",", "") if len(row) > 1 else "0"
            amt_str = row[2].replace(",", "") if len(row) > 2 else "0"
            return {
                "total_qty": int(qty_str) if qty_str.isdigit() else 0,
                "total_amount": int(amt_str) if amt_str.isdigit() else 0,
            }
    return {"total_qty": 0, "total_amount": 0}


def parse_detail(text):
    p = TableParser()
    p.feed(text)
    products = []
    for row in p.rows[2:]:  # skip header rows
        if len(row) < 11:
            continue
        name = row[4]
        code = row[3]
        price_type = row[8]   # 通常 or セール
        qty_str = row[9].replace(",", "")
        amt_str = row[10].replace(",", "")
        if not qty_str.isdigit():
            continue
        products.append({
            "name": name,
            "code": code,
            "price_type": price_type,
            "qty": int(qty_str),
            "amount": int(amt_str),
        })
    return sorted(products, key=lambda p: -p["amount"])


def summarize(date, summary, products):
    return {
        "date": date,
        "total_qty": summary["total_qty"],
        "total_amount": summary["total_amount"],
        "products": products,
    }


def format_slack(s):
    d = datetime.strptime(s["date"], "%Y/%m/%d")
    wd = WEEKDAYS[d.weekday()]
    lines = [
        f":shopping_bags: *ZOZOTOWN krähe 売上 {s['date']}({wd})*",
        f"*¥{s['total_amount']:,}*（税抜） / {s['total_qty']:,}点",
        "",
        "*商品別*",
    ]
    for p in s["products"][:20]:
        label = "🏷" if p["price_type"] == "セール" else ""
        lines.append(f"• {label}{p['name']} `{p['code']}` — ¥{p['amount']:,} ({p['qty']}点)")
    rest = s["products"][20:]
    if rest:
        rest_amt = sum(p["amount"] for p in rest)
        lines.append(f"　他 {len(rest)}商品 — ¥{rest_amt:,}")
    return "\n".join(lines)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    as_json = "--json" in sys.argv
    if args:
        date = datetime.strptime(args[0], "%Y-%m-%d").strftime("%Y/%m/%d")
    else:
        date = (datetime.now(JST) - timedelta(days=1)).strftime("%Y/%m/%d")

    portal = Portal()
    portal.login()
    summary_text = portal.search_summary(date)
    summary = parse_summary(summary_text, date)
    detail_text = portal.get_detail(date)
    products = parse_detail(detail_text)
    s = summarize(date, summary, products)

    if as_json:
        print(json.dumps(s, ensure_ascii=False, indent=1))
    else:
        print(format_slack(s))


if __name__ == "__main__":
    main()
