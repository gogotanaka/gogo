#!/usr/bin/env python3
"""Push the current Slack-dashboard snapshot to a Cloudflare D1 database.

Reads ~/.config/dashboard/d1.json for {account_id, database_name}, runs
fetch_counts.collect() to gather counts + unread mentions for every
workspace, then shells out to `npx wrangler d1 execute <db> --remote
--file=<tmp.sql>` with a single batch that:

  - clears the `counts` and `mentions` tables
  - inserts the current snapshot

Usage:
    python3 push_d1.py            # collect + push
    python3 push_d1.py --init     # also (re-)apply schema.sql first
    python3 push_d1.py --dry-run  # print SQL, do not push
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_counts  # noqa: E402

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path.home() / ".config" / "dashboard" / "d1.json"
SCHEMA_PATH = HERE / "schema.sql"


def sql_lit(v):
    """Render a python value as a SQLite literal."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    # str / fallback
    s = str(v).replace("'", "''")
    return f"'{s}'"


def build_sql(rows, fetched_at):
    # wrangler d1 execute wraps the whole file in an implicit transaction,
    # so DO NOT emit BEGIN/COMMIT here (D1 rejects explicit txn statements).
    out = ["DELETE FROM counts;", "DELETE FROM mentions;"]
    for i, r in enumerate(rows):
        out.append(
            "INSERT INTO counts (team_id, name, domain, sidebar_order, "
            "later, later_overdue, activity, drafts, mentions_total, "
            "later_via, activity_via, drafts_via, error, fetched_at) VALUES ("
            + ", ".join(sql_lit(v) for v in [
                r.get("team_id"), r.get("name"), r.get("domain"), i,
                r.get("later"), r.get("later_overdue"),
                r.get("activity"), r.get("drafts"),
                r.get("mentions_total"),
                r.get("later_via"), r.get("activity_via"), r.get("drafts_via"),
                r.get("error"), fetched_at,
            ]) + ");"
        )
        for m in (r.get("mentions") or []):
            out.append(
                "INSERT OR REPLACE INTO mentions (team_id, ts, channel_id, "
                "channel_name, channel_is_im, user, username, text, permalink, "
                "thread_ts, fetched_at) VALUES ("
                + ", ".join(sql_lit(v) for v in [
                    r.get("team_id"), m.get("ts"),
                    m.get("channel_id"), m.get("channel_name"),
                    bool(m.get("channel_is_im")),
                    m.get("user"), m.get("username"),
                    m.get("text"), m.get("permalink"),
                    m.get("thread_ts"), fetched_at,
                ]) + ");"
            )
    return "\n".join(out) + "\n"


def run_wrangler(cfg, sql_path):
    cmd = ["npx", "wrangler", "d1", "execute", cfg["database_name"],
           "--remote", f"--file={sql_path}"]
    env = {**os.environ, "CLOUDFLARE_ACCOUNT_ID": cfg["account_id"]}
    p = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stdout)
        sys.stderr.write(p.stderr)
        raise SystemExit(f"wrangler failed (exit {p.returncode})")
    # Keep stderr (wrangler's progress) muted on success; show a one-liner.
    return p.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true",
                    help="apply schema.sql before pushing")
    ap.add_argument("--dry-run", action="store_true",
                    help="print SQL, do not push")
    args = ap.parse_args()

    if not CONFIG_PATH.exists():
        raise SystemExit(f"missing config: {CONFIG_PATH}")
    cfg = json.loads(CONFIG_PATH.read_text())

    if args.init and not args.dry_run:
        print(f"[push_d1] applying schema {SCHEMA_PATH}", file=sys.stderr)
        run_wrangler(cfg, str(SCHEMA_PATH))

    print("[push_d1] collecting…", file=sys.stderr)
    rows = fetch_counts.collect()
    fetched_at = int(time.time())
    sql = build_sql(rows, fetched_at)

    if args.dry_run:
        sys.stdout.write(sql)
        return

    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as f:
        f.write(sql)
        sql_path = f.name
    try:
        print(f"[push_d1] pushing {len(rows)} workspaces + "
              f"{sum(len(r.get('mentions') or []) for r in rows)} mentions "
              f"-> {cfg['database_name']}", file=sys.stderr)
        run_wrangler(cfg, sql_path)
        print(f"[push_d1] ok at {fetched_at}", file=sys.stderr)
    finally:
        os.unlink(sql_path)


if __name__ == "__main__":
    main()
