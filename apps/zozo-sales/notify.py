#!/usr/bin/env python3
"""売上集計を1時間ごとにチェックし、新規売上があれば Slack に通知する。

usage: ./notify.py [YYYY-MM-DD] [--force]
  日付省略時は今日 (JST)。--force で差分なしでも必ず通知。

state は config/notify_state.json に保存する。
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from fetch_sales import Portal, parse_summary, parse_detail, CONF_DIR

JST = timezone(timedelta(hours=9))
WEEKDAYS = "月火水木金土日"
STATE_PATH = os.path.join(CONF_DIR, "notify_state.json")
BOT_TOKEN_PATH = os.path.join(CONF_DIR, "slack_bot_token")
CHANNEL_PATH = os.path.join(CONF_DIR, "slack_channel")

USERNAME = "krähe 売上通知"
ICON_EMOJI = ":dress:"


def read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def post_slack(text):
    bot_token = os.environ.get("SLACK_BOT_TOKEN") or read_file(BOT_TOKEN_PATH)
    channel = os.environ.get("ZOZO_SALES_CHANNEL") or read_file(CHANNEL_PATH)
    if not bot_token or not channel:
        sys.exit("[zozo-sales notify] bot_token または channel が未設定です")
    payload = {
        "channel": channel,
        "text": text,
        "username": USERNAME,
        "icon_emoji": ICON_EMOJI,
    }
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {bot_token}",
                 "Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.loads(r.read())
    if not resp.get("ok"):
        sys.exit(f"[zozo-sales notify] chat.postMessage failed: {resp.get('error')}")
    print(f"sent to {channel}", file=sys.stderr)


def products_to_dict(products):
    d = {}
    for p in products:
        key = f"{p['name']}|{p['price_type']}"
        d[key] = {"qty": p["qty"], "amount": p["amount"],
                  "name": p["name"], "code": p.get("code", ""), "price_type": p["price_type"]}
    return d


def format_message(date, delta_qty, delta_amount, new_products, total_qty, total_amount):
    d = datetime.strptime(date, "%Y/%m/%d")
    wd = WEEKDAYS[d.weekday()]
    lines = [
        f"*krähe 売上通知です！* {date}({wd})",
        f"新着 *+{delta_qty:,}点 +¥{delta_amount:,}*（税抜）",
        f"（本日合計: {total_qty:,}点 ¥{total_amount:,}）",
        "",
    ]
    for p in sorted(new_products, key=lambda x: -x["amount"])[:15]:
        label = "🏷" if p["price_type"] == "セール" else ""
        lines.append(f"• {label}{p['name']} `{p.get('code', '')}` — ¥{p['amount']:,} ({p['qty']}点)")
    return "\n".join(lines)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    if args:
        date = datetime.strptime(args[0], "%Y-%m-%d").strftime("%Y/%m/%d")
    else:
        date = datetime.now(JST).strftime("%Y/%m/%d")

    portal = Portal()
    portal.login()
    summary_text = portal.search_summary(date)
    summary = parse_summary(summary_text, date)
    products = parse_detail(portal.get_detail(date))

    total_qty = summary["total_qty"]
    total_amount = summary["total_amount"]

    state = load_state()
    last_date = state.get("date")
    last_products = state.get("products", {}) if last_date == date else {}
    last_total_qty = state.get("total_qty", 0) if last_date == date else 0

    current_products = products_to_dict(products)

    # 差分計算
    delta_items = []
    delta_qty = 0
    delta_amount = 0
    for key, cur in current_products.items():
        prev_qty = last_products.get(key, {}).get("qty", 0)
        if cur["qty"] > prev_qty:
            added_qty = cur["qty"] - prev_qty
            # 追加分の金額 = 単価 × 追加点数
            unit_price = cur["amount"] // cur["qty"] if cur["qty"] else 0
            added_amount = unit_price * added_qty
            delta_items.append({**cur, "qty": added_qty, "amount": added_amount})
            delta_qty += added_qty
            delta_amount += added_amount

    if not force and delta_qty == 0:
        print(f"[zozo-sales notify] {date} 新規売上なし (合計 {total_qty}点)", file=sys.stderr)
        save_state({"date": date, "total_qty": total_qty, "total_amount": total_amount,
                    "products": current_products})
        return

    if force and delta_qty == 0:
        # --force 時は合計を表示
        d = datetime.strptime(date, "%Y/%m/%d")
        wd = WEEKDAYS[d.weekday()]
        msg = (f"*krähe 売上通知です！* {date}({wd})\n"
               f"*{total_qty:,}点 ¥{total_amount:,}*（税抜）\n")
        if products:
            msg += "\n"
            for p in sorted(products, key=lambda x: -x["amount"])[:15]:
                label = "🏷" if p["price_type"] == "セール" else ""
                msg += f"• {label}{p['name']} — ¥{p['amount']:,} ({p['qty']}点)\n"
    else:
        msg = format_message(date, delta_qty, delta_amount, delta_items, total_qty, total_amount)

    post_slack(msg)
    save_state({"date": date, "total_qty": total_qty, "total_amount": total_amount,
                "products": current_products})


if __name__ == "__main__":
    main()
