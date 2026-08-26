"""
Postgres-backed drop-in replacement for src/db.py, used only by the Vercel
deployment (api/index.py substitutes this module into sys.modules["db"]
before review_server.py or settlement_qa.py ever runs `import db`, so
neither file is edited to know this exists).

Exists because Vercel's serverless functions are stateless and don't get a
writable local disk that survives between invocations -- src/db.py's
SQLite file can't be the persistence layer here. This mirrors db.py's
public function signatures and behavior (including the resolve_exception
concurrency-race guard, and the narration_rules/narration_templates
learning writes) against a real Postgres database (Neon) instead, using
psycopg3's dict_row so `row["col"]` and `dict(row)` keep working exactly
like sqlite3.Row did in the original.

Kept out of src/ on purpose: src/ is genuinely zero-third-party-dependency
(see requirements.txt), and this file's only reason to exist is a
psycopg3 dependency the local desktop app has no need for.
"""
import json
import os
import re
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS exceptions (
        id SERIAL PRIMARY KEY,
        match_key TEXT NOT NULL UNIQUE,
        run_id TEXT NOT NULL,
        order_id TEXT,
        settlement_id TEXT,
        net_amount DOUBLE PRECISION,
        status TEXT NOT NULL,
        category TEXT,
        reason TEXT,
        narration TEXT,
        needs_action TEXT NOT NULL,
        replay_log TEXT NOT NULL,
        resolution_status TEXT NOT NULL DEFAULT 'OPEN',
        resolution_note TEXT,
        resolved_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS narration_rules (
        narration TEXT PRIMARY KEY,
        order_id TEXT NOT NULL,
        confirmed_at TEXT NOT NULL,
        source TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS narration_templates (
        template TEXT PRIMARY KEY,
        confirmed_at TEXT NOT NULL,
        source TEXT NOT NULL
    )
    """,
]

_schema_ready = False


def get_connection() -> psycopg.Connection:
    """Opens a connection to Neon (DATABASE_URL) and ensures the schema
    exists. Postgres connections aren't free to open (TCP+TLS per
    invocation, same cost profile as the rest of this stateless-function
    deployment), so the schema check only runs once per warm container via
    _schema_ready, not on every call."""
    global _schema_ready
    url = os.environ["DATABASE_URL"]
    conn = psycopg.connect(url, row_factory=dict_row)
    if not _schema_ready:
        # NOT `with conn:` -- on a bare (non-pooled) psycopg3 Connection,
        # __exit__ always closes it (see Connection.__exit__: "Close the
        # connection only if it doesn't belong to a pool"), which would
        # hand the caller back a connection that's already dead. Commit
        # explicitly instead and leave the connection open for the caller.
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)
        conn.commit()
        _schema_ready = True
    return conn


def persist_results(results: list[dict], run_id: str) -> None:
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
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (match_key) DO UPDATE SET
                           run_id = EXCLUDED.run_id,
                           order_id = EXCLUDED.order_id,
                           settlement_id = EXCLUDED.settlement_id,
                           net_amount = EXCLUDED.net_amount,
                           status = EXCLUDED.status,
                           category = EXCLUDED.category,
                           reason = EXCLUDED.reason,
                           narration = EXCLUDED.narration,
                           needs_action = EXCLUDED.needs_action,
                           replay_log = EXCLUDED.replay_log
                       WHERE exceptions.resolution_status = 'OPEN'""",
                    (match_key, run_id, r.get("order_id"), r.get("settlement_id"), r.get("net"),
                     r["status"], r.get("category"), r.get("reason"), r.get("narration", ""),
                     needs_action, json.dumps(r.get("stage", []))),
                )
    finally:
        conn.close()


def get_open_exceptions() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM exceptions WHERE needs_action = 'yes' AND resolution_status = 'OPEN' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_all_exceptions() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM exceptions ORDER BY id").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def add_note(exception_id: int, note: str) -> None:
    conn = get_connection()
    try:
        with conn:
            conn.execute("UPDATE exceptions SET resolution_note = %s WHERE id = %s", (note, exception_id))
    finally:
        conn.close()


