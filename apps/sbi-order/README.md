# sbi-order — SBI証券 指値注文の自動発注・約定通知

## できること

- 指定した銘柄・株数・価格で現物株を指値発注し、約定したらSlack/macOS通知
- 板情報（気配値・本日出来高）を、相場が開く日の8:00から10分毎（時計整列）に
  Slackへメンション付きで投稿。`@bot book` でその場取得も可能
- `@bot buy/sell 銘柄 株数 価格` でメンション発注、`@bot clear all` で全取消
- ログインは初回のみ人間がパスキーで行う。以降のログアウトはID/PWでの自動再ログイン
  （`docs/adr/0012`）で復旧し、メールOTPが必要な場合だけSlackで人間に依頼する
- リビッドセッション: `@bot start 3930 1000 760`（銘柄・合計株数・上限価格）で開始。
  start は**営業日の8:59〜9:05（寄り付き前後）だけ受け付ける**。開始後は即座に初回
  ティック、翌営業日以降は8:59に初回、以降場中20〜30分おき（ランダム）に
  「未約定注文を全取消 → 最良買気配と同値で残量（目標−買付済み）の買い指値」を
  繰り返し、毎回Slackでメンション報告する。最良買気配が上限価格を超えている間は
  既存注文に触らず見送る（通知は超えた最初の回だけ）。部分約定も注文照会の
  「注文株数（未約定）」から数え、目標到達で自動終了。`@bot stop` で停止。
  セッションは `config/rebid_session.json` に永続化され、再起動・日またぎでも続く
- **売買の方針判断は人間が行う**。リビッドセッションも「上限価格以下で最良買気配に
  指値を置き続ける」という人間が決めた機械的ルールの実行のみで、相場判断はしない

ブラウザは Playwright が新規に立ち上げるのではなく、**普段使っているブラウザに
CDP (Chrome DevTools Protocol) 経由で接続する**方式（SBIのパスキーがブラウザ
プロファイルに紐づくため。`apps/mf-pl/send_slack.py` のSlack向けCDPフォールバック
と同じ発想）。設計の背景・調査した規制まわりの話・トラブルシューティングの経緯は
[docs/adr/](docs/adr/) 参照（特に [0001](docs/adr/0001-sbi-order-automation.md)・
[0003](docs/adr/0003-sbi-order-cdp-attach.md)）。

## 前提として承知しておくこと

- SBI証券は個人向けの現物株API を提供していない。このツールはブラウザ自動化
  （Playwright、CDP経由で普段使っているブラウザに接続）で SBI の Web サイトを
  人間の代わりに操作する
- SBI証券の約款第17条（非承認ツール・過大アクセスの禁止）に抵触するリスクがあり、
  最悪の場合アカウントが強制解約されうる。刑事罰の話ではなく契約上の措置
- 認証情報（`config/.env`, `config/slack_bot_token`）は平文で端末に置かれる。
  端末が侵害された場合の漏洩リスクを承知しておくこと
- 普段使っているブラウザをリモートデバッグ有効で起動する必要がある。その間、
  同じ端末上の別プロセスからもそのブラウザを操作できてしまう（他のタブ・セッションも
  含めて）ので、共有端末・信頼できないプロセスが動いている環境では避けること
- パスキーは自動化していない・できない。最初のログインは常に人間が普段のブラウザで行う。
  ログアウト検知後の再ログインは、ID/PWでの自動ログインを試みる（`config/.env` に
  `SBI_USER_ID`/`SBI_LOGIN_PASSWORD` が必要）。ID/PWログインの認証情報も、パスキー同様
  平文で端末に保存する点は変わらない
- 想定外の画面（デバイス認証・エラー等）が出たら自動突破せず処理を止める。
  人間がその場でブラウザを直接操作して解決する

## セットアップ

```sh
cd apps/sbi-order
pip3 install -r requirements.txt
playwright install chromium
mkdir -p config && chmod 700 config
```

普段使っているChromeを完全終了してから、リモートデバッグを有効にして起動し直す
（既存プロセスには後から付けられない）:

```sh
open -a "Google Chrome" --args --remote-debugging-port=9223 --remote-allow-origins=*
```

そのChromeでSBI証券にパスキーでログインしておく。

`config/.env` を作成する（`config/` は `.gitignore` 済みなのでコミットされない）:

