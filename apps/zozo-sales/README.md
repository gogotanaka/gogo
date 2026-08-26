# zozo-sales

ZOZOTOWN 店舗管理ポータル (to.zozo.jp) から日次の売上サマリを取得し、
毎朝 9:00 JST に前日分を Slack チャンネルへ投稿する。

ZOZOTOWN の受注 API はパートナー (OMS 事業者) 限定のため、
ポータルをスクレイピングする方式。

## セットアップ

`config/` (gitignore 済み) に以下を置く:

| ファイル | 内容 |
|---|---|
| `config/basic_auth` | to.zozo.jp の Basic 認証 `user:pass` (1行) |
| `config/portal_login` | ポータルのログイン ID/パスワード `user:pass` (1行) |
| `config/slack_bot_token` | Slack bot トークン (xoxb-…、chat:write) |
| `config/slack_channel` | 投稿先チャンネル ID |

## ファイル

- `probe.py` — ポータルのページ構造を調査する開発用スクリプト
- (これから実装) `fetch_sales.py` — 前日の売上明細を取得
- (これから実装) `send_slack.py` — サマリを Slack へ投稿
- (これから実装) `run.sh` — launchd から呼ばれるエントリポイント

## 実行

```sh
./probe.py            # トップ/ログインページの構造を確認
./probe.py /some/path # 任意パスを確認
```
