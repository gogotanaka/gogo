# gogo

個人用の自動化アプリ置き場。各アプリは `apps/<name>/` に独立して置き、
詳細はそれぞれの README、設計判断の経緯は `apps/<name>/docs/adr/` に残す。

## Apps

| アプリ | 概要 |
|---|---|
| [dashboard](apps/dashboard/) | 各 Slack workspace の Later / Activity / Drafts 件数と未読メンションを1画面表示(port 8380)。Drafts はサイドバー件数と一致するよう幽霊 draft を除外 |
| [tasker](apps/tasker/) | Notion MAIN + Slack Later を覗いて「まだこれやる?」を DM する秘書と、Notion todo のカテゴリ表示 web UI(port 8378) |
| [people-reminders](apps/people-reminders/) | Slack List「people-reminders」+ 紐づくスレッドを Claude に「終わってそうか」判定させて表示(port 8379) |
| [mf-pl](apps/mf-pl/) | Money Forward クラウド会計から月次 PL/BS を取得して Slack に投稿(複数社対応) |

Slack 系はいずれも Slack desktop の CDP (port 9222) から `xoxc` + `xoxd` を
取り出して内部 API を叩く方式。
