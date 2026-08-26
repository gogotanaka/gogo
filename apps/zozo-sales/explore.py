#!/usr/bin/env python3
"""to.zozo.jp にログインした状態でページを取得する開発用スクリプト。

usage: ./explore.py [path]        # デフォルトはログイン直後のページ
       ./explore.py '/to/Default.asp?c=Xxx'
"""
import html.parser
import http.cookiejar
import os
import re
import sys
import urllib.parse
import urllib.request

BASE = "https://to.zozo.jp"
CONF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")


def read_conf(name):
    path = os.path.join(CONF_DIR, name)
    with open(path) as f:
        return f.read().strip()


class PageDumper(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.links = []
        self.title = ""
        self._in_title = False
        self._texts = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "form":
            self.forms.append({"action": d.get("action"), "method": d.get("method"), "inputs": []})
        elif tag in ("input", "select") and self.forms:
            self.forms[-1]["inputs"].append(
                {k: d.get(k) for k in ("type", "name", "value") if d.get(k) is not None})
        elif tag == "a" and d.get("href"):
            self.links.append(d["href"])

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        t = data.strip()
        if t:
            self._texts.append(t)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False


def decode(body):
    for enc in ("utf-8", "shift_jis", "cp932", "euc-jp"):
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


class Session:
    def __init__(self):
        basic = read_conf("basic_auth")
        user, _, pw = basic.partition(":")
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, BASE, user, pw)
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPBasicAuthHandler(mgr),
            urllib.request.HTTPCookieProcessor(self.cj))

    def fetch(self, path, data=None):
        url = path if path.startswith("http") else BASE + path
        headers = {"User-Agent": "Mozilla/5.0"}
        if data is not None:
            data = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, headers=headers)
        with self.opener.open(req, timeout=30) as resp:
            return resp.geturl(), decode(resp.read())

    def login(self):
        _, text = self.fetch("/to/")
        m = re.search(r'name="csrf_token"\s+value="([^"]+)"', text)
        if not m:
            sys.exit("ログインページに csrf_token が見つかりません")
        login = read_conf("portal_login")
        user, _, pw = login.partition(":")
        url, text = self.fetch("/to/Default.asp", {
            "c": "Login", "csrf_token": m.group(1), "redirect_uri": "",
            "zozo-app-os": "", "zozo-app-os-ver": "", "zozo-app-name": "",
            "zozo-app-ver": "", "LoginName": user, "Password": pw, "TerminalID": "",
        })
        return url, text


def dump(url, text, save="last_page.html"):
    p = PageDumper()
    p.feed(text)
    print(f"URL: {url}")
    print(f"TITLE: {p.title.strip()}")
    print(f"\nFORMS ({len(p.forms)}):")
    for f in p.forms:
        print(f"  action={f['action']} method={f['method']}")
        for i in f["inputs"]:
            print(f"    {i}")
    print(f"\nLINKS ({len(p.links)}):")
    seen = set()
    for l in p.links:
        if l not in seen:
            seen.add(l)
            print(f"  {l}")
    print("\nTEXT (first 80 fragments):")
    print("  " + " | ".join(p._texts[:80]))
    out = os.path.join(CONF_DIR, save)
    with open(out, "w") as f:
        f.write(text)
    print(f"\n(raw html saved to {out})")


def main():
    s = Session()
    url, text = s.login()
    if len(sys.argv) > 1:
        url, text = s.fetch(sys.argv[1])
    dump(url, text)


if __name__ == "__main__":
    main()
