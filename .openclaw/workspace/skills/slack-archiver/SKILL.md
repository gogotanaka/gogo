---
name: slack-archiver
description: "Slackの全チャンネルメッセージをSQLite3に保存するアーカイバー。進捗をDBに保存するため中断・再開が可能。Use when: user wants to archive or search Slack messages locally."
---

# Slack Archiver Skill

Slackに参加している全チャンネルのメッセージを SQLite3 (`messages.db`) に保存するスクリプト。

## セットアップ

```bash
cd skills/slack-archiver
npm install
cp .env.example .env
# .env を編集して SLACK_TOKEN を設定
```

## Slack Token の取得

1. https://api.slack.com/apps でアプリを作成
2. 「OAuth & Permissions」で以下のスコープを追加:
   - `channels:history` - パブリックチャンネルの履歴
   - `groups:history` - プライベートチャンネルの履歴
   - `im:history` - DMの履歴
   - `mpim:history` - グループDMの履歴
   - `channels:read` / `groups:read` / `im:read` / `mpim:read` - チャンネル一覧
3. ワークスペースにインストールして `xoxp-...` トークンを取得

## 使い方

```bash
# 全チャンネルを同期
node sync.js

# 特定チャンネルだけ
node sync.js --channel general

# 先頭10チャンネルだけ（テスト用）
node sync.js --limit 10
```

## DB構造

| テーブル | 用途 |
|---------|------|
| `channels` | チャンネル一覧と同期状態 |
| `messages` | メッセージ本体 |
| `sync_log` | 同期ログ |

## 再開について

`channels.synced_until_ts` に最後に取得したメッセージの ts を保存。
再実行時はその ts より新しいメッセージだけ取得する（差分更新）。

## DB確認クエリ例

```sql
-- 総メッセージ数
SELECT COUNT(*) FROM messages;

-- チャンネル別メッセージ数
SELECT c.name, COUNT(m.ts) as cnt
FROM channels c
LEFT JOIN messages m ON c.id = m.channel_id
GROUP BY c.id ORDER BY cnt DESC;

-- 特定チャンネルのメッセージ検索
SELECT text, ts FROM messages
WHERE channel_id = 'C...' AND text LIKE '%検索ワード%'
ORDER BY ts DESC LIMIT 20;
```
