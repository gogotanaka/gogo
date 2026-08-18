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

## 設定ファイル（`config/` 配下、.gitignore 済み）

| パス | 内容 |
|---|---|
| `config/oauth_client.json` | 共通の Client ID/Secret（`{"client_id","client_secret"}`） |
| `config/oauth_client-<profile>.json` | 会社別クライアント（あれば共通より優先） |
| `config/tokens.db` | トークン (SQLite)。`tokens` テーブルにプロファイル別で保存、auth.py が管理 |
| `config/slack_bot_token` | Slack bot トークン（xoxb-…、awsm workspace、scope: chat:write + files:write） |
| `config/slack_channel` | 投稿先チャンネルID（環境変数 MF_PL_CHANNEL が優先） |
| `config/api_key` | MF APIキー（会計APIには使えないため未使用。連結会計用） |

`config/` は認証情報のみのディレクトリで、リポジトリにはコミットしない。
バックアップ・別マシン移行時はこのディレクトリごとコピーすればよい。

## 会社を追加するには

前提: 自分のマネーフォワードIDがその会社（事業者）に閲覧権限付きで入っていること。

```sh
python3 auth.py <新プロファイル名>   # ブラウザの認可画面で対象の会社を選んで同意
python3 fetch_pl.py --profile <新プロファイル名> --check   # 会社名が出ればOK
```

以降 `./run.sh` が自動で全プロファイルを回す。会社名はAPIから取得するので設定不要。

**`--check` は必ず実行すること。** 認可画面は事業者を選ぶUIなので、選び間違えると
別の会社のトークンがそのプロファイル名で保存され、集計時に同じ会社が二重計上される
（実際に一度起きた）。違っていたら `sqlite3 config/tokens.db "DELETE FROM tokens WHERE profile='<名前>'"`
で消してから認可し直す。

### リダイレクトURIの制約（重要）

**MF はリダイレクトURIに `http://` を受け付けない**（`https` 必須）。`http://localhost:8384/callback`
はアプリポータルで保存自体はできるが、`/authorize` が HTTP 400 を返す（`localhost` /
`127.0.0.1` / 末尾スラッシュ有無すべて不可）。そのためローカルで認可を通すにも
**https のトンネルが要る**：

```sh
cloudflared tunnel --url http://localhost:8384          # 出力の https://xxx.trycloudflare.com を控える
# → アプリポータルでそのURL + /callback をリダイレクトURIに登録（「追加」で複数登録可）
# → oauth_client*.json の redirect_uri に同じ値を書き、local_port: 8384 を添える
python3 auth.py <profile>
```

trycloudflare の quick tunnel はホスト名が毎回変わるので、**認可のたびにポータルへの
登録が必要**。認可さえ通ればリフレッシュトークン（540日）で回るのでトンネルは不要になる。
頻繁に会社を足すなら名前付きトンネル＋独自ドメインで固定URLにするとこの手間が消える。

### 別アプリが必要になる場合

会社によっては共通の連携用アプリを使えず（アプリポータルはMFID/事業者ごと）、その会社の
アプリポータルで新規にアプリを登録することになる。その場合は Client ID/Secret を
`config/oauth_client-<profile>.json` に置けば共通設定より優先される。1つのアプリで複数の
会社を認可できることもあるので（認可画面の事業者リストに出れば可）、まず既存アプリで
試すとよい。

アプリポータル: https://app-portal.moneyforward.com/apps/

## セットアップの経緯・APIメモ

- クラウド会計 API は **APIキー非対応（OAuth 2.0 のみ）**。
  APIキーのJWTに載る `conac` は連結会計のことで、会計には使えない
- 認可: `GET https://api.biz.moneyforward.com/authorize` → `POST /token`
  （scope: `mfc/accounting/report.read mfc/accounting/offices.read`、
  クライアント認証方式: CLIENT_SECRET_BASIC）
- アクセストークン1時間、リフレッシュトークン540日 → 初回認可後は無人運用可
- PL/BS: `GET https://api-accounting.moneyforward.com/api/v3/reports/trial_balance_{pl,bs}?start_date=…&end_date=…`
- レスポンスは階層 rows。values は `[前期残高, 借方, 貸方, 期末残高, 構成比]` 固定。
  **PL の期末残高は単月指定でも期首からの累計**なので、当月発生額 = 期末残高 − 前期残高
  （`format_pl.amount()`。月次推移表 transition_pl の当月列と一致を確認済み）。
  BS は期末残高がそのまま月末時点残高。構成比も PL は累計ベースなので当月売上高で再計算する
- **締めていない月は数値が壊れて見える**（売上高や負債がマイナスになる等）。
  当月・進行中の月ではなく、締んだ月を指定すること
- 金額0の科目と一部の決算書項目は返却されない。常に全部門合計
- Slack bot（awsm workspace, scope: chat:write のみ）は招待済みチャンネルにしか投稿できない。
  CDP フォールバックは Slack を勝手に再起動しない（過去の launchd 問題のため）

## 定期実行したい場合

bot トークン経由なら Slack アプリの起動状態に依存しないので、launchd で毎月回せる。
例: 毎月1日 10:00 に先月分を投稿（未設定。必要になったら plist を作る）。
