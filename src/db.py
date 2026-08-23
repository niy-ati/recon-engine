"""
SQLite persistence layer for the reconciliation pipeline.

Two tables, one file (data/reconcile.db), stdlib sqlite3 only:

  exceptions      -- one row per reconciliation result, keyed on match_key
                     (stable across runs of the same batch -- see
                     reconcile.py for how it's built), with a replay_log
                     (the stage-by-stage trace reconcile.py builds) so a
                     reviewer can see why the pipeline landed on this
                     outcome.
  narration_rules -- confirmed-match memory. A human confirming a match in
                     review_server.py writes here automatically, and
                     reconcile.py's Pass 2.5 reads from here before ever
                     building a fuzzy shortlist.

persist_results() upserts on match_key instead of deleting and reinserting:
re-running the same batch (a settlement recon job runs daily against a
growing dataset, not once) must never erase a human's earlier confirm or
reject. Once a row has a terminal resolution_status (CONFIRMED/REJECTED),
a later run can still see it, but its result fields stay frozen -- see
persist_results()'s SQL for the exact mechanism.
"""
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "reconcile.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS exceptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_key TEXT NOT NULL,
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


def _migrate(conn):
    """One-time migration for a data/reconcile.db created before match_key
    existed. New databases get match_key from SCHEMA directly; this only
    runs the ALTER/backfill/index steps when the column is actually
    missing, so it's a no-op on every later call."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(exceptions)")}
    if "match_key" not in columns:
        conn.execute("ALTER TABLE exceptions ADD COLUMN match_key TEXT")
        conn.execute(
            "UPDATE exceptions SET match_key = COALESCE(settlement_id, order_id, 'legacy:' || id) "
            "WHERE match_key IS NULL"
        )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_exceptions_match_key ON exceptions(match_key)")


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def persist_results(results, run_id):
    """Upserts every result row into `exceptions` on match_key. A row whose
    resolution_status is still OPEN gets its fields fully refreshed from
    this run. A row a human already CONFIRMED or REJECTED keeps its frozen
    fields -- the WHERE clause on the UPDATE branch below is what makes
    that true, not application-level logic that could be skipped."""
    conn = get_connection()
    try:
        with conn:
            for r in results:
                match_key = r.get("match_key")
                if not match_key:
                    raise ValueError(f"result row missing match_key: {r}")
                needs_action = "yes" if r["status"] in ("EXCEPTION", "MATCHED_LOW_CONFIDENCE") else "no"
                conn.execute(
                    """INSERT INTO exceptions
                       (match_key, run_id, order_id, settlement_id, net_amount, status, category,
                        reason, narration, needs_action, replay_log)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(match_key) DO UPDATE SET
                           run_id = excluded.run_id,
                           order_id = excluded.order_id,
                           settlement_id = excluded.settlement_id,
                           net_amount = excluded.net_amount,
                           status = excluded.status,
                           category = excluded.category,
                           reason = excluded.reason,
                           narration = excluded.narration,
                           needs_action = excluded.needs_action,
                           replay_log = excluded.replay_log
                       WHERE exceptions.resolution_status = 'OPEN'""",
                    (match_key, run_id, r.get("order_id"), r.get("settlement_id"), r.get("net"),
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
