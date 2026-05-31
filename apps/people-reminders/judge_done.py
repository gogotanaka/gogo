#!/usr/bin/env python3
"""Ask Claude to judge whether each people-reminders item looks done.

Reads JSON (output of fetch_list.py) from stdin or a file argument,
asks `claude -p` once with all rows, and emits the same JSON with two
extra keys per row: `done_guess` (one of: done / unclear / open) and
`done_reason` (one short Japanese sentence).

If Claude is unavailable or the parse fails, every row gets done_guess=
"unclear" and the original data is preserved.

Usage:
  python3 fetch_list.py | python3 judge_done.py
"""
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

MODEL = os.environ.get("PEOPLE_REMINDERS_MODEL", "claude-sonnet-4-6")
BATCH_SIZE = int(os.environ.get("PEOPLE_REMINDERS_BATCH", "20"))
PARALLEL = int(os.environ.get("PEOPLE_REMINDERS_PARALLEL", "4"))
BATCH_TIMEOUT = int(os.environ.get("PEOPLE_REMINDERS_BATCH_TIMEOUT", "240"))


def build_prompt(rows):
    compact = []
    for i, r in enumerate(rows):
        thread = [
            {
                "ts": m.get("ts", ""),
                "when": m.get("when", ""),
                "who": m.get("user_name") or m.get("user", "?"),
                "text": (m.get("text", "") or "")[:300],
            }
            for m in r.get("thread", [])
        ]
        compact.append({
            "i": i,
            "title": r.get("title", ""),
            "columns": r.get("columns", {}),
            "thread": thread,
        })
    body = json.dumps(compact, ensure_ascii=False, indent=2)
    return f"""あなたは「人にリマインドする項目」のリストを見て、各項目が **終わってそうか** を判定するアシスタント。

各項目は以下を持つ:
- title: リマインド内容
- columns: Slack list の他の列
- thread: 関連スレッドのメッセージ列（古い順）。各メッセージに ts, when, who, text。空のこともある。

スレッドの内容から、相手から返信がもらえたか／お礼が来たか／タスクが完了したことを示す発言があるかなどを根拠に、以下のいずれかを返す:
- "done"    … 完了していそう
- "unclear" … 判断材料が足りない／スレッドが無い
- "open"    … まだ未完了・要フォロー

**done と判定する場合** は、その根拠となるスレッドメッセージの `ts` を **必ず** `evidence_ts` 配列に入れる（複数可、最も決定的なもの優先）。done 以外は `evidence_ts: []` でよい。

各項目について **必ず `summary` も返す**: その todo が「誰に何をリマインドしているか」を 1行40〜80字程度の日本語で要約。固有名詞・対象物は省かない。

出力形式は **必ず以下のJSONのみ** （前置きや説明禁止）:
```
[{{"i": 0, "summary": "@Sone に今後の戦略をチャンネルに投下するよう依頼", "guess": "done|unclear|open", "reason": "短い日本語の根拠", "evidence_ts": ["1779527934.365909"]}}, ...]
```

入力:
```
{body}
```
"""


def parse_reply(text):
    # find first JSON array in the reply
    m = re.search(r"\[\s*\{.*?\}\s*\]", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _fallback(rows, reason):
    for r in rows:
        r["summary"] = ""
        r["done_guess"] = "unclear"
        r["done_reason"] = f"judge未実行 ({reason})"
        r["evidence_ts"] = []
    return rows


def judge_batch(batch_rows):
    """Run claude on one batch of rows. Returns {local_i: {summary, guess, reason, evidence_ts}}."""
    prompt = build_prompt(batch_rows)
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", MODEL],
            input=prompt, capture_output=True, text=True, timeout=BATCH_TIMEOUT,
        )
    except FileNotFoundError:
        return {"_error": "claude CLI なし"}
    except subprocess.TimeoutExpired:
        return {"_error": "timeout"}
    if result.returncode != 0:
        err = (result.stderr or "")[:120].replace("\n", " ")
        return {"_error": f"exit {result.returncode}: {err}"}
    parsed = parse_reply(result.stdout)
    if parsed is None:
        print(f"[judge_done] parse fail on batch: {result.stdout[:300]}", file=sys.stderr)
        return {"_error": "parse fail"}
    return {p.get("i"): p for p in parsed if isinstance(p, dict)}


def judge(rows):
    if not rows:
        return rows

    # Quick existence check on claude CLI before spawning workers
    try:
        subprocess.run(["claude", "--version"],
                       capture_output=True, timeout=5, check=False)
    except FileNotFoundError:
        print("[judge_done] claude CLI not found; skipping", file=sys.stderr)
        return _fallback(rows, "claude CLI なし")

    # Split into batches; each batch is a list of (orig_index, row) pairs.
    batches = []
    for start in range(0, len(rows), BATCH_SIZE):
        chunk = list(enumerate(rows))[start:start + BATCH_SIZE]
        batches.append(chunk)

    print(f"[judge_done] judging {len(rows)} rows in {len(batches)} "
          f"batches of <={BATCH_SIZE}, parallel={PARALLEL}", file=sys.stderr)

    def run_one(batch):
        # batch is [(orig_i, row), ...]; build a temporary list with local i
        local_rows = [r for _, r in batch]
        result = judge_batch(local_rows)
        return batch, result

    results = []
    with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
        for done in ex.map(run_one, batches):
            results.append(done)

    # Merge back into rows
    for batch, result in results:
        if "_error" in result:
            err = result["_error"]
            print(f"[judge_done] batch failed: {err}", file=sys.stderr)
            for _, r in batch:
                r["summary"] = ""
                r["done_guess"] = "unclear"
                r["done_reason"] = f"judge未実行 ({err})"
                r["evidence_ts"] = []
            continue
        for local_i, (orig_i, r) in enumerate(batch):
            p = result.get(local_i, {})
            r["summary"] = p.get("summary", "")
            r["done_guess"] = p.get("guess", "unclear")
            r["done_reason"] = p.get("reason", "")
            ev = p.get("evidence_ts") or []
            thread_ts = {m.get("ts") for m in r.get("thread", []) if m.get("ts")}
            r["evidence_ts"] = [t for t in ev if isinstance(t, str) and t in thread_ts]

    judged = sum(1 for r in rows if r.get("done_guess") and not r.get("done_reason", "").startswith("judge未実行"))
    print(f"[judge_done] judged {judged}/{len(rows)} successfully", file=sys.stderr)
    return rows


def main():
    src = sys.stdin if len(sys.argv) < 2 else open(sys.argv[1])
    rows = json.load(src)
    rows = judge(rows)
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
