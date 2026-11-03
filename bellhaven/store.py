"""SQLite state: proposals + their decisions, and a log of runs.

Re-run safety: a proposal's id is a fingerprint of what it would change.
If a run regenerates a fingerprint that already exists (pending, approved,
rejected, applied...) it is NOT re-inserted, so decided items never come back.
Pending proposals that a later run no longer generates are marked 'stale'
(the CRM or website changed underneath them) so nobody approves outdated data.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("SYNC_DB", os.path.join(os.path.dirname(__file__), "..", "state", "sync.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT, finished_at TEXT,
  n_locations INTEGER, n_accounts INTEGER,
  n_new_proposals INTEGER, n_stale INTEGER, report TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY,
  kind TEXT, title TEXT, summary TEXT,
  location_slug TEXT, account_id TEXT,
  ops TEXT, evidence TEXT,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected|applied|failed|stale
  first_run INTEGER, last_seen_run INTEGER,
  decided_at TEXT, reviewer_comment TEXT,
  result TEXT, created_ids TEXT DEFAULT '{}'
);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def connect(path=None):
    path = path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def record_run(con, locations, accounts, proposals, report, started_at):
    cur = con.execute("INSERT INTO runs (started_at, n_locations, n_accounts) VALUES (?,?,?)",
                      (started_at, len(locations), len(accounts)))
    run_id = cur.lastrowid
    new = 0
    ids = set()
    for p in proposals:
        ids.add(p["id"])
        row = con.execute("SELECT status FROM proposals WHERE id=?", (p["id"],)).fetchone()
        if row:
            con.execute("UPDATE proposals SET last_seen_run=? WHERE id=?", (run_id, p["id"]))
            if row["status"] == "stale":  # came back unchanged -> reopen
                con.execute("UPDATE proposals SET status='pending' WHERE id=?", (p["id"],))
            continue
        con.execute(
            "INSERT INTO proposals (id, kind, title, summary, location_slug, account_id, ops, evidence,"
            " first_run, last_seen_run) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (p["id"], p["kind"], p["title"], p["summary"], p["location_slug"], p["account_id"],
             json.dumps(p["ops"]), json.dumps(p["evidence"]), run_id, run_id))
        new += 1
    stale = con.execute(
        "UPDATE proposals SET status='stale' WHERE status='pending' AND last_seen_run < ?", (run_id,)).rowcount
    con.execute("UPDATE runs SET finished_at=?, n_new_proposals=?, n_stale=?, report=? WHERE id=?",
                (now(), new, stale, json.dumps(report), run_id))
    con.commit()
    return run_id, new, stale


def load(row):
    d = dict(row)
    for k in ("ops", "evidence", "result", "created_ids"):
        if d.get(k):
            d[k] = json.loads(d[k])
    return d
