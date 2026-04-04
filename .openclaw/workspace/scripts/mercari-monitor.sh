#!/bin/bash
# メルカリ ミュウ 25th PSA10 最安値監視スクリプト
# ミュウ 25th psa10 / 販売中のみ / ¥35,000以上 / 安い順
# 30分ごとに #pc-ミュウex-監視 へ投稿

SLACK_CHANNEL="C0AKK0V9JUR"  # #pc-ミュウex-監視
STATE_FILE="$HOME/work/gogo/.openclaw/workspace/data/mercari-psa10-price.txt"
EXCLUDE_FILE="$HOME/work/gogo/.openclaw/workspace/data/mercari-psa10-exclude.txt"
LOG_FILE="$HOME/work/gogo/.openclaw/workspace/logs/mercari-monitor.log"
OPENCLAW="$HOME/.local/share/mise/installs/node/25.6.1/bin/openclaw"
SEARCH_URL="https://jp.mercari.com/search?keyword=%E3%83%9F%E3%83%A5%E3%82%A6%2025th%20psa10&order=asc&price_min=35000&sort=price&status=on_sale"
RESPONSE_TMP="/tmp/mercari-monitor-response.json"

mkdir -p "$(dirname "$STATE_FILE")"
mkdir -p "$(dirname "$LOG_FILE")"

# openclaw browser responsebody + navigate で取得
# 1) responsebody をバックグラウンドで待機
"$OPENCLAW" browser responsebody --browser-profile openclaw "**/entities:search" --timeout-ms 20000 > "$RESPONSE_TMP" 2>/dev/null &
BGPID=$!

# 2) 少し待ってからMercari検索ページへナビゲート
sleep 2
"$OPENCLAW" browser navigate --browser-profile openclaw "$SEARCH_URL" >> "$LOG_FILE" 2>&1

# 3) responsebody の結果を待つ
wait $BGPID

# 4) レスポンスをパース（除外リストを考慮）
EXCLUDE_IDS=""
if [ -f "$EXCLUDE_FILE" ]; then
    EXCLUDE_IDS=$(cat "$EXCLUDE_FILE" | tr '\n' ',' | sed 's/,$//')
fi

PARSED=$(python3 -c "
import sys, json
try:
    with open('$RESPONSE_TMP') as f:
        d = json.load(f)
    items = d.get('items', [])
    if not items:
        print('ERROR: no items')
        sys.exit(1)
    exclude_ids = set(x.strip() for x in '$EXCLUDE_IDS'.split(',') if x.strip())
    for item in items:
        iid = item.get('id', '')
        if iid in exclude_ids:
            continue
        price = item.get('price', 0)
        name = str(item.get('name', ''))[:40]
        print(f'{price}|{iid}|{name}')
        sys.exit(0)
    print('ERROR: all items excluded')
    sys.exit(1)
except Exception as e:
    print(f'ERROR: {e}')
    sys.exit(1)
" 2>&1)

if echo "$PARSED" | grep -q "^ERROR"; then
    echo "$(date): $PARSED" >> "$LOG_FILE"
    exit 1
fi

CURRENT_PRICE=$(echo "$PARSED" | cut -d'|' -f1)
ITEM_ID=$(echo "$PARSED" | cut -d'|' -f2)
ITEM_NAME=$(echo "$PARSED" | cut -d'|' -f3)

if [ -z "$CURRENT_PRICE" ] || [ "$CURRENT_PRICE" = "0" ]; then
    echo "$(date): Could not get price" >> "$LOG_FILE"
    exit 1
fi

ITEM_URL="https://jp.mercari.com/item/$ITEM_ID"

# 前回価格を読み込む
if [ -f "$STATE_FILE" ]; then
    PREV_PRICE=$(cat "$STATE_FILE")
else
    PREV_PRICE=""
fi

echo "$(date): Current lowest PSA10 (Mercari) = ¥$CURRENT_PRICE (prev: ${PREV_PRICE:-none})" >> "$LOG_FILE"

# 現在価格を保存
echo "$CURRENT_PRICE" > "$STATE_FILE"

# 数字フォーマット
fmt() { python3 -c "print(f'{$1:,}')"; }

if [ -z "$PREV_PRICE" ]; then
    TREND="📊"
    PRICE_TEXT="*¥$(fmt $CURRENT_PRICE)*"
elif [ "$CURRENT_PRICE" -lt "$PREV_PRICE" ]; then
    DIFF=$((PREV_PRICE - CURRENT_PRICE))
    TREND="🔻"
    PRICE_TEXT="*¥$(fmt $CURRENT_PRICE)* (▼¥$(fmt $DIFF) from ¥$(fmt $PREV_PRICE))"
elif [ "$CURRENT_PRICE" -gt "$PREV_PRICE" ]; then
    DIFF=$((CURRENT_PRICE - PREV_PRICE))
    TREND="🔺"
    PRICE_TEXT="¥$(fmt $CURRENT_PRICE) (▲¥$(fmt $DIFF) from ¥$(fmt $PREV_PRICE))"
else
    TREND="📊"
    PRICE_TEXT="¥$(fmt $CURRENT_PRICE) (変化なし)"
fi

MSG="${TREND} *ミュウex PSA10 最安値（メルカリ）*
現在: ${PRICE_TEXT}
${ITEM_NAME}
${ITEM_URL}"

# openclaw で Slack に通知
"$OPENCLAW" message send --channel slack --target "$SLACK_CHANNEL" -m "$MSG" >> "$LOG_FILE" 2>&1
echo "$(date): Posted to Slack: ¥$CURRENT_PRICE" >> "$LOG_FILE"
