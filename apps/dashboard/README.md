# dashboard

各 Slack workspace の **Later / Activity / Drafts** の件数を1画面で表示する個人用 web ダッシュボード。

- URL: <http://localhost:8380>
- 対象: Slack desktop に signed-in 中の全 workspace（`localConfig_v2.teams` を列挙）
- 取得方法: tasker / people-reminders と同じく CDP port 9222 → `xoxc` + `xoxd`

## 起動

```bash
python3 apps/dashboard/web.py
```

ブラウザは自動で開く。リロードで再 fetch。

## ファイル

- `fetch_counts.py` — 各 workspace の3つの count を並列取得、JSON 出力もできる (`python3 fetch_counts.py`)
- `web.py` — port 8380 の HTTP サーバー

## 内部 API メモ

| 用途 | 採用 | フォールバック候補 |
|---|---|---|
| Later | `saved.list` (state=in_progress を数える) | — |
| Activity | `activity.list` の `total` | `users.activity.list`, `client.counts` |
| Drafts | `drafts.list` の `total` | `client.drafts.list`, `drafts.list` の `drafts[]` 長さ |

Activity / Drafts は内部 API 名が不確実なので候補を順番に試し、最初に `ok:true` を返したものを採用する。UI には `via xxx.xxx` で実際に使われた method 名を表示する（`saved.list` は確定なので表示しない）。

新しい method が判明したら `count_activity` / `count_drafts` の candidates 先頭に足す。