```sh
SBI_CDP_URL=http://localhost:9223   # 普段使っているブラウザのCDPエンドポイント
SBI_USER_ID=ログインID              # 自動再ログイン用。無いとログアウト時に自動復旧せず
                                     # 常にSlackで人間に依頼する（従来通りの挙動）
SBI_LOGIN_PASSWORD=ログインパスワード  # 同上（パスキーの代わりに使うID/PWログイン用。
                                     # 取引パスワードとは別物）
SBI_TRADE_PASSWORD=取引パスワード   # 発注に必須。無いと place_order がエラーになる

# 株価監視 → Slack通知
SBI_WATCH_TICKERS=3930              # カンマ区切りで複数銘柄コード指定可（例: 3930,7203）
SBI_PRICE_POST_INTERVAL_SEC=600     # 板情報投稿の間隔（秒）。時計に揃える（既定600 = 8:00, 8:10, ...）
SLACK_CHANNEL=C0BQEBW40V9           # 板情報・約定・ログイン依頼を投稿するチャンネルID
SLACK_MENTION_USER=U09GPTXH00H      # メンション先（板情報・リビッド報告・ログイン依頼）／
                                     # メンション発注を許可するユーザーID
SBI_MAX_ORDER_VALUE_YEN=500000      # メンション発注・リビッドの上限見積金額（円）。超えたら拒否する

# リビッドセッション（`start` メンションで開始するので、envは間隔等の調整のみ）
SBI_REBID_INTERVAL_MIN_SEC=1200     # 実行間隔の下限（秒）。既定20分
SBI_REBID_INTERVAL_MAX_SEC=1800     # 実行間隔の上限（秒）。既定30分。毎回この範囲でランダム
SBI_REBID_LOT_SIZE=100              # 売買単位（株）。残量の切り捨てに使う。既定100
```

Slack bot トークンも別ファイルで置く（上記チャンネルに招待済みであること）:

```sh
echo "xoxb-..." > config/slack_bot_token
```

`SLACK_CHANNEL` / `SLACK_MENTION_USER` を空にしておけば、その機能（板情報投稿・ログイン依頼の
メンション）は無効化され、macOS通知のみになる。

### メンションでの発注（Events API, HTTP）

`@bot buy 3930 200 742`（buy/sell 銘柄コード 株数 価格。買い/売りでも可）のようにこのアプリのSlack botへ
メンションすると発注できる。メンションを受信したら、まず👀リアクションを付けて
「見た」ことを合図する（権限・書式チェックより前。bot に `reactions:write` スコープが必要）。
安全のため: 発言者が `SLACK_MENTION_USER` と一致しない場合は
無視、書式が厳密に一致しない場合も無視（どちらも理由をスレッドに返信するだけで発注はしない）、
見積金額が `SBI_MAX_ORDER_VALUE_YEN`（既定50万円）を超える場合も拒否する。

`@bot clear all` で、現在未約定の注文を全て取消する（1件ずつ取消し、結果をスレッドに
まとめて返信する）。銘柄・数量の指定はできず、無条件に全部取消す点に注意。

`@bot start 3930 1000 760`（銘柄 合計株数 上限価格）でリビッドセッションを開始、
`@bot stop` で停止する（詳細は冒頭の「できること」参照）。start は既存セッションを
置き換える。stop しても未約定の注文はそのまま残る（消すには `clear all`）。
リビッドの取消は「注文中」の注文全部が対象なので、セッション中に `buy` で出した
手動注文も次のティックで取り消される点に注意。

`@bot book` で、板情報（気配値）をその場で取得してスレッドに投稿する（定期投稿を
待たずに済む）。対象は `SBI_WATCH_TICKERS` の全銘柄。`@bot book 3930` のように銘柄
コードを指定すれば、その銘柄だけを投稿する。板情報には本日の出来高（株数）も
あわせて表示する（`docs/adr/0013`）。

最初はSocket Modeで実装したが、接続自体はできるのに `app_mention` イベントが実際には
配送されない現象が解消できず、HTTP Request URL方式（Slackがこのアプリの
`/slack/events` に直接POSTしてくる）に切り替えた（`docs/adr/0007` 参照）。

有効にするには:

