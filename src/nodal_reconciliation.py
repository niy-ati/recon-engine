"""
Escrow/nodal account balance check -- a reconciliation Razorpay itself has
to run as a Payment Aggregator, distinct from anything a merchant-facing
tool checks. Every rupee a customer pays flows through Razorpay's own
escrow account before it reaches a merchant's bank account (T+2 in this
project's own synthetic pipeline); at any moment, RBI's Master Direction
on Regulation of Payment Aggregators (RBI/DPSS/2025-26/141, 15 Sep 2025,
Chapter V) requires the escrow balance to be an actual floor, not an exact
target: "shall not be less than the amount realised in escrow towards
funds payable to the merchants, but not settled to them." A balance BELOW
that floor is a real compliance breach (money that should be sitting
ring-fenced for merchants isn't there); a balance ABOVE it isn't itself a
violation of this clause, so this module reports it as a softer,
informational note, not a manufactured violation dressed up to look like
one -- an earlier draft of this feature conflated the two based on a
secondary practitioner blog's looser framing ("commingling"), corrected
here against the primary Direction's own text.

The Direction itself only mandates periodic (quarterly auditor and
banker's certificates, monthly transaction statistics to RBI) reporting
of escrow compliance, not a stated daily cadence -- the daily version
checked here is the real INTERNAL operational practice this is modeled
on: Razorpay's own Senior Analyst, Financial Operations listing (see
README) names "day-to-day reconciliation of deposits and withdrawals into
the Nodal account" and "bank MIS reconciliation with system data...on a
daily basis" as an active job function, not a hypothetical.

Reads settlement_report.csv directly (same reasoning as tax_audit.py's own
module docstring: this needs the transient gross/mdr/net columns
reconcile.py never persists to the database). DUPLICATE settlement pairs
are deduplicated by base settlement_id before summing -- exactly one real
bank credit exists per pair, so counting both would double the obligation
figure for money that only actually moved once.
"""
import csv
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Matches generate_data.py's own settlement_date = txn_date + timedelta(days=2)
# -- the collection date is reconstructed backward from the settlement date
# already on the CSV rather than requiring a second column, since the two
# are always exactly T+2 apart by construction in this synthetic batch.
T_PLUS_DAYS = 2

# A few rupees of rounding between two independently-rounded daily sums is
# noise, not a real finding -- same discipline as tax_audit.py's own
# TOLERANCE_RS, sized for a per-day aggregate instead of a single row.
SHORTFALL_TOLERANCE_RS = 1.00


def _base_settlement_id(settlement_id: str) -> str:
    return settlement_id[:-4] if settlement_id.endswith("_dup") else settlement_id


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def compute_daily_obligation(data_dir: str | Path = DATA_DIR) -> dict[str, float]:
    """The real escrow floor for every date the batch touches: how much
    money has been collected from customers but not yet settled out to
    the merchant as of that date. An on_hold row never settles in this
    batch, so its obligation never clears -- exactly like the real
    on_hold=true settlement it represents, still sitting in escrow with
    no payout yet."""
    path = Path(data_dir) / "settlement_report.csv"
    if not path.exists():
        return {}

    seen_base_ids = set()
    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            base_id = _base_settlement_id(row["settlement_id"])
            if base_id in seen_base_ids:
                continue
            seen_base_ids.add(base_id)
            rows.append(row)

    if not rows:
        return {}

    collected_dates = []
    settled_dates = []
    parsed_rows = []
    for row in rows:
        settled = _parse_date(row["settlement_date"])
        collected = settled - timedelta(days=T_PLUS_DAYS)
        on_hold = (row.get("on_hold") or "False").strip().lower() == "true"
        parsed_rows.append((collected, settled, on_hold, float(row["net"])))
        collected_dates.append(collected)
        settled_dates.append(settled)

    horizon = max(settled_dates) + timedelta(days=1)
    obligation: dict[str, float] = {}
    d = min(collected_dates)
    while d <= horizon:
        total = 0.0
        for collected, settled, on_hold, net in parsed_rows:
            if d < collected:
                continue
            if on_hold or d < settled:
                total += net
        obligation[d.isoformat()] = round(total, 2)
        d += timedelta(days=1)
    return obligation


def audit_nodal_balance(data_dir: str | Path = DATA_DIR) -> list[dict]:
    """Compares the recorded daily escrow balance (nodal_balance.csv, a
    stand-in for the bank's own statement of that account) against the
    obligation this batch's own settlement data implies. Returns an empty
    list when the file is missing or every day clears the floor -- see
    module docstring for why a surplus day is reported as an
    informational note, never as a violation."""
    data_dir = Path(data_dir)
    balance_path = data_dir / "nodal_balance.csv"
    if not balance_path.exists():
        return []

    obligation = compute_daily_obligation(data_dir)
    findings = []
    with open(balance_path, newline="") as f:
        for row in csv.DictReader(f):
            day = row["date"]
            balance = float(row["closing_balance"])
            owed = obligation.get(day)
            if owed is None:
                continue
            diff = round(balance - owed, 2)
            if diff < -SHORTFALL_TOLERANCE_RS:
                findings.append({
                    "date": day, "balance": balance, "obligation": owed,
                    "diff": round(abs(diff), 2), "kind": "SHORTFALL",
                })
            elif diff > SHORTFALL_TOLERANCE_RS:
                findings.append({
                    "date": day, "balance": balance, "obligation": owed,
                    "diff": round(diff, 2), "kind": "SURPLUS_NOTE",
                })
    return findings


def write_synthetic_balance_csv(data_dir: str | Path = DATA_DIR) -> None:
    """Writes data/nodal_balance.csv, a stand-in for the escrow account's
    own bank statement -- matches the true obligation on every date
    except two, deliberately perturbed the same way failure_injection_demo.py
    injects its own adversarial case: a real gap to find, not a clean batch
    with nothing to check. No randomness involved -- both perturbed dates
    and amounts are fixed, so this is exactly as reproducible as the rest
    of the synthetic pipeline."""
    data_dir = Path(data_dir)
    obligation = compute_daily_obligation(data_dir)
    if not obligation:
        return

    days = sorted(obligation)
    shortfall_day = days[-2] if len(days) > 1 else days[-1]
    surplus_day = days[len(days) // 2]

    with open(data_dir / "nodal_balance.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "closing_balance"])
        for day in days:
            balance = obligation[day]
            if day == shortfall_day:
                balance = round(balance - 15000.0, 2)
            elif day == surplus_day:
                balance = round(balance + 25000.0, 2)
            w.writerow([day, balance])


if __name__ == "__main__":
    write_synthetic_balance_csv()
    findings = audit_nodal_balance()
    print(f"Wrote data/nodal_balance.csv, {len(findings)} day(s) flagged:")
    for f in findings:
        print(f"  {f['date']}: {f['kind']} -- balance Rs.{f['balance']:,.2f} vs "
              f"obligation Rs.{f['obligation']:,.2f} (Rs.{f['diff']:,.2f})")
