#!/usr/bin/env python3
"""to.zozo.jp の中身を調査するためのプローブ。

config/basic_auth (user:pass 形式) を使って Basic 認証を通し、
トップページ / ログインフォームの構造をダンプする。
config/portal_login (user:pass 形式) があればフォームログインも試す。

usage: ./probe.py [path]
"""
import html.parser
import os
import sys
import urllib.parse
import urllib.request

BASE = "https://to.zozo.jp"
CONF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")


def read_conf(name):
    path = os.path.join(CONF_DIR, name)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return f.read().strip()


class FormDumper(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.links = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "form":
            self.forms.append({"action": d.get("action"), "method": d.get("method"), "inputs": []})
        elif tag == "input" and self.forms:
            self.forms[-1]["inputs"].append(
                {k: d.get(k) for k in ("type", "name", "value") if d.get(k) is not None})
        elif tag == "a" and d.get("href"):
            self.links.append(d["href"])

    def handle_data(self, data):
        if self._in_title:
            self.title += data

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False


def build_opener():
    basic = read_conf("basic_auth")
    if not basic:
        sys.exit(f"config/basic_auth (user:pass) を置いてください: {CONF_DIR}/basic_auth")
    user, _, pw = basic.partition(":")
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, BASE, user, pw)
    cj = urllib.request.HTTPCookieProcessor()
    return urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(mgr), cj)


def fetch(opener, path, data=None):
    url = BASE + path
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "Mozilla/5.0"})
    with opener.open(req, timeout=30) as resp:
        body = resp.read()
        for enc in ("utf-8", "shift_jis", "cp932", "euc-jp"):
            try:
                return resp.geturl(), body.decode(enc)
            except UnicodeDecodeError:
                continue
        return resp.geturl(), body.decode("utf-8", errors="replace")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "/"
    opener = build_opener()
    url, text = fetch(opener, path)
    p = FormDumper()
    p.feed(text)
    print(f"URL: {url}")
    print(f"TITLE: {p.title.strip()}")
    print(f"\nFORMS ({len(p.forms)}):")
    for f in p.forms:
        print(f"  action={f['action']} method={f['method']}")
        for i in f["inputs"]:
            print(f"    {i}")
    print(f"\nLINKS ({len(p.links)}):")
    for l in p.links[:60]:
        print(f"  {l}")
    out = os.path.join(CONF_DIR, "last_page.html")
    with open(out, "w") as f:
        f.write(text)
    print(f"\n(raw html saved to {out})")


if __name__ == "__main__":
    main()
