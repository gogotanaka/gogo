---
name: analytics_x_follower
description: 普段使いのGoogle ChromeをAppleScript経由で操作してX(Twitter)のフォロワー一覧を取得・分析する。DOMから@handle/名前/bio/verifiedを収集し、bioキーワードでカテゴリ分け(エンジニア/起業/AI/PM/マーケ/デザイン/VC等)してレポート。「Xのフォロワー分析して」「@xxxのフォロワー一覧出して」「直近100人のフォロワー見て」等のリクエストで使う。
---

# analytics_x_follower

普段使いの Google Chrome（既にXにログインしているプロファイル）をそのまま使い、AppleScript で `https://x.com/<user>/followers` を開いて DOM からフォロワー情報を抽出する。

**重要な制約**: 2026年時点のXは React fiber を DOM に attach しなくなり、`window.fetch` / `XMLHttpRequest` を後から hook しても捕捉できない（X側が初期化時に originalFetch をクロージャに抱え込むため）。したがって **follower_count はこの方式では取れない**。取れるのは `@screen_name` / `display_name` / `bio` / `verified` のみ。フィルタは bio キーワードと verified のみで行う。

## 前提

1. Google Chrome が起動していること（ログイン済み）。
2. Chrome の `View → Developer → Allow JavaScript from Apple Events` がオン。
   - 一度だけ手動で有効化が必要。オフだと `osascript` でJS実行ができずエラーになる。
3. 普通の Python3 が `osascript` を呼べる環境（macOS）。
4. CDP は使わない（Slack desktop が 9222 を専有しているのと、ログインを引き継ぐためにユーザープロファイルの再起動はしたくない）。

## 使い方

3ステップ:

```bash
# 1. フォロワーDOM収集 (1〜2分)
python3 .../scripts/run.py gogo_tanaka --no-it     # 全件収集 (439人前後)

# 2. follower_count をエンリッチ (各プロフィールを巡回, 1件約2秒)
python3 .../scripts/enrich_followers.py /tmp/x_followers_gogo_tanaka.json --skip-with-count
# 全439件で約15分。--limit N で部分エンリッチも可。

# 3. フィルタしてレポート
python3 .../scripts/filter_report.py /tmp/x_followers_gogo_tanaka.json --min-followers 300
python3 .../scripts/filter_report.py /tmp/x_followers_gogo_tanaka.json --min-followers 1000 --category founder
python3 .../scripts/filter_report.py /tmp/x_followers_gogo_tanaka.json --category engineer --top 30
```

`run.py` の直接フィルタ機能（旧仕様の `--min-followers`）は follower_count が無いため動きません。エンリッチ→フィルタの2段構えに統一しています。

例:

```bash
# gogo_tanaka のフォロワーを、フォロワー300以上 or IT系で抽出（既定）
python3 .../run.py gogo_tanaka

# 直近100人のフォロワーから抽出、しきい値500
python3 .../run.py gogo_tanaka --min-followers 500 --limit 100

# フォロワー数だけで判定（IT系判定なし）
python3 .../run.py gogo_tanaka --no-it

# 全収集ユーザー中フォロワー上位50人もレポート末尾に追加
python3 .../run.py gogo_tanaka --top-all 50
```

結果は標準出力に Markdown 風に整形、生 JSON は `--out`（既定 `/tmp/x_followers_<user>.json`）に保存。

## パラメータ

| 引数 | 既定 | 説明 |
|---|---|---|
| `--min-followers` | 300 | このフォロワー数以上を「マッチ」とする |
| `--max-scrolls` | 30 | フォロワーページの最大スクロール回数 |
| `--limit` | 0 | 直近何人分を収集対象にするか（0=無制限）。「直近50人」指定に対応 |
| `--no-it` | off | バイオのキーワードによるIT系判定を無効化 |
| `--top-all` | 0 | 全収集ユーザー中フォロワー数上位N人をレポート末尾に追加 |
| `--out` | `/tmp/x_followers_<user>.json` | 生JSONの保存先 |

## フィルタリング条件

以下のいずれかに合致するユーザーを `users` として返す（`reason` フィールドに根拠）：

1. **followers_count >= min-followers**
2. **bio に IT系キーワード**（`--no-it` でオフ）
   - エンジニア系: `engineer`, `developer`, `CTO`, `SRE`, `devops`, `backend`, `frontend`, `ML`, `AI`, `エンジニア`, `開発`, ...
   - PM/マーケ/起業: `product manager`, `PM`, `marketing`, `CMO`, `growth`, `founder`, `CEO`, `起業`, ...
   - デザイナー: `designer`, `デザイナー`

## 仕組み

- `scripts/run.py` は AppleScript で:
  1. 既存タブに `x.com/<user>/followers` があれば前面化、無ければ新規タブで開く
  2. ページ遷移を待つ
  3. `scripts/extract_followers.js`（base64 でラップして渡す）を `execute javascript` で投入
  4. JS 側は非同期にスクロール+収集して結果を `window.__x_followers_result` に置く
  5. Python が `window.__x_followers_done` を 1.5秒間隔でポーリングし、完了したら JSON を取り出す
- JS は `[data-testid="UserCell"]` の中の `[data-testid^="UserAvatar-Container-"]` から `@screen_name`、`a[role=link] span` から name、`[data-testid="UserDescription"]` から bio を取る。fallback として `innerText` 分解も用意。
- 重複は `screen_name` で除外、画面表示順（=フォローが新しい順）で並ぶ。

## 注意

- AppleScript の `execute javascript` は Promise を待たないので、結果は `window` に置いて poll する設計。`window.__x_followers_running` で多重起動防止。
- 取得できる上限は X の遅延読み込み仕様（だいたい最新400〜1000件）。それ以上はスクロールしても虚空。
- ログアウト状態のタブで開くと UserCell が出てこないので 0件になる。
- **follower_count は取得できない**。試みた方法（React fiber, fetch hook, XHR hook, SW cache）はすべて X 側で塞がれている。どうしても必要なら各 `https://x.com/<screen_name>` を個別に巡る必要があるが N requests になるので未実装。

## トラブルシュート

- `Executing JavaScript through AppleScript is turned off`: Chrome の View → Developer → Allow JavaScript from Apple Events を有効化。
- `URL did not become '/followers'`: ログインしていない or リダイレクトされている。Chrome で該当アカウントに手動ログイン。
- 結果が `total_collected: 0`: fiber 構造変更の可能性。`extract_followers.js` の `findUserInFiber` を最新の memoizedProps 構造に合わせて修正。
