# 0007: Socket ModeからEvents API(HTTP)への切り替え

## Status

Accepted (2026-08-17)

## Context

0006でSocket Modeによるメンション発注を実装したが、実運用で「Slackアプリの設定
（bot scope・Enable Events・Subscribe to bot events・Socket Modeトグル）はすべて
正しいのに、実際に `app_mention` イベントが一度も配送されない」という現象に遭遇した。

診断の過程でわかったこと:
- `apps.connections.open` は毎回 `ok:true` を返し、WSS接続URLも取得できるが、
  レスポンスの `response_metadata.messages` に `[WARN] Socket Mode is not turned on.`
  という警告が一貫して出続けた（Slack側のUIでは「Socket Mode is enabled」と表示されて
  いたにもかかわらず）
- 接続自体は確立し `hello` メッセージも受信できるのに、実際のメンションイベントは
  一度も届かなかった（複数回、プロセスをクリーンに立て直しても再現）
- 送信者側の確認（メンションが実際に青いピル表示になっているか等）、Slack側の設定
  （Enable Events・Subscribe to bot events・招待状況）は全て問題なしと確認済み

この状態から抜け出せなかったため、Socket ModeをやめてHTTPのRequest URL方式
（Events API）に切り替えたところ、問題なく動作した。根本原因はSlack側（またはこの
特定アプリのSocket Mode状態）の不具合的な何かだったと推測されるが、特定には至って
いない。

## Decision

- `mention_listener.py` を全面的に書き直し、Socket Mode (`slack_bolt`,
  `SocketModeHandler`) をやめて、生のHTTP POSTを受けて自前で検証するHTTPハンドラに
  変更した。このリポジトリの他のSlack連携（mf-pl, dashboard, tasker,
  people-reminders）と同様、フレームワークを使わず標準ライブラリ（`hmac`,
  `hashlib`, `json`）で直接署名検証する流儀に揃えた
- `web.py` に `/slack/events` エンドポイントを追加。`url_verification` の
  challenge応答、`X-Slack-Signature`/`X-Slack-Request-Timestamp` を使った
  署名検証（リプレイ対策で5分以内のタイムスタンプのみ許容）、`X-Slack-Retry-Num`
  がある場合の再送スキップ（二重発注防止）を実装した
- 依存関係から `slack_bolt`/`slack_sdk` を削除し、`playwright` のみに戻した
- HTTPで受けるには公開URLが必要。当初は無料の cloudflared クイックトンネル
  （`cloudflared tunnel --url ...`、apps/mf-plのOAuthコールバックと同じ仕組み）を
  `web.py` 起動時に自動で立ち上げる実装にしたが、**接続自体はできてもリクエストが
  Cloudflareのエッジで404になり届かない**という、Socket Modeの時と類似した現象に
  遭遇した（アカウント無しのクイックトンネルはSLA無しとcloudflared自身が警告している
  通り、不安定だった可能性がある）
- そのため、この端末に既に存在した名前付きCloudflare Tunnel（`~/.cloudflared/config.yml`,
  tunnel名`main`。`dashboard.awsm.jp`等、他のアプリも同じトンネル経由で公開している）
  に `sbi-order.awsm.jp → http://localhost:8381` のingressルールを追加する方式に
  変更した。これによりURLが起動のたびに変わる問題も解消される
  - `cloudflared tunnel route dns main sbi-order.awsm.jp` でCNAMEを追加
  - `~/.cloudflared/config.yml` にingressエントリを追加
  - 実行中のトンネルプロセス（launchd管理ではなく手動起動されていた長時間プロセス
    だった）を再起動して設定を反映。この間、同じトンネルを使う他のサービス
    （`dashboard.awsm.jp`等）も一瞬止まる。ユーザーの許可を得た上で実施した
  - `web.py` 側の自動クイックトンネル起動ロジックは削除し、既に公開用トンネルが
    このポートを向いている前提のシンプルな実装に戻した

## Consequences

- メンション発注は最終的に実際のSlackメンションから実注文（はてな3930、100株、
  700円、SBI注文番号493）が通ることを確認した
- `sbi-order.awsm.jp` は他のアプリ（dashboard等）と同じ名前付きトンネルを共有する
  ようになった。今後このトンネルの設定・再起動は複数アプリに影響することを踏まえて
  行う必要がある
- Socket Modeで発生した「配送されない」現象の根本原因は未解明のまま。将来また
  似た問題が起きた場合は、まず `[slack-event] received ...` のようなリクエスト
  受信ログ（今回のデバッグで追加した）で「そもそも届いているか」を先に切り分けると
  早い
- ルートの `slack-app-manifest.json` は `socket_mode_enabled: false` とし、
  `event_subscriptions.request_url` に `https://sbi-order.awsm.jp/slack/events`
  を明記した（クイックトンネルと違い恒久的なURLなので、manifestに直接書いても
  陳腐化しない）
