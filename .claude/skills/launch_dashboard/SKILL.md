---
name: launch_dashboard
description: apps/dashboard/web.py を起動して、各 Slack workspace の Later/Activity/Drafts の件数を表示する web UI (http://localhost:8380) をブラウザで開く
---

# launch_dashboard

`apps/dashboard/web.py` を起動する。スクリプト側が `http://localhost:8380` を自動でブラウザに開く。

## 実行手順

以下を Bash ツールで実行する:

```bash
cd /Users/gogo/work/gogo/apps/dashboard && python3 web.py
```

- 既に 8380 で起動中なら `lsof -i :8380` で PID を確認しユーザーに伝える（勝手に kill しない）
- フォアグラウンドで動き続けるので、バックグラウンド実行 (`run_in_background: true`) を推奨
- Slack desktop が CDP port 9222 で立ち上がっていない場合、初回 fetch で自動再起動を試みる（30秒程度かかる）
- Activity / Drafts の数値が `—` で出る場合は内部 API 名がズレている。`apps/dashboard/fetch_counts.py` の `count_activity` / `count_drafts` の candidates に追記して試す
