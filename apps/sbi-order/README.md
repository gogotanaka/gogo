# sbi-order — SBI証券 指値注文の自動発注・約定通知

指定した銘柄・株数・価格で成行/指値注文を出し、約定したら通知する。
板情報（気配値）を定期的にSlackチャンネルへ流す機能もある。
**「いつ・いくら買うか」の判断は人間が行う前提**で、発注操作・約定確認・株価共有だけを
自動化する（価格条件を見て自動で買い判断をするロジックはこのアプリのスコープ外）。

ブラウザは Playwright が新規に立ち上げるのではなく、**普段使っているブラウザに
CDP (Chrome DevTools Protocol) 経由で接続する**方式にした。SBIのパスキーはブラウザの
プロファイルに紐づく（まっさらな自動化専用プロファイルには passkey が無い）ため、
ログイン自体は普段のブラウザで人間がパスキーで行い、このツールはそのタブに接続して
発注・約定確認・株価取得だけを行う（`apps/mf-pl/send_slack.py` が Slack に対して
使っているCDPフォールバックと同じ発想）。ログインが必要な状態（未ログイン・
接続できない等）になったら、Slackで人（`SLACK_MENTION_USER`）にメンションして
知らせるので、その人が普段のブラウザでログインする。

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
- パスキーは自動化していない・できない。ログインは常に人間が普段のブラウザで行う
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
SBI_TRADE_PASSWORD=取引パスワード   # 発注に必須。無いと place_order がエラーになる

# 株価監視 → Slack通知
SBI_WATCH_TICKERS=3930              # カンマ区切りで複数銘柄コード指定可（例: 3930,7203）
SBI_PRICE_INTERVAL_MIN_SEC=1200     # 取得間隔の下限（秒）。既定20分
SBI_PRICE_INTERVAL_MAX_SEC=1800     # 取得間隔の上限（秒）。既定30分。毎回この範囲でランダムに決める
SLACK_CHANNEL=C0BQEBW40V9           # 株価・約定・ログイン依頼を投稿するチャンネルID
SLACK_MENTION_USER=U09GPTXH00H      # ログイン対応が必要なときにメンションするユーザーID
```

Slack bot トークンも別ファイルで置く（`chat:write` スコープ、上記チャンネルに招待済みであること）:

```sh
echo "xoxb-..." > config/slack_bot_token
```

`SLACK_CHANNEL` / `SLACK_MENTION_USER` を空にしておけば、その機能（株価投稿・ログイン依頼の
メンション）は無効化され、macOS通知のみになる。

## セレクタの状態（2026-08-17 実画面で確認済み）

`_is_logged_in` / `check_order_status` / `get_price` / `get_order_book` / `place_order`
（発注〜確認画面〜最終発注〜注文番号取得まで）は、実際にログイン済みのブラウザに接続して
エンドツーエンドで動作確認済み。はてな(3930) 現物買 200株 指値742円で実際に発注し
（注文番号487）、`docs/adr/0005` に記録した。

## 使い方

```sh
python3 web.py
```

起動時に普段使っているブラウザ（`SBI_CDP_URL`）へ接続し、ログイン済みか確認する。
接続できない・ログインしていない場合は Slack の `SLACK_CHANNEL` に `SLACK_MENTION_USER`
へのメンション付きで知らせるので、その人が普段のブラウザを起動 or ログインして解決する
（自動突破はしない）。

`http://localhost:8381` を開き、銘柄コード・売買・株数・指値価格を入力して発注する。
発注はキューに積まれ、SBI/ブラウザとのやり取りを専属で行う単一スレッド（`_sbi_loop`）が
順番に処理する。Playwrightの同期APIはスレッドを跨いで使えない（Cannot switch to a
different threadで壊れる）ため、発注処理・約定確認・株価取得は全部このスレッド1つに
まとめてあり、HTTPサーバ側は `queue.Queue` 経由でしか関与しない。60秒おきに未約定の
注文を確認し、約定を検知したらmacOS通知 + `SLACK_CHANNEL` への投稿で知らせる。

`SBI_WATCH_TICKERS` を設定していれば、`SBI_PRICE_INTERVAL_MIN_SEC`〜`SBI_PRICE_INTERVAL_MAX_SEC`
（既定20〜30分）の範囲でランダムな間隔をおいて対象銘柄の板情報（気配値）を取得し、`SLACK_CHANNEL` に
投稿し続ける。固定間隔にしていないのは、機械的なアクセスパターンを避けるため。

## 構成

| ファイル | 役割 |
|---|---|
| `config.py` | `config/.env` の読み込み（他モジュールで共有） |
| `sbi_client.py` | Playwright によるログイン・発注・約定確認・株価/板情報取得（発注含め実画面で確認済み） |
| `order_store.py` | 発注記録の永続化（SQLite, `config/orders.db`） |
| `notify.py` | macOS 通知（`osascript`） |
| `slack_client.py` | Slack投稿（bot token, `chat.postMessage`） |
| `web.py` | ローカルWeb UI + 発注ワーカー + 約定ポーラー + 株価ポーラー |

## 既知の制約

- 価格・板情報は SBI の認証済みセッションから見る前提（外部の株価APIは使っていない）
- ログインセッションは無操作60分程度で切れるとみられる。`ensure_logged_in` は各操作の前に
  毎回ログイン状態を確認するが、自動での再ログインは行わない（パスキーなので人間が
  普段のブラウザで再ログインする必要がある）。切れていたらSlackで知らせる
- 普段使っているブラウザを閉じる・リモートデバッグなしで再起動すると接続できなくなる。
  その場合も上記のコマンドで起動し直せば次のチェックで自動的に再接続する
- 株価監視は市場が開いていない時間帯も含めて20〜30分間隔で動き続ける
  （取引時間帯だけに絞る機能は無いので、必要なら `web.py` の起動・停止で調整する）
- 5%を超えて保有する場合は大量保有報告書、取得割合次第ではTOB規制など、
  このツールの外側で別途の法規制がかかる。対象・規模次第では専門家への相談が前提になる
