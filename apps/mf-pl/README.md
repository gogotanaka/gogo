# mf-pl — 月次PL/BSをSlackに流す

Money Forward クラウド会計 API v3 の残高試算表から対象月のPL（損益計算書）とBS（貸借対照表）を
取得し、Slack に整形して投稿する。複数の会社（プロファイル）に対応。

## 構成

| ファイル | 役割 |
|---|---|
| `auth.py` | OAuth 2.0 認可コードフロー + トークン自動リフレッシュ（プロファイル別） |
| `fetch_pl.py` | `/api/v3/reports/trial_balance_{pl,bs}` から対象月のJSONを取得 |
| `format_pl.py` | JSON → Slack投稿用テキスト（PL: 主要損益項目+販管費TOP5 / BS: 部合計） |
| `send_slack.py` | Bot トークンで投稿（なければ CDP 経由の自分DMにフォールバック） |
| `run.sh` | 全プロファイルを回すエントリポイント |

## 使い方

```sh
./run.sh                       # 全社の先月売上高サマリーを1通で Slack へ（デフォルト）
./run.sh --full                # 会社ごとの PL+BS 詳細を投稿
./run.sh --month 2026-06       # 対象月指定
./run.sh --full --profile aisaac  # 1社だけ詳細
./run.sh --dry-run             # 送信せず表示のみ
```

売上サマリーは `sales.py` が生成（売上高の大きい順 + 合計行）。

## 設定ファイル

| パス | 内容 |
|---|---|
| `~/.config/moneyforward/oauth_client.json` | 共通の Client ID/Secret（`{"client_id","client_secret"}`） |
| `~/.config/moneyforward/oauth_client-<profile>.json` | 会社別クライアント（あれば共通より優先） |
| `~/.config/moneyforward/tokens.db` | トークン (SQLite)。`tokens` テーブルにプロファイル別で保存、auth.py が管理 |
| `~/.config/slack/mf_pl_bot_token` | Slack bot トークン（xoxb-…、awsm workspace） |
| `~/.config/slack/mf_pl_channel` | 投稿先チャンネルID（環境変数 MF_PL_CHANNEL が優先） |

## 会社を追加するには

前提: 自分のマネーフォワードIDがその会社（事業者）に閲覧権限付きで入っていること。

```sh
python3 auth.py <新プロファイル名>   # ブラウザの認可画面で対象の会社を選んで同意
python3 fetch_pl.py --profile <新プロファイル名> --check   # 会社名が出ればOK
```

以降 `./run.sh` が自動で全プロファイルを回す。会社名はAPIから取得するので設定不要。
別テナントで同じ連携用アプリが使えない場合は、その会社のアプリポータルでアプリを登録し、
`oauth_client-<profile>.json` に Client ID/Secret を置く。

## セットアップの経緯・APIメモ

- クラウド会計 API は **APIキー非対応（OAuth 2.0 のみ）**。
  APIキーのJWTに載る `conac` は連結会計のことで、会計には使えない
- 認可: `GET https://api.biz.moneyforward.com/authorize` → `POST /token`
  （scope: `mfc/accounting/report.read mfc/accounting/offices.read`、
  クライアント認証方式: CLIENT_SECRET_BASIC）
- アクセストークン1時間、リフレッシュトークン540日 → 初回認可後は無人運用可
- PL/BS: `GET https://api-accounting.moneyforward.com/api/v3/reports/trial_balance_{pl,bs}?start_date=…&end_date=…`
- レスポンスは階層 rows。values は `[前期残高, 借方, 貸方, 期末残高, 構成比]` 固定。
  単月指定なら PL は期末残高=当月発生額、BS は期末残高=月末時点残高
- 金額0の科目と一部の決算書項目は返却されない。常に全部門合計
- Slack bot（awsm workspace, scope: chat:write のみ）は招待済みチャンネルにしか投稿できない。
  CDP フォールバックは Slack を勝手に再起動しない（過去の launchd 問題のため）

## 定期実行したい場合

bot トークン経由なら Slack アプリの起動状態に依存しないので、launchd で毎月回せる。
例: 毎月1日 10:00 に先月分を投稿（未設定。必要になったら plist を作る）。
