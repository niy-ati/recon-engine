"""
SQLite persistence layer for the reconciliation pipeline.

Two tables, one file (data/reconcile.db), stdlib sqlite3 only:

  exceptions      -- one row per reconciliation result that needed a
                     decision (needs_action='yes'), with a replay_log (the
                     stage-by-stage trace reconcile.py builds) so a reviewer
                     can see why the pipeline landed on this outcome.
  narration_rules -- confirmed-match memory. A human confirming a match in
                     review_server.py writes here automatically, and
                     reconcile.py's Pass 2.5 reads from here before ever
                     building a fuzzy shortlist.
"""
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "reconcile.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS exceptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    order_id TEXT,
    settlement_id TEXT,
    net_amount REAL,
    status TEXT NOT NULL,
    category TEXT,
    reason TEXT,
    narration TEXT,
    needs_action TEXT NOT NULL,
    replay_log TEXT NOT NULL,
    resolution_status TEXT NOT NULL DEFAULT 'OPEN',
    resolution_note TEXT,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS narration_rules (
    narration TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    source TEXT NOT NULL
);
"""


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def persist_results(results, run_id):
    """Writes every result row into `exceptions`, replacing whatever was
    there before. `exceptions` represents the current batch's state, not an
    accumulating history, so a fresh run fully replaces the prior one
    instead of piling up alongside it. narration_rules is untouched and
    keeps accumulating across runs as intended."""
    conn = get_connection()
    try:
        with conn:
            conn.execute("DELETE FROM exceptions")
            for r in results:
                needs_action = "yes" if r["status"] in ("EXCEPTION", "MATCHED_LOW_CONFIDENCE") else "no"
                conn.execute(
                    """INSERT INTO exceptions
                       (run_id, order_id, settlement_id, net_amount, status, category,
                        reason, narration, needs_action, replay_log)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, r.get("order_id"), r.get("settlement_id"), r.get("net"),
                     r["status"], r.get("category"), r.get("reason"), r.get("narration", ""),
                     needs_action, json.dumps(r.get("stage", []))),
                )
    finally:
        conn.close()


def get_open_exceptions():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM exceptions WHERE needs_action = 'yes' AND resolution_status = 'OPEN' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_all_exceptions():
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM exceptions ORDER BY id").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def add_note(exception_id, note):
    """Attaches a note without resolving the row -- lets a reviewer record
    context without being forced into a binary confirm/reject decision.
    Row stays OPEN, stays in the queue."""
    conn = get_connection()
    try:
        with conn:
            conn.execute("UPDATE exceptions SET resolution_note = ? WHERE id = ?", (note, exception_id))
    finally:
        conn.close()


def resolve_exception(exception_id, action, note=None):
    """action: 'confirm' | 'reject' -- terminal decisions only.

    Confirming a FUZZY_MATCH_NEEDS_REVIEW row also writes a narration_rules
    entry, so the same narration resolves automatically next time."""
    status_map = {"confirm": "CONFIRMED", "reject": "REJECTED"}
    resolution_status = status_map.get(action)
    if resolution_status is None:
        raise ValueError(f"unknown action: {action}")

    conn = get_connection()
    try:
        with conn:
            row = conn.execute("SELECT * FROM exceptions WHERE id = ?", (exception_id,)).fetchone()
            if row is None:
                raise KeyError(f"no exception with id {exception_id}")

            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            conn.execute(
                "UPDATE exceptions SET resolution_status = ?, resolution_note = ?, resolved_at = ? WHERE id = ?",
                (resolution_status, note, now, exception_id),
            )

            if action == "confirm" and row["category"] == "FUZZY_MATCH_NEEDS_REVIEW" and row["narration"] and row["order_id"]:
                conn.execute(
                    "INSERT OR REPLACE INTO narration_rules (narration, order_id, confirmed_at, source) VALUES (?, ?, ?, ?)",
                    (row["narration"], row["order_id"], now, "human_review"),
                )
    finally:
        conn.close()


def get_narration_rule(narration):
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM narration_rules WHERE narration = ?", (narration,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None
