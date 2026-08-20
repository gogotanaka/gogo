#!/usr/bin/env python3
"""Slack メンション経由の注文コマンド受信 (Events API, HTTP Request URL)。

例: `@gogo buy 3930 200 742` のように、このアプリのSlack botにメンションで
話しかけると発注できる。web.py の `/slack/events` にSlackがHTTPでPOSTしてくる
イベントを handle_event() で処理する（Socket Modeは接続はできるのにイベントが
実際には配送されない現象が解消できなかったため、HTTP方式に切り替えた）。

安全のため:
- 発言者が SLACK_MENTION_USER と一致しない場合は何もせず、権限が無い旨を返信するだけ
- 書式が厳密に一致しない場合は何もせず、使い方を返信するだけ（曖昧な自然文解析はしない）
- 金額の上限（SBI_MAX_ORDER_VALUE_YEN）を超える注文は拒否する（誤入力の暴走を防ぐ）
- Slackの署名（X-Slack-Signature）を検証し、本物のSlackからのリクエストであることを
  確認する（apps/mf-pl等と同様、このリポジトリはSlack SDK等のフレームワークを使わず
  標準ライブラリで直接HTTP/署名検証を行う流儀にしている）

このモジュールはSlackイベントの受信・検証・返信だけを行い、実際の発注（Playwright操作）
は一切行わない。パース済みコマンドは on_command コールバック経由で web.py 側の
キューに渡し、SBI/ブラウザに触る唯一のスレッド（_sbi_loop）がそこから処理する。
"""
import hashlib
import hmac
import json
import os
import re
import threading
import time

from config import CONF_DIR, ENV

SIGNING_SECRET_PATH = os.path.join(CONF_DIR, "slack_signing_secret")

_SIDE_MAP = {
    "買い": "buy", "買": "buy", "buy": "buy",
    "売り": "sell", "売": "sell", "sell": "sell",
}
_COMMAND_RE = re.compile(
    r"^\s*(買い|買|buy|売り|売|sell)\s+(\d+)\s+(\d+)\s+([\d.]+)\s*$",
    re.IGNORECASE,
)
# `clear all` / `clear-all` の両方を受ける（言語仕様はハイフン、旧来の空白も互換）
_CLEAR_ALL_RE = re.compile(r"^\s*clear[\s-]+all\s*$", re.IGNORECASE)
_BOOK_RE = re.compile(r"^\s*book(?:\s+(\d+))?\s*$", re.IGNORECASE)
_WATCH_RE = re.compile(r"^\s*watch\s+(\d+)\s+(\d+)\s+([\d.]+)\s+(\d+)\s*$", re.IGNORECASE)
_WATCH_OPEN_RE = re.compile(
    r"^\s*watch-open\s+(買い|買|buy)\s+(\d+)\s+(\d+)\s+([\d.]+)\s*$", re.IGNORECASE)
_UNWATCH_RE = re.compile(r"^\s*unwatch\s+(\d+)\s*$", re.IGNORECASE)
_UNWATCH_OPEN_RE = re.compile(r"^\s*unwatch-open\s+(\d+)\s*$", re.IGNORECASE)
USAGE = (
    "書式が正しくありません。\n"
    "• `buy 3930 300 744` / `sell 3930 300 744` … 一回きりの指値注文（銘柄 株数 価格）\n"
    "• `watch 3930 400 760 900` … 銘柄・平均株数(±30%)・上限価格・平均間隔秒(±30%)で"
    "rebid（その銘柄の未約定注文を取消して最良買気配に買い指値）を続ける。`unwatch 3930` で解除\n"
    "• `watch-open buy 3930 400 760` … 毎営業日8:59〜9:05に20秒毎のrebid。"
    "`unwatch-open 3930` で解除\n"
    "• `clear-all` … 未約定注文を全取消\n"
    "• `book` / `book 3930` … 板情報を投稿"
)


def _read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def signing_secret():
    return os.environ.get("SLACK_SIGNING_SECRET") or _read_file(SIGNING_SECRET_PATH)


def enabled():
    return bool(signing_secret() and ENV.get("SLACK_MENTION_USER"))


def _strip_mention(text, bot_user_id):
    return re.sub(rf"<@{re.escape(bot_user_id)}>", "", text).strip()


def parse_command(text, bot_user_id):
    """メンション本文からコマンドを取り出す。書式に合わなければ None。"""
    m = _COMMAND_RE.match(_strip_mention(text, bot_user_id))
    if not m:
        return None
    side_raw, ticker, qty, price = m.groups()
    return {
        "side": _SIDE_MAP[side_raw.lower()],
        "ticker": ticker,
        "qty": int(qty),
        "price": float(price),
    }


def is_clear_all(text, bot_user_id):
    """`clear all` という厳密な文字列（大文字小文字は無視）かどうか。"""
    return bool(_CLEAR_ALL_RE.match(_strip_mention(text, bot_user_id)))


def parse_watch(text, bot_user_id):
    """`watch 3930 400 760 900`（銘柄 平均株数 上限価格 平均間隔秒）を解析する。"""
    m = _WATCH_RE.match(_strip_mention(text, bot_user_id))
    if not m:
        return None
    return {
        "ticker": m.group(1),
        "avg_qty": int(m.group(2)),
        "price_cap": float(m.group(3)),
        "avg_interval_sec": int(m.group(4)),
    }


