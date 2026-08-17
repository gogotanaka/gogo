#!/usr/bin/env python3
"""Slack へのメッセージ送信 (chat.postMessage)。

apps/mf-pl/send_slack.py の bot トークン方式を踏襲（CDPフォールバックは省略、
このアプリでは bot token 必須にする）。bot は投稿先チャンネルに招待済みである必要がある。
"""
import json
import os
import urllib.request

from config import CONF_DIR

BOT_TOKEN_PATH = os.path.join(CONF_DIR, "slack_bot_token")


def _read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def _bot_token():
    return os.environ.get("SLACK_BOT_TOKEN") or _read_file(BOT_TOKEN_PATH)


def post(channel, text, thread_ts=None):
    token = _bot_token()
    if not token:
        raise RuntimeError(
            f"Slack bot token がありません。{BOT_TOKEN_PATH} に置くか"
            " SLACK_BOT_TOKEN を設定してください（bot は投稿先チャンネルに招待済みが必要）。"
        )
    body = {"channel": channel, "text": text}
    if thread_ts:
        body["thread_ts"] = thread_ts
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.loads(r.read())
    if not resp.get("ok"):
        raise RuntimeError(f"chat.postMessage failed: {resp.get('error')}")
