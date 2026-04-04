#!/bin/bash
# SNKRDUNK PSA10 最安値監視スクリプト
# ミュウex: プロモ[S8a-P 014/025](プロモカードパック 25th ANNIVERSARY edition)
# PSA10 / 販売中のみ / 安い順
# 30分ごとに #pc-ミュウex-監視 へ投稿

APPAREL_ID=98531
CONDITION_ID=22  # PSA10
STATE_FILE="$HOME/work/gogo/.openclaw/workspace/data/snkrdunk-psa10-price.txt"
SLACK_CHANNEL="C0AKK0V9JUR"  # #pc-ミュウex-監視
OPENCLAW="$HOME/.local/share/mise/installs/node/25.6.1/bin/openclaw"

mkdir -p "$(dirname "$STATE_FILE")"
mkdir -p "$HOME/work/gogo/.openclaw/workspace/logs"

# API から最安値を取得
RESPONSE=$(curl -s "https://snkrdunk.com/v1/apparels/$APPAREL_ID/used?perPage=5&page=1&order=price&withAllColors=false&isSaleOnly=true&conditionIds=$CONDITION_ID" -H "Accept: application/json")

CURRENT_PRICE=$(echo "$RESPONSE" | python3 -c "
import sys, json
d = json.load(sys.stdin)
items = d.get('apparelUsedItems', [])
if items:
    print(items[0].get('price', ''))
")

ITEM_ID=$(echo "$RESPONSE" | python3 -c "
import sys, json
d = json.load(sys.stdin)
items = d.get('apparelUsedItems', [])
if items:
    print(items[0].get('id', ''))
")

if [ -z "$CURRENT_PRICE" ]; then
    echo "$(date): API error or no items available"
    exit 1
fi

ITEM_URL="https://snkrdunk.com/apparels/$APPAREL_ID/used/$ITEM_ID?slide=right"

# 前回の価格を読み込む
if [ -f "$STATE_FILE" ]; then
    PREV_PRICE=$(cat "$STATE_FILE")
else
    PREV_PRICE=""
fi

echo "$(date): Current lowest PSA10 = ¥$CURRENT_PRICE (prev: ${PREV_PRICE:-none})"

# 現在価格を保存
echo "$CURRENT_PRICE" > "$STATE_FILE"

# メッセージ組み立て (数字フォーマット)
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

MSG="${TREND} *ミュウex PSA10 最安値*
現在: ${PRICE_TEXT}
${ITEM_URL}"

# openclaw で Slack に通知
"$OPENCLAW" message send --channel slack --target "$SLACK_CHANNEL" -m "$MSG"
echo "$(date): Posted to Slack: ¥$CURRENT_PRICE"
