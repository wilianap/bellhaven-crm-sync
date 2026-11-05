"""State: what has been decided (durable, in git) + the working queue (local cache).

* state/decisions.jsonl  - SOURCE OF TRUTH for decisions. Append-only, one JSON
  line per status change (approved / rejected / applied / failed). It is text,
  committed to git, and marked `merge=union` in .gitattributes, so decisions made
  on different machines merge without conflicts. Last line per proposal id wins.
* state/sync.db          - local SQLite cache of proposals, evidence and runs.
  Not committed; it can be deleted at any time and rebuilt by a run, which
  re-applies decisions.jsonl.

Re-run safety: a proposal's id is a fingerprint of what it would change.
If a run regenerates a fingerprint that is already in the queue or in
decisions.jsonl, it is NOT re-proposed, so decided items never come back.
Pending proposals that a later run no longer generates are marked 'stale'
(the CRM or website changed underneath them) so nobody approves outdated data.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state")
DB_PATH = os.environ.get("SYNC_DB", os.path.join(STATE_DIR, "sync.db"))
DECIDED = ("approved", "rejected", "applied", "failed")

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


class _Con(sqlite3.Connection):
    decisions_path = None


def connect(path=None, decisions_path=None):
    """Open the local cache. decisions.jsonl defaults to $SYNC_DECISIONS, else sits next to the db."""
    path = os.path.abspath(path or DB_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, factory=_Con)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.decisions_path = (decisions_path or os.environ.get("SYNC_DECISIONS")
                          or os.path.join(os.path.dirname(path), "decisions.jsonl"))
    sync_decisions(con)
    return con


def read_decisions(path):
    """Latest decision per proposal id from the append-only log."""
    latest = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    d = json.loads(line)
                    if d.get("type") == "survivor_override":
                        continue
                    latest[d["id"]] = d
    return latest


def read_survivor_overrides(path):
    """{website slug: account id} chosen by a reviewer to break a survivor tie (last line wins)."""
    out = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    d = json.loads(line)
                    if d.get("type") == "survivor_override":
                        out[d["slug"]] = d["account_id"]
    return out


def log_survivor_override(con, slug, account_id):
    with open(con.decisions_path, "a") as f:
        f.write(json.dumps({"type": "survivor_override", "id": f"survivor:{slug}", "slug": slug,
                            "account_id": account_id, "logged_at": now()}, sort_keys=True) + "\n")


def log_decision(con, proposal_id):
    """Append the proposal's current decision state to decisions.jsonl."""
    r = con.execute("SELECT id, kind, title, account_id, location_slug, status, decided_at, reviewer_comment,"
                    " result, created_ids FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    d = dict(r)
    for k in ("result", "created_ids"):
        d[k] = json.loads(d[k]) if d[k] else None
    d["logged_at"] = now()
    with open(con.decisions_path, "a") as f:
        f.write(json.dumps(d, sort_keys=True) + "\n")


def sync_decisions(con):
    """Bring the local cache in line with decisions.jsonl (e.g. after a git pull)."""
    for pid, d in read_decisions(con.decisions_path).items():
        con.execute(
            "UPDATE proposals SET status=?, decided_at=?, reviewer_comment=?, result=?, created_ids=? "
            "WHERE id=? AND status != ?",
            (d["status"], d.get("decided_at"), d.get("reviewer_comment"),
             json.dumps(d["result"]) if d.get("result") else None,
             json.dumps(d.get("created_ids") or {}), pid, d["status"]))
    con.commit()


def record_run(con, locations, accounts, proposals, report, started_at):
    cur = con.execute("INSERT INTO runs (started_at, n_locations, n_accounts) VALUES (?,?,?)",
                      (started_at, len(locations), len(accounts)))
    run_id = cur.lastrowid
    new = 0
    decided = read_decisions(con.decisions_path)
    for p in proposals:
        row = con.execute("SELECT status FROM proposals WHERE id=?", (p["id"],)).fetchone()
        if not row and p["id"] in decided:
            # Decided on another machine / before this cache existed: keep it as history, not as new work.
            d = decided[p["id"]]
            con.execute(
                "INSERT INTO proposals (id, kind, title, summary, location_slug, account_id, ops, evidence,"
                " status, first_run, last_seen_run, decided_at, reviewer_comment, result, created_ids)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["id"], p["kind"], p["title"], p["summary"], p["location_slug"], p["account_id"],
                 json.dumps(p["ops"]), json.dumps(p["evidence"]), d["status"], run_id, run_id,
                 d.get("decided_at"), d.get("reviewer_comment"),
                 json.dumps(d["result"]) if d.get("result") else None, json.dumps(d.get("created_ids") or {})))
            continue
        if row:
            # Same change, fresher evidence (e.g. a new tie flag or contacts) - evidence isn't fingerprinted.
            con.execute("UPDATE proposals SET last_seen_run=?, evidence=?, summary=? WHERE id=?",
                        (run_id, json.dumps(p["evidence"]), p["summary"], p["id"]))
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
