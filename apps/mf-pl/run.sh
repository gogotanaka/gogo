#!/bin/bash
# 月次の売上/PL/BS を Money Forward クラウド会計から取得して Slack に投稿する。
#
# Usage:
#   ./run.sh                       # 全社の先月売上高サマリーを1通で Slack へ（デフォルト）
#   ./run.sh --full                # 会社ごとの PL+BS 詳細を投稿
#   ./run.sh --month 2026-06       # 対象月を指定
#   ./run.sh --profile aisaac      # (--full 時) 1社だけ
#   ./run.sh --dry-run             # 送信せずメッセージを表示
#
# 投稿先: config/slack_channel（または MF_PL_CHANNEL）
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
MONTH=""
PROFILE=""
DRY_RUN=0
FULL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --month) MONTH="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --full) FULL=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

send() {
  if [ "$DRY_RUN" = "1" ]; then
    echo "---- generated message ----"
    cat
    echo "---- (dry-run, not sent) ----"
  else
    python3 "$DIR/send_slack.py"
  fi
}

if [ "$FULL" = "0" ]; then
  echo "[mf-pl] fetching sales summary..." >&2
  if [ -n "$MONTH" ]; then
    python3 "$DIR/sales.py" --month "$MONTH" | send
  else
    python3 "$DIR/sales.py" | send
  fi
  exit 0
fi

if [ -n "$PROFILE" ]; then
  PROFILES="$PROFILE"
else
  PROFILES=$(cd "$DIR" && python3 -c "import auth; print('\n'.join(auth.list_profiles()))")
  if [ -z "$PROFILES" ]; then
    echo "[mf-pl] プロファイルがありません。初回認可: python3 $DIR/auth.py <profile>" >&2
    exit 1
  fi
fi

for p in $PROFILES; do
  echo "[mf-pl] $p: fetching PL/BS..." >&2
  ARGS=(--profile "$p")
  [ -n "$MONTH" ] && ARGS+=(--month "$MONTH")
  PL=$(python3 "$DIR/fetch_pl.py" "${ARGS[@]}" --report pl | python3 "$DIR/format_pl.py")
  BS=$(python3 "$DIR/fetch_pl.py" "${ARGS[@]}" --report bs | python3 "$DIR/format_pl.py")
  printf '%s\n\n%s\n' "$PL" "$BS" | send
done