1. リポジトリルートの [`slack-app-manifest.json`](../../slack-app-manifest.json) の内容を
   [api.slack.com/apps](https://api.slack.com/apps) の対象アプリ → **App Manifest** タブに
   貼り付けて保存（Bot Token Scopes・`app_mention` イベント購読が反映される。
   Socket Modeは使わないので `socket_mode_enabled: false` のまま）。保存後、上部の
   「Reinstall to Workspace」を実行する
2. **Basic Information** → App Credentials → **Signing Secret** を取得し、保存する:

   ```sh
   echo "..." > config/slack_signing_secret
   ```

3. `python3 web.py` をポート8381で公開するトンネル/リバースプロキシを用意する
   （このリポジトリでは `~/.cloudflared/config.yml` の名前付きトンネルに
   `sbi-order.awsm.jp → http://localhost:8381` を追加する形にした。無料の
   `cloudflared tunnel --url` クイックトンネルは、今回接続はできてもリクエストが
   届かない・404になる現象が頻発したため避けている）
4. **Event Subscriptions** → 有効化 → Request URL に `https://<公開URL>/slack/events`
   を入力（保存時にSlackが自動でURL検証してくる。`{"challenge": "..."}` を返せば
   「Verified」と表示される）→ Subscribe to bot events に `app_mention` を追加して保存

`config/slack_app_token` が無い場合、メンション機能は無効のまま（他の機能には影響しない）。

## セレクタの状態（2026-08-17 実画面で確認済み）

`_is_logged_in` / `check_order_status` / `get_price` / `get_order_book` / `place_order` /
`cancel_order` / `list_pending_order_ids` は、実際にログイン済みのブラウザに接続して
エンドツーエンドで動作確認済み。`place_order` は「注文確認画面を省略」を有効にして
確認画面を経由せず一発で発注する（`docs/adr/0009`）。はてな(3930)で複数回実発注・
実取消して確認した（`docs/adr/0005`, `0008`, `0009`）。

## 使い方

```sh
python3 web.py
```

起動時に普段使っているブラウザ（`SBI_CDP_URL`）へ接続し、ログイン済みか確認する。
接続できない場合や、ログインしておらず自動再ログインもできない場合（認証情報未設定・
OTP認証が必要等）は Slack の `SLACK_CHANNEL` に `SLACK_MENTION_USER` へのメンション付きで
知らせるので、その人が普段のブラウザを起動・ログインして解決する（自動突破はしない）。

`http://localhost:8381` を開き、銘柄コード・売買・株数・指値価格を入力して発注する。
発注はキューに積まれ、SBI/ブラウザとのやり取りを専属で行う単一スレッド（`_sbi_loop`）が
順番に処理する。Playwrightの同期APIはスレッドを跨いで使えない（Cannot switch to a
different threadで壊れる）ため、発注処理・約定確認・株価取得は全部このスレッド1つに
まとめてあり、HTTPサーバ側は `queue.Queue` 経由でしか関与しない。60秒おきに未約定の
注文を確認し、約定を検知したらmacOS通知 + `SLACK_CHANNEL` への投稿で知らせる。

`SBI_WATCH_TICKERS` を設定していれば、相場が開く日の8:00から `SBI_PRICE_POST_INTERVAL_SEC`
（既定600秒 = 10分毎、時計に揃えて 8:00, 8:10, ...）間隔で対象銘柄の板情報（気配値）を取得し、
`SLACK_CHANNEL` に `SLACK_MENTION_USER` 宛メンション付きで投稿し続ける。

## 構成

| ファイル | 役割 |
|---|---|
| `config.py` | `config/.env` の読み込み（他モジュールで共有） |
| `sbi_client.py` | Playwright によるログイン・発注・取消・約定確認・株価/板情報取得（全て実画面で確認済み） |
| `order_store.py` | 発注記録の永続化（SQLite, `config/orders.db`） |
| `rebid_session.py` | リビッドセッションの永続化（JSON, `config/rebid_session.json`）と買付済み株数の集計 |
| `notify.py` | macOS 通知（`osascript`） |
| `slack_client.py` | Slack投稿（bot token, `chat.postMessage`） |
| `mention_listener.py` | Slackメンションの受信・署名検証・コマンド解析（Events API, HTTP） |
| `web.py` | ローカルWeb UI + 発注ワーカー + 約定ポーラー + 板情報ポーラー + リビッドセッション実行 |

## 既知の制約

- 価格・板情報は SBI の認証済みセッションから見る前提（外部の株価APIは使っていない）
- ログインセッションは無操作60分程度で切れるとみられる。`ensure_logged_in` は各操作の前に
  毎回HOME_URLへ遷移してログイン状態を確認し（ログアウト直後の画面はDOMが古いままの
  ことがあり誤判定するため、必ず遷移してから判定する）、切れていれば
  `SBI_USER_ID`/`SBI_LOGIN_PASSWORD` を使った自動再ログインを試みる（`docs/adr/0012`）。
  ID/PWログインに登録メール宛のOTPが挟まった場合はそこで止まり、Slackで知らせる
  （メール受信箱へのアクセスが必要な認証はこのツールの自動化対象外という方針のため）
- 普段使っているブラウザを閉じる・リモートデバッグなしで再起動すると接続できなくなる。
  その場合も上記のコマンドで起動し直せば次のチェックで自動的に再接続する
- 板情報の投稿は板が動いている時間帯（前場8:00-11:30, 後場12:05-15:30, 平日のみ）に
  限定している（`_is_market_hours`）。寄り付き（9:00/12:30）より前から板寄せで
  気配が動くため、開始を前倒ししている。祝日カレンダーまでは見ていないので、
  祝日は無駄にポーリングされる（実害はないが、板は動いていないので投稿内容も
  代わり映えしない）
- 5%を超えて保有する場合は大量保有報告書、取得割合次第ではTOB規制など、
  このツールの外側で別途の法規制がかかる。対象・規模次第では専門家への相談が前提になる
