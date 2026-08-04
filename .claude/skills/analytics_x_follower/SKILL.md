---
name: analytics_x_follower
description: 普段使いのGoogle ChromeをAppleScript経由で操作してX(Twitter)のフォロワー一覧を取得・分析する。DOMから@handle/名前/bio/verifiedを収集→各プロフィールを巡回してfollowers_count/following_count/i_follow/直近投稿/protectedをSQLiteに保存→SQLで分析。「Xのフォロワー分析して」「@xxxのフォロワー一覧出して」「直近100人のフォロワー見て」「DBに保存して」等のリクエストで使う。
---

# analytics_x_follower

普段使いの Google Chrome（既にXにログインしているプロファイル）をそのまま使い、AppleScript で X プロフィール DOM から情報を抽出する。

**2段アーキテクチャ**:
1. `followers` ページからフォロワー一覧（screen_name / name / bio / verified）を DOM 抽出 → JSON
2. 各プロフィールページを巡回して `followers_count` / `following_count` / `i_follow`（自分がフォロー中か）/ `location` / `joined` / `website` / `protected`（鍵アカ）/ 直近の投稿 を抽出 → **SQLite**

`followers` ページ単体からは follower_count / following_count は取れない（X は React fiber を DOM に attach せず、後付け fetch hook も塞いでいる）。プロフィール巡回経路では DOM の `a[href$="/verified_followers"]` / `a[href$="/following"]` の innerText から取得し、鍵アカは primaryColumn テキストの正規表現フォールバックで拾う。

## 前提

1. Google Chrome が起動していること（ログイン済み）。
2. Chrome の `View → Developer → Allow JavaScript from Apple Events` がオン。
   - 一度だけ手動で有効化が必要。オフだと `osascript` でJS実行ができずエラーになる。
3. 普通の Python3 が `osascript` を呼べる環境（macOS）。
4. CDP は使わない（Slack desktop が 9222 を専有しているのと、ログインを引き継ぐためにユーザープロファイルの再起動はしたくない）。

## 使い方（推奨: DB経路）

```bash
# 1. フォロワー一覧収集 (1〜2分) → JSON
python3 .../scripts/run.py gogo_tanaka --no-it       # 全件 (439人前後)

# 2. 各プロフィールを巡回して full データを SQLite に保存 (約5秒/人 → 全439人で35〜40分)
python3 .../scripts/collect_profile_to_db.py /tmp/x_followers_gogo_tanaka.json --skip-existing

# 3. SQL で分析
sqlite3 /tmp/x_followers_gogo_tanaka.db "SELECT screen_name, name, followers_count FROM profiles WHERE i_follow=1 ORDER BY followers_count DESC LIMIT 20;"
```

`--skip-existing` は DB に既に follower_count が入っているユーザーを飛ばすので、中断→再開や差分実行に使える。

## 出力先

| 用途 | パス |
|---|---|
| フォロワー一覧 JSON | `/tmp/x_followers_<user>.json` (`--out` で変更可) |
| プロフィール+投稿 DB | `/tmp/x_followers_<user>.db` (`--db` で変更可) |

`/tmp` は macOS の再起動で消える点に注意。永続化したい場合は `--db ~/.local/share/x_analytics/<user>.db` 等を指定。

## DB スキーマ

```sql
CREATE TABLE profiles (
  screen_name      TEXT PRIMARY KEY,
  name             TEXT,
  description      TEXT,           -- bio
  verified         INTEGER,        -- 0/1
  protected        INTEGER,        -- 0/1 (鍵アカ)
  followers_count  INTEGER,        -- 鍵アカでもテキスト fallback で取れる
  following_count  INTEGER,
  i_follow         INTEGER,        -- 0/1/NULL: 自分がフォロー中か
  location         TEXT,
  joined           TEXT,           -- "2019年4月からXを利用しています" など
  website          TEXT,
  url              TEXT,           -- https://x.com/<screen_name>
  collected_at     TEXT            -- ISO datetime
);
CREATE TABLE posts (
  screen_name TEXT,
  post_id     TEXT,                -- status id
  text        TEXT,
  posted_at   TEXT,                -- ISO datetime (time[datetime])
  url         TEXT,                -- https://x.com/<sn>/status/<id>
  PRIMARY KEY (screen_name, post_id)
);
```

UPSERT で更新されるので再実行は安全。

## 旧経路 (JSON のみ)

```bash
# follower_count だけ JSON にマージしたい時 (DB 不要・軽量)
python3 .../scripts/enrich_followers.py /tmp/x_followers_gogo_tanaka.json --skip-with-count

# bio キーワードでフィルタしてレポート
python3 .../scripts/filter_report.py /tmp/x_followers_gogo_tanaka.json --min-followers 1000 --category founder
```

## よく使う SQL レシピ

```sql
-- 相互フォローのうちフォロワー多い順
SELECT screen_name, name, followers_count FROM profiles WHERE i_follow=1 ORDER BY followers_count DESC LIMIT 30;

-- 自分がフォローしてないけど follower 1000+ かつ bio に AI/founder
SELECT screen_name, name, followers_count, substr(description,1,60)
FROM profiles
WHERE (i_follow IS NULL OR i_follow=0)
  AND followers_count >= 1000
  AND (description LIKE '%AI%' OR description LIKE '%founder%' OR description LIKE '%起業%')
ORDER BY followers_count DESC;

-- 最近 AI 系投稿をしているフォロワー
SELECT p.screen_name, profiles.name, p.posted_at, substr(p.text,1,80)
FROM posts p JOIN profiles ON p.screen_name=profiles.screen_name
WHERE p.text LIKE '%AI%' OR p.text LIKE '%LLM%'
ORDER BY p.posted_at DESC LIMIT 50;

-- フォロワー数分布
SELECT CASE
  WHEN followers_count<100 THEN '<100'
  WHEN followers_count<1000 THEN '100-999'
  WHEN followers_count<10000 THEN '1k-10k'
  ELSE '10k+' END AS bucket, count(*)
FROM profiles GROUP BY bucket;
```

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
