-- D1 schema for apps/dashboard. Applied via `push_d1.py --init`.

CREATE TABLE IF NOT EXISTS counts (
  team_id        TEXT PRIMARY KEY,
  name           TEXT,
  domain         TEXT,
  sidebar_order  INTEGER,
  later          INTEGER,
  later_overdue  INTEGER,
  activity       INTEGER,
  drafts         INTEGER,
  mentions_total INTEGER,
  later_via      TEXT,
  activity_via   TEXT,
  drafts_via     TEXT,
  error          TEXT,
  fetched_at     INTEGER
);

CREATE TABLE IF NOT EXISTS mentions (
  team_id        TEXT NOT NULL,
  ts             TEXT NOT NULL,
  channel_id     TEXT,
  channel_name   TEXT,
  channel_is_im  INTEGER,
  user           TEXT,
  username       TEXT,
  text           TEXT,
  permalink      TEXT,
  thread_ts      TEXT,
  fetched_at     INTEGER,
  PRIMARY KEY (team_id, ts)
);

CREATE INDEX IF NOT EXISTS mentions_team_idx ON mentions (team_id);
CREATE INDEX IF NOT EXISTS mentions_ts_idx   ON mentions (ts DESC);
