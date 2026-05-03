#!/bin/bash
# Secretary: gather Notion todos + Slack Later, ask Claude to synthesize a
# casual "still doing this?" check-in, and DM it to self.
#
# Flags:
#   --dry-run   print the generated message instead of sending
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
DRY_RUN=0
COUNT=5
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    [0-9]*) COUNT="$arg" ;;
  esac
done

echo "[tasker] fetching Notion MAIN..." >&2
NOTION=$(python3 "$DIR/fetch_notion.py")

echo "[tasker] fetching Slack Later..." >&2
LATER=$(python3 "$DIR/fetch_later.py" 100)

PROMPT=$(cat <<EOF
$(cat "$DIR/secretary.md")

---

# 今回の件数指定
リマインド項目数は **最大${COUNT}件** に絞って。優先度の高いもの／古くて放置されてるものを選ぶ。

# 現在時刻
$(date "+%Y-%m-%d %H:%M %Z")

# Notion MAIN ページ（todoの山）
\`\`\`
$NOTION
\`\`\`

# Slack Later（保存済みアイテム、新しい順）
\`\`\`json
$LATER
\`\`\`

---

上記を踏まえて、ユーザーへのカジュアルなチェックイン1通を作って。Slackにそのまま投稿できる形式で、メッセージ本文だけを出力して（前置きや解説は不要）。
EOF
)

echo "[tasker] asking Claude..." >&2
MSG=$(echo "$PROMPT" | claude -p --model claude-sonnet-4-6)

if [ "$DRY_RUN" = "1" ]; then
  echo "---- generated message ----"
  echo "$MSG"
  echo "---- (dry-run, not sent) ----"
else
  echo "[tasker] sending DM..." >&2
  echo "$MSG" | python3 "$DIR/send_dm.py"
fi
