---
name: tasker_web
description: apps/tasker/tasker_web.py を起動して Notion todo カテゴリ表示の web UI (http://localhost:8378) をブラウザで開く
---

# tasker_web

`apps/tasker/tasker_web.py` を起動する。スクリプト側が `http://localhost:8378` を自動でブラウザに開く。

## 実行手順

以下を Bash ツールで実行する:

```bash
cd /Users/gogo/work/gogo/apps/tasker && python3 tasker_web.py
```

- 既に 8378 で起動中なら `lsof -i :8378` で PID を確認しユーザーに伝える（勝手に kill しない）
- `~/.config/notion/api_key` が無いと失敗する。エラー時はその点を確認する
- フォアグラウンドで動き続けるので、バックグラウンド実行 (`run_in_background: true`) を推奨
