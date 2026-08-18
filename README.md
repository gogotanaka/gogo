# gogo

個人用ツール群のモノレポ。各アプリは `apps/` 配下で独立して動く（フレームワークなし、
標準ライブラリ中心）。

| アプリ | 概要 |
|---|---|
| [dashboard](apps/dashboard/) | 各Slack workspaceのLater/Activity/Draftsの件数を1画面で表示する個人用ダッシュボード |
| [mf-pl](apps/mf-pl/) | Money Forwardクラウド会計から月次PL/BSを取得してSlackに投稿 |
| [people-reminders](apps/people-reminders/) | Slack Listと紐づくスレッドを見て「終わってそうか」をClaudeに判定させ、Webで表示 |
| [sbi-order](apps/sbi-order/) | SBI証券の指値注文・約定通知・板情報のSlack投稿を自動化 |
| [tasker](apps/tasker/) | Notion/Slack Laterを見て「まだこれやる？」をDMする秘書 + todoのカテゴリ表示 |

複数アプリで共有するSlack app manifestは [`slack-app-manifest.json`](slack-app-manifest.json)。
