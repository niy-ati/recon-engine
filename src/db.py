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
import re
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
    gross_amount REAL,
    mdr_amount REAL,
    utr TEXT,
    settlement_date TEXT,
    method TEXT,
    dispute_id TEXT,
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

-- Order-independent generalization of narration_rules: the same confirmed
-- narration with its order's own digit reference replaced by a {REF}
-- placeholder. A recurring narration-generating system (the same customer,
-- the same payment gateway template) produces the same surrounding text
-- every time and only the order reference changes -- this lets a future,
-- differently-numbered narration from that same template resolve without
-- a fresh human confirm, which a plain exact-string narration_rules entry
-- never could. See README, 'the learned pattern store memorizes exact
-- strings, not a generalized template.'
CREATE TABLE IF NOT EXISTS narration_templates (
    template TEXT PRIMARY KEY,
    confirmed_at TEXT NOT NULL,
    source TEXT NOT NULL
);

-- One row per pipeline run, appended, never overwritten or reset by
-- reset_batch() -- this tracks this TOOL's own real usage history across
-- runs, a genuinely different thing from `exceptions`, which is the
-- current batch's own per-row state. The Overview page's trend chart
-- reads this directly: real data points from real runs, not a
-- simulated or backfilled history.
CREATE TABLE IF NOT EXISTS run_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    total_rows INTEGER NOT NULL,
    resolved_pct REAL NOT NULL,
    pending_review_pct REAL NOT NULL,
    open_pct REAL NOT NULL
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
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

    # gross_amount/mdr_amount/utr/settlement_date: added so settlement_qa.py
    # can answer "settlement on the 5th" / "when's my next settlement" with
    # real UTR and payment-breakdown data, not just the net figure matching
    # already used -- see reconcile.py's module docstring for why these are
    # newly carried through. A DB created before this column existed just
    # gets it backfilled as NULL on next persist_results(), same as every
    # other pre-existing row this migration doesn't try to fabricate data for.
    for column in ("gross_amount", "mdr_amount", "utr", "settlement_date", "method", "dispute_id"):
        if column not in columns:
            column_type = "REAL" if column.endswith("_amount") else "TEXT"
            conn.execute(f"ALTER TABLE exceptions ADD COLUMN {column} {column_type}")


def get_connection() -> sqlite3.Connection:
    """Opens (creating if needed) data/reconcile.db, applies SCHEMA, and
    runs the match_key migration. Callers own closing the connection."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def persist_results(results: list[dict], run_id: str) -> None:
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
                       (match_key, run_id, order_id, settlement_id, net_amount, gross_amount,
                        mdr_amount, utr, settlement_date, method, dispute_id, status, category,
                        reason, narration, needs_action, replay_log)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(match_key) DO UPDATE SET
                           run_id = excluded.run_id,
                           order_id = excluded.order_id,
                           settlement_id = excluded.settlement_id,
                           net_amount = excluded.net_amount,
                           gross_amount = excluded.gross_amount,
                           mdr_amount = excluded.mdr_amount,
                           utr = excluded.utr,
                           settlement_date = excluded.settlement_date,
                           method = excluded.method,
                           dispute_id = excluded.dispute_id,
                           status = excluded.status,
                           category = excluded.category,
                           reason = excluded.reason,
                           narration = excluded.narration,
                           needs_action = excluded.needs_action,
                           replay_log = excluded.replay_log
                       WHERE exceptions.resolution_status = 'OPEN'""",
                    (match_key, run_id, r.get("order_id"), r.get("settlement_id"), r.get("net"),
                     r.get("gross"), r.get("mdr"), r.get("utr"), r.get("settlement_date"),
                     r.get("method"), r.get("dispute_id"),
                     r["status"], r.get("category"), r.get("reason"), r.get("narration", ""),
                     needs_action, json.dumps(r.get("stage", []))),
                )
    finally:
        conn.close()


def reset_batch() -> None:
    """Deletes every row from `exceptions` -- a deliberate, explicit "start
    over," never called from persist_results() itself. That function's own
    upsert-on-match_key is correct for re-running the SAME batch (a code
    change to the matching logic, say) without losing a human's earlier
    confirm/reject. But generate_data.py draws every ID from one seeded
    random stream, so touching that file (adding a field, a new case
    bucket, anything that changes how many random values are drawn before
    another) shifts every ID after that point -- the batch is still fully
    reproducible under its seed, just a DIFFERENT batch, with settlement_ids
    that share nothing with whatever's already persisted. Found live: five
    small, unrelated edits to generate_data.py during one session each
    left the previous run's rows behind under now-orphaned match_keys,
    silently growing data/reconcile.db from one real batch's ~525 rows to
    3,104 rows across 6 forgotten runs -- report.py calling persist_results()
    dutifully, correctly, upserting rows that could never collide with the
    old ones because their IDs were never going to match again.

    narration_rules/narration_templates are deliberately NOT touched here:
    those are cross-batch learned memory by design (a human confirming a
    narration pattern once should keep working on a future batch with the
    same recurring template), not per-batch state the way `exceptions` is."""
    conn = get_connection()
    try:
        with conn:
            conn.execute("DELETE FROM exceptions")
    finally:
        conn.close()


