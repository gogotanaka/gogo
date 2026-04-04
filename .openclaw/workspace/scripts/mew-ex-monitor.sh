#!/bin/bash
# ミュウex PSA10 最安値モニタリング
# スニーカーダンク API から取得して Slack に投稿

CHANNEL="C0AKK0V9JUR"
APPAREL_ID=98531
PSA10_CONDITION_ID=22

# API から最安値を取得
RESPONSE=$(curl -s "https://snkrdunk.com/v1/apparels/${APPAREL_ID}/used?perPage=5&page=1&order=price&isSaleOnly=true&conditionIds=${PSA10_CONDITION_ID}" \
  -H "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")

# 最安値と商品IDを取得
MIN_PRICE=$(echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
items = data.get('apparelUsedItems', [])
if items:
    print(items[0].get('price', 0))
else:
    print(0)
")

ITEM_ID=$(echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
items = data.get('apparelUsedItems', [])
if items:
    print(items[0].get('id', ''))
else:
    print('')
")

ITEM_COUNT=$(echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
items = data.get('apparelUsedItems', [])
# 同じ最安値の件数
if items:
    min_price = items[0].get('price', 0)
    count = sum(1 for i in items if i.get('price') == min_price)
    print(count)
else:
    print(0)
")

if [ -z "$MIN_PRICE" ] || [ "$MIN_PRICE" = "0" ]; then
  echo "Failed to fetch price"
  exit 1
fi

# 価格フォーマット (カンマ区切り)
FORMATTED_PRICE=$(python3 -c "print(f'{${MIN_PRICE}:,}')" 2>/dev/null || echo "$MIN_PRICE")

URL="https://snkrdunk.com/apparels/${APPAREL_ID}/used/${ITEM_ID}?slide=right"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M')

# Slack に投稿
openclaw message send \
  --channel "$CHANNEL" \
  --message "📊 *ミュウex PSA10 最安値* (${TIMESTAMP})
現在: *¥${MIN_PRICE}* (${ITEM_COUNT}点)
${URL}"

echo "Posted: ¥${MIN_PRICE} at ${TIMESTAMP}"
