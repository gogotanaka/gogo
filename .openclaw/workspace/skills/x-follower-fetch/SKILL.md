---
name: x-follower-fetch
description: "X (Twitter) のフォロワーリストを取得し、フォロワー数またはIT業界キーワードでフィルタリングして結果をSlackスレッドに貼る。Use when user wants to fetch/list Twitter/X followers with follower count filtering (e.g. 'フォロワー1000以上を出して', 'Xのフォロワーリスト取って', '直近50人のフォロワーを見て'). Requires Chrome browser relay (attached tab on x.com) or can open x.com itself."
---

# X Follower Fetch

Chrome ブラウザ経由で X のフォロワーページを開き、React fiber tree から全フォロワーのデータを抽出する。

## パラメータ

| 引数 | デフォルト | 説明 |
|---|---|---|
| `minFollowers` | 1000 | フォロワー数のしきい値（これ以上はマッチ） |
| `maxScrolls` | 30 | 最大スクロール回数 |
| `limit` | 0 | 直近何人分を収集するか（0=無制限）。「直近50人」などの指定に対応 |
| `includeITPros` | true | バイオのキーワードでIT系プロを追加抽出 |

## フィルタリング条件

以下のいずれかに該当するユーザーをピックアップする：

1. **フォロワー数 >= minFollowers**（デフォルト 1000）
2. **IT業界の優秀そうな人**（バイオにキーワードが含まれる場合）
   - エンジニア: `エンジニア`, `engineer`, `developer`, `CTO`, `SRE`, `devops` など
   - プロダクトマネージャー: `プロダクトマネージャ`, `product manager`, `PM` など
   - マーケ: `マーケティング`, `marketing`, `marketer`, `CMO`, `growth` など

結果の各ユーザーには `reason` フィールド（例: `フォロワー2,500 / IT系`）が付与される。

## 手順

### 1. フォロワーページを開く

```
profile="chrome" で browser(action=open, url="https://x.com/{username}/followers")
```

- Chrome extension relay を使う（`profile="chrome"`）
- ページが完全にロードされるまで待つ

### 2. スクリプトを実行してデータ取得

`scripts/extract_followers.js` の中身を `browser(action=act, kind=evaluate)` で実行する。

引数のカスタマイズ例：

```js
// 直近100人から抽出（limitを100に設定）
})(1000, 30, 100, true);

// フォロワー500以上 or IT系、制限なし
})(500, 30, 0, true);

// フォロワー数のみで判定（IT系フラグ無効）
})(1000, 30, 0, false);
```

スクリプト最終行の `})(1000, 30, 0, true)` の4つの数値を変える。

### 3. 結果を整形してSlackに投稿

返却JSONの `users` 配列を整形する：

```
1. @screen_name（name） - フォロワー数N人 [reason]
   bio: ...
2. ...
```

フォロワー数の多い順でソート済み。結果はスレッドに貼る（`threadId` 指定）。

## 注意点

- X はReactのfiber構造が変わることがあり、スクリプトが動かなくなることがある。その場合は `__reactFiber` のキー名や data path を調整する。
- ログインが必要。Chrome relay のアタッチ済みタブがXにログインしていること。
- `limit` は「直近N人を収集対象にする」もので、フィルタ後の出力件数ではない点に注意。
- スクロールが追いつかない場合は `maxScrolls` を増やす（デフォルト30）。
- 取得できる上限はXの仕様によるが、通常500〜1000件程度。
