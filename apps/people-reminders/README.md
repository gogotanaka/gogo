# people-reminders

Slack の List 「people-reminders」を読み込み、各行にひもづく Slack スレッドも見て **「終わってそうかどうか」** を Claude に判定させて Web で表示する個人用アプリ。

`apps/tasker/` 方式（CDP port 9222 経由で xoxc/xoxd 抽出）を踏襲。**aisaac workspace 専用**。

## 構成

- `fetch_list.py` — `files.list?types=lists` で list を `title` マッチで探し、`lists.records.list?list_id=...` で全行取得。各 record の `Col*` を `files.info` の schema で human-readable な column 名に変換。`Message` 列の Slack URL から channel/ts をパースして `conversations.replies` でスレッドを合体。`users.info` で user_id を表示名に解決、`<@U...>` mention と `<URL|label>` 記法も全部展開
- `judge_done.py` — `fetch_list.py` の JSON を stdin で受けて `claude -p` で各行を `done` / `unclear` / `open` 分類。**done の場合は根拠となるスレッドメッセージの ts を `evidence_ts` 配列で返す**
- `web.py` — 上 2 つを使って `http://localhost:8379` で表示。
    - Done card には判定理由 + **「★ user 日時」リンク**（スレッド内の根拠メッセージへの deep link）を表示
    - スレッド内の根拠メッセージは緑ハイライト
    - URL は全部 `<a>` タグ化（Slack URL は「Slackメッセージ↗」と短縮表示）
    - **「本当に終わってる ✓」ボタン**で `lists.records.delete` を叩いて Slack list から削除

## 使い方

```bash
cd apps/people-reminders
python3 web.py
```

ブラウザが自動で開く。クエリパラメータ:

- `?show=open` / `?show=done` / `?show=unclear` / `?show=all`（デフォルト: open + unclear）
- `?refresh=1` — キャッシュ無視で再取得
- `?nojudge=1` — Claude judge をスキップ（速い、全部 unclear 扱い）

env vars:
- `PEOPLE_REMINDERS_LIST` — list 名差し替え（デフォルト `people-reminders`）
- `PEOPLE_REMINDERS_LIST_ID` — `F0XXXX` を直接指定して discovery をスキップ
- `PEOPLE_REMINDERS_DEBUG=1` — 全 API レスポンスを stderr に dump

## 発見した Slack 内部 API

| 目的 | method | params | 備考 |
|------|--------|--------|------|
| list を探す | `files.list` | `types=lists` | name は常に `"list"`、表示名は `title` 側 |
| schema 取得 | `files.info` | `file=F...` | `file.list_metadata.schema` に column 定義 |
| 行取得 | `lists.records.list` | `list_id=F...` | `records[]`、各要素に `fields[]` |
| 行削除 | `lists.records.delete` | `list_id=F..., id=Rec...` | **`record_id` ではなく `id`** |

存在しない: `slackLists.*`、`lists.list`、`lists.info` (全部 `unknown_method`)

## 注意

- Slack desktop が CDP port 9222 で立ち上がってないと `fetch_list.py` が自動で再起動を試みる
- Slack Lists API はパブリック未公開なので変更で壊れる可能性。失敗時は `PEOPLE_REMINDERS_DEBUG=1` で stderr 確認
- `claude` CLI 不在時は judge スキップ、全件 `unclear`
- 削除は確認ダイアログ後にサーバ POST 経由で実行
- `apps/tasker/` とコード共有しない（1アプリ=1ディレクトリ完結）
