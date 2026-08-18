# 0002: sbi-order の通知先をSlackに、ブラウザを常時起動に変更

## Status

Accepted (2026-08-17)

## Context

[0001](0001-sbi-order-automation.md) のMVPに対し、ユーザーから追加要望が出た:

- まず株価監視を作り、指定のSlackチャンネル（`C0BQEBW40V9`）に通知してほしい
- ブラウザは毎回開き直さず常時立ち上げておく形にしたい
- 最初はheadless（画面非表示）にしない方がよい
- ログイン対応が必要なときは特定のSlackユーザー（`U09GPTXH00H`）にメンションしてほしい

## Decision

- `SBIClient` の起動・ログインを `web.py` の `main()` で起動時に1回だけ行い、以後
  プロセス終了まで同じブラウザ（headed）を使い回す（0001時点では発注時に遅延起動していた）
- `HumanInterventionRequired`（ログイン失敗・想定外の画面など）を検知したら、
  `SLACK_CHANNEL` に `<@SLACK_MENTION_USER>` 付きでメッセージを投稿する。
  自動でCAPTCHA等を突破しようとはせず、人間がheadedブラウザ画面を直接操作して解決する前提は
  0001から変更していない
- 同じ通知を連投しないよう、ログイン依頼のSlackメンションは状態が変わるまで一度だけに絞った
  （`_login_alert_sent` フラグ、再ログイン成功でクリア）
- 株価監視 (`get_price`) を追加。`SBI_WATCH_TICKERS` に設定した銘柄を
  `SBI_PRICE_INTERVAL_SEC`（既定5分）おきに取得し `SLACK_CHANNEL` に投稿する
- 約定通知も macOS通知に加えて `SLACK_CHANNEL` へ投稿するようにした
  （常時ブラウザ運用だとPCの前にいない時間が増える想定のため）
- Slack投稿は `apps/mf-pl/send_slack.py` のbotトークン方式を踏襲。CDPフォールバックは
  複雑さの割に本アプリでは必要性が薄いため省略し、bot token必須にした

## Consequences

- ブラウザを常時起動しておくことで、0001で指摘した「ログインセッションが60分程度で切れる」
  問題への対処は「切れたらSlackで人を呼ぶ」という運用でカバーする形になった
  （無人で再ログインし続ける設計にはしていない＝パスキー同様、想定外の認証要求に自動対応しない方針を維持）
- 株価監視は市場時間外も含めて一定間隔でSBIへアクセスし続ける。0001で触れた「過大なアクセス」
  リスクを増やす方向の変更だが、5分間隔程度であれば実務上は許容範囲と判断した
- Slack bot tokenが `config/slack_bot_token` に必要になった（新規で用意が要る。他アプリの
  config/ とは共有していない）
