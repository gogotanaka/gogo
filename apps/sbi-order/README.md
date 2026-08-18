# sbi-order — SBI証券 指値注文の自動発注・約定通知

指定した銘柄・株数・価格で成行/指値注文を出し、約定したら通知する。
板情報（気配値）を定期的にSlackチャンネルへ流す機能もある。
**「いつ・いくら買うか」の判断は人間が行う前提**で、発注操作・約定確認・株価共有だけを
自動化する（価格条件を見て自動で買い判断をするロジックはこのアプリのスコープ外）。

ブラウザは Playwright が新規に立ち上げるのではなく、**普段使っているブラウザに
CDP (Chrome DevTools Protocol) 経由で接続する**方式にした。SBIのパスキーはブラウザの
プロファイルに紐づく（まっさらな自動化専用プロファイルには passkey が無い）ため、
最初のログインは普段のブラウザで人間がパスキーで行い、このツールはそのタブに接続して
発注・約定確認・株価取得を行う（`apps/mf-pl/send_slack.py` が Slack に対して
使っているCDPフォールバックと同じ発想）。

ログアウトを検知した場合、`SBI_USER_ID`/`SBI_LOGIN_PASSWORD` が設定されていれば
ID・パスワードでの自動再ログインを試みる（`docs/adr/0012`）。パスキー自体は今後も
自動化しない（技術的にもできない）が、ID/PWログインは自動化できるため、通常の
セッション切れはこれで人手を介さず復旧する。ただしID/PWログインには登録メール
アドレス宛のOTP（追加認証コード）が挟まることがあり、これは自動化の対象外
（メール受信箱へのアクセスが必要なため）。OTPが必要な場合や認証情報が未設定の
場合は、Slackで人（`SLACK_MENTION_USER`）にメンションして知らせるので、その人が
普段のブラウザで続きを行う（OTP入力、またはパスキーでの再ログイン）。

設計の背景・調査した規制まわりの話は [docs/adr/0001-sbi-order-automation.md](../../docs/adr/0001-sbi-order-automation.md) と
[docs/adr/0003-sbi-order-cdp-attach.md](../../docs/adr/0003-sbi-order-cdp-attach.md) 参照。

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
SBI_PRICE_INTERVAL_MIN_SEC=480      # 取得間隔の下限（秒）。既定8分
SBI_PRICE_INTERVAL_MAX_SEC=720      # 取得間隔の上限（秒）。既定12分。毎回この範囲でランダムに決める
SLACK_CHANNEL=C0BQEBW40V9           # 板情報・約定・ログイン依頼を投稿するチャンネルID
SLACK_MENTION_USER=U09GPTXH00H      # ログイン依頼のメンション先／メンション発注を許可するユーザーID
SBI_MAX_ORDER_VALUE_YEN=500000      # メンション発注の上限見積金額（円）。超えたら拒否する
```

Slack bot トークンも別ファイルで置く（上記チャンネルに招待済みであること）:

```sh
echo "xoxb-..." > config/slack_bot_token
```

`SLACK_CHANNEL` / `SLACK_MENTION_USER` を空にしておけば、その機能（板情報投稿・ログイン依頼の
メンション）は無効化され、macOS通知のみになる。

### メンションでの発注（Events API, HTTP）

`@bot buy 3930 200 742`（buy/sell 銘柄コード 株数 価格。買い/売りでも可）のようにこのアプリのSlack botへ
メンションすると発注できる。安全のため: 発言者が `SLACK_MENTION_USER` と一致しない場合は
無視、書式が厳密に一致しない場合も無視（どちらも理由をスレッドに返信するだけで発注はしない）、
見積金額が `SBI_MAX_ORDER_VALUE_YEN`（既定50万円）を超える場合も拒否する。

`@bot clear all` で、現在未約定の注文を全て取消する（1件ずつ取消し、結果をスレッドに
まとめて返信する）。銘柄・数量の指定はできず、無条件に全部取消す点に注意。

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

`SBI_WATCH_TICKERS` を設定していれば、`SBI_PRICE_INTERVAL_MIN_SEC`〜`SBI_PRICE_INTERVAL_MAX_SEC`
（既定8〜12分、10分前後）の範囲でランダムな間隔をおいて対象銘柄の板情報（気配値）を取得し、`SLACK_CHANNEL` に
投稿し続ける。固定間隔にしていないのは、機械的なアクセスパターンを避けるため。

## 構成

| ファイル | 役割 |
|---|---|
| `config.py` | `config/.env` の読み込み（他モジュールで共有） |
| `sbi_client.py` | Playwright によるログイン・発注・取消・約定確認・株価/板情報取得（全て実画面で確認済み） |
| `order_store.py` | 発注記録の永続化（SQLite, `config/orders.db`） |
| `notify.py` | macOS 通知（`osascript`） |
| `slack_client.py` | Slack投稿（bot token, `chat.postMessage`） |
| `mention_listener.py` | Slackメンションの受信・署名検証・コマンド解析（Events API, HTTP） |
| `web.py` | ローカルWeb UI + 発注ワーカー + 約定ポーラー + 板情報ポーラー |

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