def parse_watch_open(text, bot_user_id):
    """`watch-open buy 3930 400 760`（buy 銘柄 株数 上限価格）を解析する。

    rebid は「買い板の一番上に買いを入れる」動作なので side は buy のみ受ける。
    """
    m = _WATCH_OPEN_RE.match(_strip_mention(text, bot_user_id))
    if not m:
        return None
    return {
        "side": "buy",
        "ticker": m.group(2),
        "qty": int(m.group(3)),
        "price_cap": float(m.group(4)),
    }


def parse_unwatch(text, bot_user_id):
    """`unwatch 3930` を解析して銘柄コードを返す。合わなければ None。"""
    m = _UNWATCH_RE.match(_strip_mention(text, bot_user_id))
    return m.group(1) if m else None


def parse_unwatch_open(text, bot_user_id):
    """`unwatch-open 3930` を解析して銘柄コードを返す。合わなければ None。"""
    m = _UNWATCH_OPEN_RE.match(_strip_mention(text, bot_user_id))
    return m.group(1) if m else None


def parse_book(text, bot_user_id):
    """`book` または `book 3930` を解析する。書式に合わなければ None。

    戻り値: {'ticker': '3930'} または {'ticker': None}（銘柄未指定 = SBI_WATCH_TICKERS全部）
    """
    m = _BOOK_RE.match(_strip_mention(text, bot_user_id))
    if not m:
        return None
    return {"ticker": m.group(1)}


def verify_signature(headers, raw_body):
    """Slackの署名検証 (https://api.slack.com/authentication/verifying-requests-from-slack)。"""
    secret = signing_secret()
    if not secret:
        return False
    timestamp = headers.get("X-Slack-Request-Timestamp", "")
    signature = headers.get("X-Slack-Signature", "")
    if not timestamp or not signature:
        return False
    try:
        if abs(time.time() - int(timestamp)) > 60 * 5:
            return False  # リプレイ攻撃対策: 5分以上古いリクエストは拒否
    except ValueError:
        return False
    basestring = f"v0:{timestamp}:".encode() + raw_body
    computed = "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature)


def _react_async(channel, ts):
    """メンションに👀リアクションを別スレッドで付ける（応答をブロックしない）。"""
    def _run():
        try:
            import slack_client
            slack_client.react(channel, ts, "eyes")
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def handle_event(headers, raw_body, bot_user_id, handlers):
    """`/slack/events` へのPOSTを処理する。(status_code, response_body_bytes) を返す。

    handlers はコマンド名 → コールバックの辞書:
      order(parsed, channel, thread_ts, reply)        … buy/sell（一回きりの注文）
      clear_all(channel, thread_ts, reply)            … clear-all
      book(ticker, channel, thread_ts, reply)         … book（tickerは未指定ならNone）
      watch(parsed, channel, thread_ts, reply)        … watch
      watch_open(parsed, channel, thread_ts, reply)   … watch-open
      unwatch(ticker, channel, thread_ts, reply)      … unwatch
      unwatch_open(ticker, channel, thread_ts, reply) … unwatch-open
    reply(text) はそのスレッドに返信する関数。Slackの3秒タイムアウトに収まるよう、
    いずれも重い処理をせずキュー/設定ファイルの操作だけにすること。
    """
    if not verify_signature(headers, raw_body):
        return 401, b"invalid signature"
    try:
        payload = json.loads(raw_body)
    except ValueError:
        return 400, b"bad json"

    if payload.get("type") == "url_verification":
        body = json.dumps({"challenge": payload.get("challenge", "")}).encode()
        return 200, body

    if payload.get("type") != "event_callback":
        return 200, b"ok"
    if headers.get("X-Slack-Retry-Num"):
        return 200, b"ok (retry ignored)"  # 再送は無視（二重発注防止）

    event = payload.get("event", {})
    if event.get("type") != "app_mention" or event.get("bot_id"):
        return 200, b"ok"

    # 受信の合図として、権限・書式チェックの前にまずメンションへ👀を付ける。
    # Slackの3秒タイムアウト内に200を返せるよう、別スレッドで非同期に付ける。
    # リアクションは補助なので、失敗しても（reactions:write 未付与等）本処理は続ける。
    _react_async(event.get("channel"), event.get("ts"))

    user = event.get("user")
    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")
    mention_user = ENV.get("SLACK_MENTION_USER", "")

    def reply(text):
        import slack_client
        slack_client.post(channel, text, thread_ts=thread_ts)

    if user != mention_user:
        reply("権限がありません（登録済みユーザーのみ発注できます）。")
        return 200, b"ok"

    text = event.get("text", "")
    if is_clear_all(text, bot_user_id):
        handlers["clear_all"](channel, thread_ts, reply)
        return 200, b"ok"

    watch = parse_watch(text, bot_user_id)
    if watch is not None:
        handlers["watch"](watch, channel, thread_ts, reply)
        return 200, b"ok"

    watch_open = parse_watch_open(text, bot_user_id)
    if watch_open is not None:
        handlers["watch_open"](watch_open, channel, thread_ts, reply)
        return 200, b"ok"

    unwatch = parse_unwatch(text, bot_user_id)
    if unwatch is not None:
        handlers["unwatch"](unwatch, channel, thread_ts, reply)
        return 200, b"ok"

    unwatch_open = parse_unwatch_open(text, bot_user_id)
    if unwatch_open is not None:
        handlers["unwatch_open"](unwatch_open, channel, thread_ts, reply)
        return 200, b"ok"

    book = parse_book(text, bot_user_id)
    if book is not None:
        handlers["book"](book["ticker"], channel, thread_ts, reply)
        return 200, b"ok"

    parsed = parse_command(text, bot_user_id)
    if not parsed:
        reply(USAGE)
        return 200, b"ok"
    handlers["order"](parsed, channel, thread_ts, reply)
    return 200, b"ok"
