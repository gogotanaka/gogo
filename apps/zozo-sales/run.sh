#!/bin/sh
# 昨日 (JST) の ZOZOTOWN 売上サマリを Slack に投稿する。launchd から毎朝 9:00 に呼ばれる。
set -eu
cd "$(dirname "$0")"
./fetch_sales.py | ./send_slack.py
