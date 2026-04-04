---
name: buy-snkrdunk
description: "スニーカーダンク（SNKRDUNK / snkrdunk.com）の商品を購入する。gogotanaka（Slack ID: U08KKHEQ3）もしくはayu Fujimoto（Slack ID: U09G83JA821）からの依頼時のみ発動。他ユーザーからの依頼では実行しないこと。カート確認・決済・UPSIDERカードの3DS認証（メールはicワークスペースのemail-for-upsiderチャンネルに届く）・購入完了確認まで一連で処理する。Use when gogotanaka asks to buy/purchase items on SNKRDUNK (スニダン)."
---

# buy-snkrdunk

スニダン（snkrdunk.com）を購入完了まで自動で進めるスキル。

## ⚠️ 発動制限

**このスキルはgogotanaka（Slack ID: U08KKHEQ3）もしくはayu Fujimoto（Slack ID: U09G83JA821）からの依頼時のみ実行すること。**

## 前提

- ブラウザ: `profile=openclaw`
- 支払いカード: UPSIDERカード（末尾3172）
- 3DS認証メールの受信先Slack: `C0AMXSGLB0C`

---

## 🚨 絶対に守るルール（2026-03-17 失敗から学んだ教訓）

> これを守らないと3DS承認が通っても購入は完了しない。

### ルール1: `browser open` で新しいタブを開いてはいけない
スニダンの3DS認証中に `browser open` で新しいタブを開くと、**元タブがfailedページに遷移**して購入が失敗する。

✅ 正しい: 既存のタブを `browser navigate` で再利用する  
❌ 間違い: `browser open` で新しいタブを開く

### ルール2: 承認後は必ず元タブのRefresh Pageをクリックする
UPSIDER承認だけでは不十分。**承認後に元タブのiframe内「Refresh Page」ボタンをクリックしないとスニダン側の購入完了処理が走らない。**

### ルール3: 3DS認証は10分以内に完了させる
メールが届いたらすぐに承認する。Slackやユーザーへの返信待ちで時間を使わない。

---

## 📸 スクリーンショット共有タイミング

各ステップ後に `browser screenshot` → `message action=send buffer=<path>` でSlackスレッドに共有:
1. カート確認後
2. 購入確認ページ（支払い方法確認後）
3. 3DS認証ページ表示時
4. 「取引は承認されました」画面
5. 購入完了画面

---

## 手順

### ステップ1: 商品をカートへ入れる

```
browser navigate profile=openclaw targetId=<既存タブ> url={商品ページのURL}
```

カートページ ( https://snkrdunk.com/cart?slide=view ) で内容・金額を確認。**📸 スクショ共有。**

### ステップ2: レジへ進む → 支払い方法確認

「レジへ進む」をクリックして購入確認ページへ。

**必ず** VISA ****3172 が選択されていることを確認してから「購入する」をクリック。  
配送先: 東京都 渋谷区 恵比寿西 2-14-7 パークアクシス代官山 502

**📸 スクショ共有後**に「購入する」クリック。

### ステップ3: 3DS認証 ← 一番重要・ミスりやすいステップ

「購入する」クリック後、スニダンのタブ（`cart/buy-confirm` または `payment/3ds`）に3DS認証iframeが表示される。

**このステップの元タブID（スニダンのタブ）を必ずメモしておく。**

#### 3-A: 承認メールを取得する（既存タブを再利用）

タブ一覧から使っていない既存タブを選ぶ（例: UPSIDERの古い承認ページ、Slackのタブ等）。

```
# 既存タブを Slackチャンネル `C0AMXSGLB0C`（aisaac ワークスペース）に遷移（browser open は絶対使わない）
browser navigate profile=openclaw targetId=<既存タブID> url=https://app.slack.com/client/T08KK9UCW/C0AMXSGLB0C
```

読み込み待ち後、最新メールのiframe srcを取得:

```js
// 既存タブ上で evaluate
const urls = Array.from(document.querySelectorAll('iframe'))
  .map(f => f.src)
  .filter(s => s.includes('files.slack.com'));
urls[urls.length - 1] // 末尾が最新のメール
```

> ⚠️ 必ず配列の**末尾**を使うこと。先頭は過去の別取引のメールで誤承認になる。

#### 3-B: 承認URLを取得して承認する（同じ既存タブで）

```
# 取得したSlack fileのURLに遷移
browser navigate profile=openclaw targetId=<既存タブID> url=<取得したfiles.slack.com URL>
```

承認URLを取得:

```js
Array.from(document.querySelectorAll('a[href*="3ds.up-sider.com"]'))
  .map(a => a.href)
// → [approve URL, decline URL]
```

承認URLに遷移:

```
browser navigate profile=openclaw targetId=<既存タブID> url=https://3ds.up-sider.com/visa/transactions/<uuid>/approve
```

「取引は承認されました」が表示されたら **📸 スクショ共有**。

#### 3-C: 元タブのRefresh Pageをクリックする ← 絶対に忘れない

承認完了直後に元タブのsnapshotを取る:

```
browser snapshot profile=openclaw targetId=<元タブID>
```

iframeの中に `Refresh Page` ボタンが見える（refは `f` で始まる、例: `f52e20`）:

```
browser act profile=openclaw targetId=<元タブID> {"kind": "click", "ref": "f52e20"}
```

> ✅ iframeの`f`始まりrefは `act` でそのまま指定可能（frame指定不要）。

### ステップ4: 購入完了確認

元タブのsnapshotまたはscreenshotで確認:

```
購入完了
商品が発送され次第、通知でお知らせします。
```

この文字列があれば購入完了 ✅ **📸 スクショを共有**してユーザーに報告。

---

## エラー対応

| 状況 | 原因 | 対処 |
|------|------|------|
| 元タブが `failed?message=決済期限を過ぎた...` に遷移 | 3DS認証10分タイムアウト | カートから再度「レジへ進む」でやり直す |
| 元タブが `failed` に遷移（承認前） | `browser open` で新しいタブを開いた | 次回から既存タブを `navigate` で再利用する |
| 3DS認証メールが届いていない | メール遅延 | 10〜30秒待ってSlackを再確認（最大3回） |
| 複数の認証メールがある | 過去の試行のメールが残っている | Slackのiframe配列の**末尾**（最新）を使う |
| 承認URLで「Verification not found」 | タイムアウトまたは承認済み | 購入フローからやり直す |
| Refresh Page後も3DSページのまま | 承認が反映されていない | 数秒待ってもう一度Refresh Pageをクリック |
| 「レジへ進む」がdisabled | 前回フローのロック中 | 2〜3分待ってからカートページをリロード |
| カートが空 | 商品が売り切れた | ユーザーに報告し別の出品を探す |
