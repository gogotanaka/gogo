# 0006: Slackメンションからの発注（Socket Mode）

## Status

Accepted (2026-08-17)

## Context

ユーザーから、Slackのメンションを受け取ってそこから発注できるようにしたいという依頼が
あった。受信方式としてポーリング（既存の`_sbi_loop`に相乗り、追加スコープのみで済む）と
Socket Mode（リアルタイム、Slackアプリ側でのSocket Mode有効化・app-level token発行が
別途必要）を提示し、ユーザーはSocket Modeを選んだ。

0005で `place_order` が確認画面〜最終発注まで無人で完結するようになったため、この機能は
「Slackにメッセージを送るだけで実際に株が買える」という、これまでで最も直接的な発注経路に
なる。安全設計を特に意識した。

## Decision

- `mention_listener.py` を追加。`slack_bolt` の `SocketModeHandler` でSlackの
  `app_mention` イベントを受信する。このモジュールはSlackイベントの受信・検証・返信だけを
  行い、Playwrightには一切触れない（発注そのものは既存の `_work_q` → `_sbi_loop` の
  パイプラインに乗せる。0004で確立した「SBI/ブラウザに触るのは_sbi_loopだけ」という
  スレッド境界を維持するため）
- コマンド書式は `{買い|売り|buy|sell} 銘柄コード 株数 価格`（例:
  `@bot 買い 3930 200 742`）に固定。曖昧な自然文解析はせず、正規表現に一致しない場合は
  何もせず使い方を返信するだけにした
- 安全のため3段のチェックを設けた:
  1. 発言者が `SLACK_MENTION_USER` と一致しない場合は無視（権限エラーを返信するのみ）
  2. 書式が厳密に一致しない場合は無視（使い方を返信するのみ）
  3. 見積金額（株数×価格）が `SBI_MAX_ORDER_VALUE_YEN`（既定50万円）を超える場合は拒否
     （誤入力によるファットフィンガー的な暴走の歯止め）
- 受け付けたコマンドは即座に「受け付けました」と返信し、`order_store` に登録した上で
  `_work_q` に積む。実際の発注結果（成功時は注文番号、失敗時はエラー内容）は、
  `_sbi_loop` 側の `_process_order` が処理完了後に同じSlackスレッドへ返信する
  （`order_id -> (channel, thread_ts)` を `_mention_reply_targets` で対応付け）
- Web UI経由の発注はこの対応付けに登録されないため、`_reply_mention_result` は
  何もせず無視するだけになり、既存の発注フローには影響しない

## Consequences

- Slackでメッセージを送るだけで実際の注文が発注される状態になった。0005で書いた
  「発注そのものは取引パスワードさえあれば無人で完結できる」というリスクが、
  「Slackから離れた場所・状況からでも発注できる」という形でさらに一段階進んだ
- 上記の安全のための3段チェック（送信者制限・厳密な書式・金額上限）を実装したが、
  これらはあくまでソフトウェア上の歯止めであり、Slackアカウント（`SLACK_MENTION_USER`）
  自体が乗っ取られた場合は無効。Slack側のアカウントセキュリティ（2要素認証等）が
  この機能の実質的な前提になる
- Socket Mode有効化にはSlackアプリ管理画面での作業（`app_mentions:read`スコープ追加・
  再インストール、Socket Mode有効化とapp-level token発行、Event Subscriptions設定）が
  ユーザー側で必要。`config/slack_app_token` が無い間はこの機能は無効のまま起動する
  （他の機能には影響しない）
- `slack_bolt`（および依存の`slack_sdk`）を新規依存として追加した
