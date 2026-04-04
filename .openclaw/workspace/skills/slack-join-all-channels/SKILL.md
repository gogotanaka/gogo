---
name: slack-join-all-channels
description: Join all public Slack channels in the aisaac workspace. Use when the user asks to join all public channels, bulk-join Slack channels, or subscribe to every channel in the workspace.
---

# Slack Join All Channels

全public channelに一括joinする。CDPでSlackデスクトップアプリからトークンを取得して実行。

## Prerequisites

- Slack desktop app が起動中でCDPが有効 (port 9222)
- `websocket-client` Python package: `pip install websocket-client`

## Run

```bash
# 実際にjoin
python3 <skill_dir>/scripts/join_all_channels.py

# dry run（joinしないで一覧だけ表示）
python3 <skill_dir>/scripts/join_all_channels.py --dry-run
```

`<skill_dir>` = `~/work/gogo/.openclaw/workspace/skills/slack-join-all-channels`

## Output

- すでにメンバーのチャンネル数
- joinが必要なチャンネル数
- join成功/失敗の結果サマリー

## Rate limiting

Slack Tier 3 APIに準じて1秒/リクエストのスロットリングを入れている。チャンネル数が多い場合は時間がかかる。

## Error handling

- `connection refused on 9222`: Slackデスクトップアプリが起動していないか、CDPが無効
- `websocket-client not found`: `pip install websocket-client`
- `token_expired` / `not_authed`: Slackを再起動してトークンを再取得
- `already_in_channel`: スクリプトが自動スキップする（is_member チェック済み）