def record_run_summary(run_id: str, total_rows: int, resolved_pct: float,
                        pending_review_pct: float, open_pct: float) -> None:
    """Appends one row to run_history -- called by report.py on every run,
    regardless of --fresh-batch, since this tracks real usage of the tool
    itself over time, not the current batch's own state. Never updated or
    deleted (unlike `exceptions`, this has no match_key to upsert on and no
    reason to overwrite a past run's real numbers with a later one's)."""
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                "INSERT INTO run_history (run_id, recorded_at, total_rows, resolved_pct, "
                "pending_review_pct, open_pct) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 total_rows, resolved_pct, pending_review_pct, open_pct),
            )
    finally:
        conn.close()


def get_run_history(limit: int = 20) -> list[dict]:
    """The most recent `limit` runs, oldest first (chart-ready order) --
    real data points from real past runs of this tool, never backfilled or
    simulated to fill out a nicer-looking trend."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM (SELECT * FROM run_history ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_open_exceptions() -> list[dict]:
    """Rows needing a human decision: needs_action='yes' and not yet
    confirmed/rejected. What review_server.py's /queue page lists."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM exceptions WHERE needs_action = 'yes' AND resolution_status = 'OPEN' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_all_exceptions() -> list[dict]:
    """Every persisted row from the last run, matched and exception alike."""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM exceptions ORDER BY id").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def add_note(exception_id: int, note: str) -> None:
    """Attaches a note without resolving the row -- lets a reviewer record
    context without being forced into a binary confirm/reject decision.
    Row stays OPEN, stays in the queue."""
    conn = get_connection()
    try:
        with conn:
            conn.execute("UPDATE exceptions SET resolution_note = ? WHERE id = ?", (note, exception_id))
    finally:
        conn.close()


def _is_narration_specific(conn: sqlite3.Connection, narration: str, order_id: str) -> bool:
    """A confirm on a boilerplate narration ("payment received, thanks")
    that could plausibly describe many orders shouldn't get memoized --
    it would auto-apply to the next unrelated transaction carrying the
    same generic text. Requires the narration to contain a digit run that
    is this order's own numeric suffix AND isn't shared with any other
    order_id already known to the exceptions table. See README, 'the
    learned-pattern store can be poisoned by an overly generic confirm.'"""
    own_digits = re.findall(r"\d+", order_id)
    if not own_digits:
        return False
    own_suffix = own_digits[-1]
    if own_suffix not in re.findall(r"\d+", narration):
        return False

    other_order_ids = conn.execute(
        "SELECT DISTINCT order_id FROM exceptions WHERE order_id IS NOT NULL AND order_id != ?",
        (order_id,),
    ).fetchall()
    for (other_id,) in other_order_ids:
        other_digits = re.findall(r"\d+", other_id)
        if other_digits and other_digits[-1] == own_suffix:
            return False
    return True


def _derive_template(narration: str, order_id: str) -> str | None:
    """Replaces the FIRST occurrence of order_id's own digit suffix in
    narration with a {REF} placeholder, so a future narration from the
    same recurring template (same surrounding text, a different order's
    digits) can be recognized without a fresh human confirm. Only ever
    called after _is_narration_specific already confirmed the suffix is
    present and uniquely identifies this order, so this never fabricates
    a placeholder where none is warranted. Returns None if there is
    nothing to generalize (defensive; should not happen given the caller's
    guard)."""
    own_digits = re.findall(r"\d+", order_id)
    if not own_digits:
        return None
    own_suffix = own_digits[-1]
    if own_suffix not in narration:
        return None
    template = narration.replace(own_suffix, "{REF}", 1)
    return template if template != narration else None


