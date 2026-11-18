"""SQLite storage layer shared by the three services (WAL mode).

Consistency model:
  * detail_versions is append-only; `version` is a global monotonic counter
    kept in meta['version'] and bumped inside the ingest transaction.
  * A revision batch pins `boundary_version` = the counter value *before* the
    batch is applied. Every report for that batch is computed from the state
    `version <= boundary_version` plus exactly this batch's changes, so all
    reports of one revision share one identical input boundary.
"""
import sqlite3
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
  batch_id         TEXT PRIMARY KEY,
  boundary_version INTEGER NOT NULL,
  n_changes        INTEGER NOT NULL,
  status           TEXT NOT NULL DEFAULT 'received',  -- received|computing|computed|failed
  created_at       TEXT NOT NULL,
  computed_at      TEXT
);

CREATE TABLE IF NOT EXISTS detail_versions (
  detail_id TEXT NOT NULL,
  version   INTEGER NOT NULL,
  region    TEXT NOT NULL,
  product   TEXT NOT NULL,
  channel   TEXT NOT NULL,
  amount    INTEGER NOT NULL,   -- cents
  qty       INTEGER NOT NULL,   -- units
  deleted   INTEGER NOT NULL DEFAULT 0,
  batch_id  TEXT NOT NULL,
  PRIMARY KEY (detail_id, version)
);
CREATE INDEX IF NOT EXISTS idx_dv_version ON detail_versions(version);
CREATE INDEX IF NOT EXISTS idx_dv_batch   ON detail_versions(batch_id);

CREATE TABLE IF NOT EXISTS recompute_jobs (
  batch_id   TEXT PRIMARY KEY,
  status     TEXT NOT NULL DEFAULT 'pending',         -- pending|running|done|failed
  progress   TEXT NOT NULL DEFAULT '[]',              -- JSON: finished report ids
  attempts   INTEGER NOT NULL DEFAULT 0,
  error      TEXT,
  updated_at TEXT
);

-- Per-batch impact: only the *affected* aggregate rows plus the revised
-- details' contributions (delta storage), not every intermediate contribution.
CREATE TABLE IF NOT EXISTS impact_rows (
  batch_id   TEXT NOT NULL,
  report_id  TEXT NOT NULL,
  key_json   TEXT NOT NULL,
  old_amount INTEGER NOT NULL,
  old_qty    INTEGER NOT NULL,
  new_amount INTEGER NOT NULL,
  new_qty    INTEGER NOT NULL,
  contribs   TEXT NOT NULL,                            -- JSON per-detail old/new
  PRIMARY KEY (batch_id, report_id, key_json)
);

CREATE TABLE IF NOT EXISTS report_diffs (
  batch_id   TEXT NOT NULL,
  report_id  TEXT NOT NULL,
  old_amount INTEGER NOT NULL,
  old_qty    INTEGER NOT NULL,
  new_amount INTEGER NOT NULL,
  new_qty    INTEGER NOT NULL,
  PRIMARY KEY (batch_id, report_id)
);

CREATE TABLE IF NOT EXISTS packages (
  batch_id             TEXT PRIMARY KEY,
  generation           INTEGER NOT NULL DEFAULT 0,    -- latest build generation
  published_generation INTEGER,                       -- generation that owns the artifact
  status               TEXT NOT NULL DEFAULT 'idle',  -- idle|building|cancelled|done|failed
  artifact_path        TEXT,
  artifact_sha256      TEXT,
  updated_at           TEXT
);

CREATE TABLE IF NOT EXISTS build_tasks (
  task_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id         TEXT NOT NULL,
  generation       INTEGER NOT NULL,
  status           TEXT NOT NULL DEFAULT 'pending',   -- pending|running|done|cancelled|failed|superseded
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  total_steps      INTEGER NOT NULL DEFAULT 0,
  done_steps       INTEGER NOT NULL DEFAULT 0,
  error            TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_batch ON build_tasks(batch_id);

-- Resume checkpoints: canonical payload of each finished report file.
CREATE TABLE IF NOT EXISTS build_checkpoints (
  batch_id  TEXT NOT NULL,
  report_id TEXT NOT NULL,
  payload   BLOB NOT NULL,
  sha256    TEXT NOT NULL,
  PRIMARY KEY (batch_id, report_id)
);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def connect():
    config.ensure_dirs()
    con = sqlite3.connect(config.db_path(), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init_db():
    config.ensure_dirs()
    con = connect()
    con.executescript(SCHEMA)
    con.execute("INSERT OR IGNORE INTO meta(k, v) VALUES ('version', '0')")
    con.commit()
    con.close()
