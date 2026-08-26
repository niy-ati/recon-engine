"""
One-off local script: copies the current local data/reconcile.db (SQLite)
into the Neon Postgres database the Vercel deployment reads from, so the
hosted demo shows the same real 514-row batch and resolution history as
the local review site, not an empty queue.

Run once, locally, after DATABASE_URL is set:

    DATABASE_URL=postgres://... python api/migrate_sqlite_to_pg.py

Safe to re-run: truncates the three tables in Postgres before copying, so
re-running after a fresh local `python run_all.py` re-syncs the snapshot
rather than duplicating rows.
"""
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from db_pg import SCHEMA_STATEMENTS  # noqa: E402

SQLITE_PATH = REPO_ROOT / "data" / "reconcile.db"


def _resolve_via_public_dns(hostname: str) -> str | None:
    """This machine's local ISP resolver refuses to answer for Neon's long
    pooled-connection hostname (confirmed: works fine via 8.8.8.8, fails
    via the system resolver) -- a local network quirk, not a Neon or
    Vercel problem, and irrelevant to the deployed app itself (Vercel's
    own servers resolve it fine). Falls back to normal resolution if the
    lookup fails for any reason."""
    try:
        out = subprocess.run(
            ["nslookup", "-type=A", hostname, "8.8.8.8"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return None

    lines = out.splitlines()
    # The first "Server:" / "Address:" pair identifies the DNS server we
    # queried (8.8.8.8 itself), not the answer -- only look at lines after
    # the blank line that follows it.
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == "") + 1
    except StopIteration:
        start = 0

    for line in lines[start:]:
        line = line.strip()
        if line.startswith(("Address:", "Addresses:")):
            candidate = line.split(":", 1)[1].strip()
            try:
                socket.inet_aton(candidate)
                return candidate
            except OSError:
                continue
    return None


def main() -> None:
    if "DATABASE_URL" not in os.environ:
        raise SystemExit("Set DATABASE_URL to the Neon connection string first.")
    if not SQLITE_PATH.exists():
        raise SystemExit(f"No local database at {SQLITE_PATH} -- run `python run_all.py` first.")

    sconn = sqlite3.connect(SQLITE_PATH)
    sconn.row_factory = sqlite3.Row

    conninfo = psycopg.conninfo.conninfo_to_dict(os.environ["DATABASE_URL"])
    hostaddr = _resolve_via_public_dns(conninfo["host"])
    if hostaddr:
        conninfo["hostaddr"] = hostaddr
        print(f"Local DNS wouldn't answer for {conninfo['host']}; using {hostaddr} via 8.8.8.8 instead.")
    pconn = psycopg.connect(**conninfo, row_factory=dict_row)
    with pconn:
        for stmt in SCHEMA_STATEMENTS:
            pconn.execute(stmt)
        pconn.execute("TRUNCATE exceptions, narration_rules, narration_templates")

        exceptions = [dict(r) for r in sconn.execute("SELECT * FROM exceptions").fetchall()]
        for r in exceptions:
            pconn.execute(
                """INSERT INTO exceptions
                   (match_key, run_id, order_id, settlement_id, net_amount, status, category,
                    reason, narration, needs_action, replay_log, resolution_status,
                    resolution_note, resolved_at)
                   VALUES (%(match_key)s, %(run_id)s, %(order_id)s, %(settlement_id)s, %(net_amount)s,
                           %(status)s, %(category)s, %(reason)s, %(narration)s, %(needs_action)s,
                           %(replay_log)s, %(resolution_status)s, %(resolution_note)s, %(resolved_at)s)""",
                r,
            )

        rules = [dict(r) for r in sconn.execute("SELECT * FROM narration_rules").fetchall()]
        for r in rules:
            pconn.execute(
                """INSERT INTO narration_rules (narration, order_id, confirmed_at, source)
                   VALUES (%(narration)s, %(order_id)s, %(confirmed_at)s, %(source)s)""",
                r,
            )

        try:
            templates = [dict(r) for r in sconn.execute("SELECT * FROM narration_templates").fetchall()]
        except sqlite3.OperationalError:
            templates = []
        for r in templates:
            pconn.execute(
                """INSERT INTO narration_templates (template, confirmed_at, source)
                   VALUES (%(template)s, %(confirmed_at)s, %(source)s)""",
                r,
            )

    sconn.close()
    pconn.close()
    print(f"Copied {len(exceptions)} exceptions, {len(rules)} narration_rules, "
          f"{len(templates)} narration_templates into Postgres.")


if __name__ == "__main__":
    main()
