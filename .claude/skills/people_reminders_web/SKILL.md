---
name: people_reminders_web
description: apps/people-reminders/web.py を起動して Slack list「people-reminders」+スレッド判定の web UI (http://localhost:8379) をブラウザで開く
---

# people_reminders_web

`apps/people-reminders/web.py` を起動する。スクリプト側が `http://localhost:8379` を自動でブラウザに開く。

## 実行手順

以下を Bash ツールで実行する:

```bash
cd /Users/gogo/work/gogo/apps/people-reminders && python3 web.py
```

- 既に 8379 で起動中なら `lsof -i :8379` で PID を確認しユーザーに伝える（勝手に kill しない）
- フォアグラウンドで動き続けるので、バックグラウンド実行 (`run_in_background: true`) を推奨
- Slack desktop が CDP port 9222 で立ち上がっていない場合、初回 fetch で自動再起動を試みる（30秒程度かかる）
- `claude` CLI が PATH に無いと「終わってそうか」判定がスキップされる
- 別の list 名にしたい場合は `PEOPLE_REMINDERS_LIST=other-list` を前置
