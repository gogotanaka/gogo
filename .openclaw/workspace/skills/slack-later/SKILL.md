---
name: slack-later
description: Fetch and display Slack "Save for later" (saved items) for the aisaac workspace. Use when the user asks about their Slack saved items, "later" list, bookmarks, or wants to see what they've saved in Slack.
---

# Slack Later

Fetch Slack "Save for later" items via CDP from the running Slack desktop app.

## Prerequisites

- Slack desktop app must be running with CDP enabled on port 9222
- `websocket-client` Python package: `pip install websocket-client`

## Run (Web UI — デフォルト)

```bash
python3 <skill_dir>/scripts/later_web.py [limit]
```

- `limit`: 取得件数（default: 200）
- チャンネルプレフィックスで自動カテゴリ分けし、ダークテーマのWebページを `http://localhost:8377` で表示
- ブラウザが自動で開く
- カテゴリごとに「全部Slackで開く」ボタンあり
- サーバーはバックグラウンドで起動すること（`run_in_background: true`）

## Run (JSON出力)

```bash
python3 <skill_dir>/scripts/fetch_later_standalone.py [limit]
```

- `limit`: 取得件数（default: 50）
- JSON配列を標準出力に返す

## Output fields

| field | description |
|-------|-------------|
| `saved_date` | 保存日時 (YYYY-MM-DD HH:MM) |
| `channel` | チャンネル名 |
| `text` | メッセージ冒頭200文字 |
| `link` | Slack上のリンク |

## Display

デフォルトはWeb UIを使う。Web UIが使えない場合はJSON出力をフォールバックとして使い、以下の形式で表示する：

```
📌 [saved_date] #channel
   text（省略あり）
   🔗 link
```

## Auto-restart

接続エラー（`connection refused on 9222` や `Handshake status 403`）が発生した場合、ユーザーに確認せず自動でSlackを再起動する：

```bash
osascript -e 'quit app "Slack"' 2>/dev/null; sleep 2; open -a Slack --args --remote-debugging-port=9222 --remote-allow-origins="*"
```

再起動後、5秒待ってからスクリプトを再実行する。

## Error handling

- `connection refused on 9222`: Slackが起動していないかCDPが有効でない → 上記Auto-restartを実行
- `Handshake status 403`: `--remote-allow-origins="*"` が不足 → 上記Auto-restartを実行
- `websocket-client not found`: `pip install websocket-client` を実行
- API error: xoxc/xoxdトークンが期限切れの可能性。Slackを再起動して再試行
