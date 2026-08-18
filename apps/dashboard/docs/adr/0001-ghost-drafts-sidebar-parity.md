# 0001: Drafts 件数は Slack サイドバーとの一致を正とし、幽霊 draft を除外する

## Status

Accepted (2026-08-16, PR #5 として main へマージ)

## Context

ダッシュボードは workspace ごとの下書き件数を Slack 内部 API `drafts.list` で
数えている。ところが Slack 本体のサイドバーに表示される「Drafts」件数と
食い違うケースが相次いで見つかった。ユーザーが Slack 上で見る数字と
ダッシュボードの数字がズレていると、ダッシュボードの数字全体が信用できなく
なるため、**「サイドバーが表示する件数」を正とし、それに厳密に一致させる**
ことをこのカウンタの仕様とする。

`drafts.list` は非公開 API でドキュメントがなく、サイドバーには出ない
「幽霊 draft」を返すことがある。実データの調査(該当 draft を 1 件ずつ
API で突き合わせ)で、以下のパターンを確認した。

**調査で確認した幽霊パターン**

1. **送信済み・削除済み** — `is_sent=true` / `is_deleted=true` のレコードが
   履歴として返る(以前から除外済み)
2. **宛先チャンネルがアーカイブ済み** — `conversations.info` で
   `is_archived=true`(以前から除外済み)
3. **宛先チャンネルが削除済み** — `conversations.info` が
   `channel_not_found` を返す。aisaac workspace で実例を確認(サイドバーは
   0 件なのにダッシュボードは 1 件と表示していた)
4. **スレッド宛 draft の親スレッドが削除済み** — 宛先に `thread_ts` を持つ
   draft は、スレッドの root メッセージが削除されても `drafts.list` に
   残り続ける。`conversations.replies` がその `thread_ts` に対して
   `thread_not_found` を返すことで判別できる。supa-jp workspace で実例を
   確認(削除済みスレッドへの返信 draft が 1 件残っていた)

## Decision

`count_drafts()` は以下をすべて除外した件数を返す:

- `is_sent` / `is_deleted` な draft
- 全宛先が「hidden」な draft。宛先チャンネルが hidden とは
  - `conversations.info` で `is_archived=true`、または
  - `conversations.info` が `channel_not_found` を返す(チャンネル削除済み)
- 宛先が `thread_ts` 付きで、その (channel, thread_ts) に対する
  `conversations.replies` が `thread_not_found`(または `message_not_found`)
  を返す draft(親スレッド消滅)

**設計ルール: 過小カウントより過大カウントに倒す。** draft を隠してよいのは
API が「存在しない」と確定的に答えたときだけ。ネットワークエラーや 429 等の
一時的な失敗はすべて「表示扱い」にフォールバックする(最悪でも修正前と同じ
件数に劣化するだけで、実在する draft を隠すことはない)。

API 呼び出しコストは、アクティブ draft のユニークな宛先チャンネルごとに
`conversations.info` 1 回 + ユニークな (channel, thread_ts) ごとに
`conversations.replies` 1 回(いずれも ThreadPoolExecutor で並列)。
アクティブ draft は通常ごく少数なので実用上問題ない。

## 検証

- 修正後にキャッシュを消して live fetch し、aisaac / supa-jp とも Drafts が
  0 になり、他 workspace の件数に変化がないことを http://localhost:8380/ で確認
- スレッド生存確認の diff はマルチエージェントレビュー(correctness /
  Slack API 挙動 / regression の 3 レンズ + 指摘ごとに 2 名の敵対的検証)に
  かけ、確認された欠陥 0 件。検証の過程で、実セッションから存在しない
  root ts への `conversations.replies` が常に `thread_not_found` を返すことを
  実証済み

## 未検証・留意点

- `message_not_found` は公式エラー表になく実データでも未観測。防御的に
  入れているだけで、なくても挙動は変わらない
- スレッド宛 draft が大量にある workspace でのレートリミット挙動は未検証
  (429 時は表示扱いに倒れるだけの安全側設計)
- 幽霊パターンは今後も新種が見つかる可能性がある。サイドバーとズレたら
  「該当 draft を `drafts.list` で特定 → 宛先を API で突き合わせる」の手順で
  原因パターンを特定し、ここに追記する

なお、同じ一連の作業で awsm workspace の Later CLEAR 閾値を 10 に設定した
(`web.py` の `LATER_CLEAR_THRESHOLD["awsm-inc"]`。設定変更であり
アーキテクチャ決定ではないので詳細は割愛)。
