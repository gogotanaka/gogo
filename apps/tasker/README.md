# tasker — 秘書 + todo カテゴリ表示

Notion MAIN と Slack Later を覗いて「まだこれやる？」を自分にDMする秘書と、
Notionのtodoをカテゴリ分けしてブラウザで眺めるweb UI。

## 構成

| ファイル | 役割 |
|---|---|
| `fetch_notion.py` | Notion MAIN ページ（`227efe…cf21`）のblocksをmarkdown風テキストで出力 |
| `fetch_later.py` | Slack desktopのCDP経由でLater items取得（CDP未起動ならSlackを自動再起動） |
| `send_dm.py` | Slack desktop のxoxc/xoxd を使ってチャンネル `C02ETSXK33J` に送信（`TASKER_CHANNEL` env で上書き可） |
| `secretary.md` | 秘書のペルソナ指示 |
| `run.sh` | 全部合体させて `claude -p` で生成、`send_dm.py` で送信 |
| `tasker_web.py` | Notion MAIN のtodoを見出し単位でカテゴリ分けして `http://localhost:8378` に表示 |

## 前提

- `~/.config/notion/api_key` に Notion internal integration の key が入っていて、MAIN ページに integration が接続されていること
- Slack デスクトップが起動していて CDP (9222) が有効になっていること（`fetch_later.py` が必要なら自動再起動する）
- Python の `websocket-client` が入っていること: `pip3 install websocket-client`
- `claude` CLI にパスが通っていること

## 使い方

### 秘書DM

```bash
# メッセージだけ生成して確認（デフォルト5件）
./run.sh --dry-run

# 件数指定（例: 3件）
./run.sh 3 --dry-run

# 本番送信
./run.sh           # 5件
./run.sh 7         # 7件
```

似たテーマ（同じプロジェクト／同じ相手など）のリマインドは自動でまとめる。

### Web UI（todoカテゴリ表示）

```bash
python3 tasker_web.py
```

- `http://localhost:8378` が自動で開く
- カテゴリ分けは Notion の見出し（`#` / `##` / `###`）単位
- デフォルトは未完了todoのみ表示、右上の「完了も表示」で切り替え
- 「再取得」でNotionから取り直し

## 次にやる（予定）

- [ ] `launchd` or `crontab` で 朝9時 / 夕18時(JST) に自動実行
- [ ] 過去に秘書が何を指摘したかのログを残して、同じことを繰り返さないようにする