def get_narration_templates() -> list[dict]:
    """Every learned narration template, in one query -- mirrors
    get_all_narration_rules()'s bulk-fetch-once pattern so reconcile.py's
    per-batch pass never opens a fresh connection per ledger row."""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM narration_templates").fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def resolve_exception(exception_id: int, action: str, note: str | None = None) -> None:
    """action: 'confirm' | 'reject' -- terminal decisions only.

    Confirming a FUZZY_MATCH_NEEDS_REVIEW row also writes a narration_rules
    entry, so the same narration resolves automatically next time, and a
    narration_templates entry generalizing that narration's own digit
    reference to a {REF} placeholder, so a differently-numbered future
    narration from the same recurring template also benefits.

    review_server.py serves this over a ThreadingHTTPServer, so two
    requests for the same exception_id (a double click, two open tabs)
    can genuinely race. The UPDATE below is gated on the row still being
    OPEN and its rowcount checked, so only the first request to actually
    commit can flip the status or write a narration rule -- a second,
    losing request is a silent no-op, not an overwrite of a terminal
    decision and not a duplicate narration_rules write."""
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
            cursor = conn.execute(
                "UPDATE exceptions SET resolution_status = ?, resolution_note = ?, resolved_at = ? "
                "WHERE id = ? AND resolution_status = 'OPEN'",
                (resolution_status, note, now, exception_id),
            )
            if cursor.rowcount == 0:
                # Already resolved -- by this same call racing another thread,
                # or simply already CONFIRMED/REJECTED earlier. Either way a
                # terminal decision was already made; leave it alone.
                return

            if (action == "confirm" and row["category"] == "FUZZY_MATCH_NEEDS_REVIEW"
                    and row["narration"] and row["order_id"]
                    and _is_narration_specific(conn, row["narration"], row["order_id"])):
                conn.execute(
                    "INSERT OR REPLACE INTO narration_rules (narration, order_id, confirmed_at, source) VALUES (?, ?, ?, ?)",
                    (row["narration"], row["order_id"], now, "human_review"),
                )
                template = _derive_template(row["narration"], row["order_id"])
                if template is not None:
                    conn.execute(
                        "INSERT OR REPLACE INTO narration_templates (template, confirmed_at, source) VALUES (?, ?, ?)",
                        (template, now, "human_review"),
                    )
    finally:
        conn.close()


def get_narration_rule(narration: str) -> dict | None:
    """The human-confirmed order_id for this exact narration string, if
    one was ever recorded via resolve_exception()'s confirm path."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM narration_rules WHERE narration = ?", (narration,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def summarize_replay(replay_log: list) -> str:
    """A clean match has no `reason` set -- reconcile.py only writes one for
    variance/exception cases -- but the replay_log already has the real
    stage-by-stage explanation computed for every row. This builds one
    readable line from those same structured entries instead of leaving a
    clean match with nothing to show but a dash.

    Moved here from review_server.py (same reason compute_cash_clarity()
    was: see TestComputeCashClarity's docstring in test_db.py) so
    settlement_qa.py's chat answers can show the exact same real
    explanation the Records page's audit box does for a clean match,
    without a circular import -- both modules already import db
    unconditionally, neither imports the other."""
    details = [entry.get("detail", "") for entry in replay_log if isinstance(entry, dict) and entry.get("detail")]
    if not details:
        return ""
    return "; ".join(d[0].upper() + d[1:] for d in details if d)


def compute_cash_clarity(all_rows: list[dict]) -> dict:
    """Real Rs. amounts computed from this run's own persisted net_amount/
    category/status columns -- not a forecast, not a parallel calculation.
    Quantifies the 'this build sits upstream of Cashflow Forecaster' claim
    with a number instead of just an argument: every row that hit some
    exception/variance path is cash position a downstream forecaster would
    otherwise see as ambiguous; the portion this engine explained or
    matched is now trustworthy input, the portion pending a human's
    confirm is disclosed as such (not counted as done), and the portion
    still open is disclosed honestly too. Mirrors reconcile.py's
    summarize() -- same fix applied in both places, kept in sync.

    Two things this used to get wrong, found by tracing this number
    against this module's own needs_action rule instead of assuming it
    already matched: MATCHED_LOW_CONFIDENCE (an unconfirmed arbiter
    candidate) was folded into "resolved", and DUPLICATE rows (a second
    REPORT of a transaction whose real money already cleared under its
    sibling row) were counted as separate at-risk cash, double-counting
    money that already landed.

    Lives here, not in review_server.py where it originated, so
    settlement_qa.py's cash-value chat questions can call the exact same
    function the Overview page's cash-position panel uses -- without a
    circular import, since both modules already import db unconditionally.
    api/db_pg.py (the Postgres-backed module the Vercel deployment swaps
    in for this one) carries an identical copy for the same reason; it's
    pure arithmetic over an already-fetched row list with no SQL in it, so
    the duplication risk is low -- nothing like the three genuinely
    different reimplementations of "what counts as resolved" that caused
    the real metrics bug this docstring's sibling copies reference."""
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


def get_all_narration_rules() -> dict[str, dict]:
    """Every learned narration -> order_id rule, keyed by narration, in one
    query. reconcile.py's Pass 2.5 checks one of these per unmatched ledger
    row in a batch -- calling get_narration_rule() there would open a fresh
    SQLite connection (and re-run the schema/migration script) once per
    row, which measured as the single largest cost in the whole pipeline
    at scale (profiled: ~2.8s of ~9.7s on a 3,000-row batch, more than the
    matching logic itself). One connection, one query, a dict lookup after
    that -- same data, same semantics, not a scan avoided by luck."""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM narration_rules").fetchall()
    finally:
        conn.close()
    return {row["narration"]: dict(row) for row in rows}
