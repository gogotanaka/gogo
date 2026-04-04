---
name: slack-later
description: Fetch and display Slack "Save for later" (saved items) for the aisaac workspace. Use when the user asks about their Slack saved items, "later" list, bookmarks, or wants to see what they've saved in Slack.
---

# Slack Later

Fetch Slack "Save for later" items via CDP from the running Slack desktop app.

## Prerequisites

- Slack desktop app must be running with CDP enabled on port 9222
- `websocket-client` Python package: `pip install websocket-client`

## Run

```bash
python3 <skill_dir>/scripts/fetch_later_standalone.py [limit]
```

- `limit`: number of items to fetch (default: 50)
- Returns JSON array of in-progress saved items

## Output fields

| field | description |
|-------|-------------|
| `saved_date` | 保存日時 (YYYY-MM-DD HH:MM) |
| `channel` | チャンネル名 |
| `text` | メッセージ冒頭200文字 |
| `link` | Slack上のリンク |

## Display

取得したJSONを人間が読みやすい形に整形して表示する。
各アイテムは以下の形式で表示する：

```
📌 [saved_date] #channel
   text（省略あり）
   🔗 link
```

件数が多い場合はチャンネルごとにグループ化するか確認する。

## Error handling

- `connection refused on 9222`: Slackのデスクトップアプリが起動していないか、CDPが有効でない
- `websocket-client not found`: `pip install websocket-client` を実行
- API error: xoxc/xoxdトークンが期限切れの可能性。Slackを再起動して再試行
