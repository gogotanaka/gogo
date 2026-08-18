# 0002: キャッシュ層の堅牢化と /refresh の POST 化

## Status

Accepted (2026-08-11 作成、2026-08-16 に PR #4 として main へマージ)

## Context

全 workspace の Slack fetch は 1 回に 1 分前後かかるため、`slack_cache.json`
に TTL 600 秒のキャッシュを持ち、HTTP サーバーはスレッド化されている
(キャッシュ層自体の導入はこの ADR 以前)。初期実装にはレビューで複数の
問題が見つかった:

- **`GET /refresh` が破壊的だった。** force sync するとブラウザの URL が
  /refresh に留まり、ページの `<meta http-equiv="refresh">`(600 秒)が
  発火するたびに「キャッシュ全削除 → フル Slack fetch」が永久に繰り返される
- キャッシュ削除が `exists()`/`unlink()` の 2 段階で、スレッド間の TOCTOU
  レースで `FileNotFoundError` になり得た。また force sync が進行中の fetch と
  競合し、失敗時に最後の正常キャッシュまで失われた
- キャッシュの鮮度判定がファイル mtime 基準で、payload 内の `fetched_at` と
  二重管理。型不正な payload で TypeError のエラーページが TTL の間固定される
- キャッシュ書き込みが非アトミック(読み手が書きかけのファイルを読み得る)
- 空・全 workspace エラーのスナップショットもキャッシュされ、一時障害が
  TTL の間ピン留めされた

## Decision

- **/refresh は POST + 303 リダイレクト**(`<form method="post">`)。
  処理後に `/` へ戻すので meta-refresh は常に `/` で発火する。素の
  `GET /refresh` は副作用なしで `/` へリダイレクトするだけ
- キャッシュ削除の代わりに **`get_rows(force=True)`**: 古いキャッシュを
  残したまま fetch し、成功したときだけ上書きする(失敗しても最後の正常
  スナップショットが残る)
- 鮮度判定は payload 内 `fetched_at` に一本化し、型・形状を検証。不正なら
  単なるキャッシュミス扱い
- 書き込みは tmp ファイル + `os.replace` でアトミック、かつ best-effort
  (書き込み失敗で fetch 結果を捨てない)
- 空・全エラーのスナップショットはキャッシュしない(次のリロードで再試行)
- ハンドラ共通化、エラーページは 500、`Cache-Control: no-store`、
  クエリ文字列を除去してからルーティング、hand-rolled の
  ThreadingMixIn クラスを stdlib の `http.server.ThreadingHTTPServer` に置換

## 検証

- マージ後の main でサーバーを再起動し、`GET /` → 200、`POST /refresh` →
  303 で `/` へ(force sync 実行)、`GET /refresh` → 副作用なしで 303、を確認
- live fetch → キャッシュ経由の応答とも正常動作を確認

## 留意点

- CACHE_TTL(600 秒)はページの meta-refresh 間隔と同値に揃えてある。
  自動リロードは常に live fetch になり、キャッシュは手動リロード・複数タブ・
  API ポーリングだけを吸収する。片方だけ変えるとこの関係が崩れる