def _is_narration_specific(conn: psycopg.Connection, narration: str, order_id: str) -> bool:
    own_digits = re.findall(r"\d+", order_id)
    if not own_digits:
        return False
    own_suffix = own_digits[-1]
    if own_suffix not in re.findall(r"\d+", narration):
        return False

    other_order_ids = conn.execute(
        "SELECT DISTINCT order_id FROM exceptions WHERE order_id IS NOT NULL AND order_id != %s",
        (order_id,),
    ).fetchall()
    for row in other_order_ids:
        other_id = row["order_id"]
        other_digits = re.findall(r"\d+", other_id)
        if other_digits and other_digits[-1] == own_suffix:
            return False
    return True


def _derive_template(narration: str, order_id: str) -> str | None:
    own_digits = re.findall(r"\d+", order_id)
    if not own_digits:
        return None
    own_suffix = own_digits[-1]
    if own_suffix not in narration:
        return None
    template = narration.replace(own_suffix, "{REF}", 1)
    return template if template != narration else None


def get_narration_templates() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM narration_templates").fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def resolve_exception(exception_id: int, action: str, note: str | None = None) -> None:
    status_map = {"confirm": "CONFIRMED", "reject": "REJECTED"}
    resolution_status = status_map.get(action)
    if resolution_status is None:
        raise ValueError(f"unknown action: {action}")

    conn = get_connection()
    try:
        with conn:
            row = conn.execute("SELECT * FROM exceptions WHERE id = %s", (exception_id,)).fetchone()
            if row is None:
                raise KeyError(f"no exception with id {exception_id}")

            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            cursor = conn.execute(
                "UPDATE exceptions SET resolution_status = %s, resolution_note = %s, resolved_at = %s "
                "WHERE id = %s AND resolution_status = 'OPEN'",
                (resolution_status, note, now, exception_id),
            )
            if cursor.rowcount == 0:
                return

            if (action == "confirm" and row["category"] == "FUZZY_MATCH_NEEDS_REVIEW"
                    and row["narration"] and row["order_id"]
                    and _is_narration_specific(conn, row["narration"], row["order_id"])):
                conn.execute(
                    """INSERT INTO narration_rules (narration, order_id, confirmed_at, source)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (narration) DO UPDATE SET
                           order_id = EXCLUDED.order_id,
                           confirmed_at = EXCLUDED.confirmed_at,
                           source = EXCLUDED.source""",
                    (row["narration"], row["order_id"], now, "human_review"),
                )
                template = _derive_template(row["narration"], row["order_id"])
                if template is not None:
                    conn.execute(
                        """INSERT INTO narration_templates (template, confirmed_at, source)
                           VALUES (%s, %s, %s)
                           ON CONFLICT (template) DO UPDATE SET
                               confirmed_at = EXCLUDED.confirmed_at,
                               source = EXCLUDED.source""",
                        (template, now, "human_review"),
                    )
    finally:
        conn.close()


def compute_cash_clarity(all_rows: list[dict]) -> dict:
    """Identical copy of src/db.py's compute_cash_clarity() -- pure
    arithmetic over an already-fetched row list, no SQL involved, so
    duplicating it here (rather than importing across the src/ <-> api/
    boundary, which would tie the local zero-dependency app to this
    Postgres-only module) carries low drift risk. Keep both copies in
    sync if the resolved/pending/still-open logic ever changes."""
    at_risk = resolved = pending_review = still_open = 0.0
    for r in all_rows:
        amt = r["net_amount"]
        if amt is None or not r["category"] or r["category"] == "DUPLICATE":
            continue
        at_risk += amt
        if r["status"] == "MATCHED_LOW_CONFIDENCE":
            pending_review += amt
        elif r["status"] == "EXCEPTION":
            still_open += amt
        else:
            resolved += amt
    return {
        "at_risk": round(at_risk, 2),
        "resolved": round(resolved, 2),
        "pending_review": round(pending_review, 2),
        "still_open": round(still_open, 2),
        "resolved_pct": round(100 * resolved / at_risk, 1) if at_risk else 0.0,
        "pending_review_pct": round(100 * pending_review / at_risk, 1) if at_risk else 0.0,
        "still_open_pct": round(100 * still_open / at_risk, 1) if at_risk else 0.0,
    }


def get_narration_rule(narration: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM narration_rules WHERE narration = %s", (narration,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def get_all_narration_rules() -> dict[str, dict]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM narration_rules").fetchall()
    finally:
        conn.close()
    return {row["narration"]: dict(row) for row in rows}
