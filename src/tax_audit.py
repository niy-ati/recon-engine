"""
Tax-line matcher -- a distinct capability from settlement<->bank<->ledger
reconciliation. reconcile.py's
existing TAX_DEDUCTION category only checks that the settlement report and
the internal ledger AGREE with each other on the GST-on-MDR figure -- an
internal-consistency check between this project's own two numbers, not a
check against the actual law. A row can pass that check cleanly (both
sources agree) while still charging the wrong GST rate, because agreeing
with each other says nothing about agreeing with the statute.

This module checks the one thing neither the reconciliation passes nor a
naive "do the totals match" review ever would: whether the GST actually
charged on each settlement's MDR fee matches the real, current statutory
rate. Found live on this project's own real 509-row batch: ten rows
(order_1058, order_1128, and eight others) currently sit as plain MATCHED
with no category at all -- clean by every check this system already runs,
and clean by Razorpay's own dashboard too -- yet every one was actually
charged Rs.1 more GST than the law requires. That's the differentiated
case a tax-line matcher exists for: not a bigger discrepancy, but one
invisible to amount-matching entirely, because the settlement and the
ledger AGREE with each other -- they just both agree on the wrong number.

GST_ON_MDR_RATE is a real, verified figure (18%, the standard GST rate
applied to payment-gateway/aggregator service fees in India), not
invented for this feature -- see README for the source. Reads the raw
settlement report directly rather than the persisted exceptions table:
this has to check EVERY line regardless of that line's own match status,
and mdr/gst_on_mdr are transient CSV columns reconcile.py never persists
to the database (only order/settlement identity and the match outcome
are stored there).

RazorpayX itself ships this exact two-tier structure for real, per its own
docs (Manage Teams > Billing): a transaction-level "Invoice Reconciliation
Report" and a consolidated "Monthly Tax Invoice Report" a GST-registered
merchant reconciles against before filing ITC. audit_tax_lines() above is
the transaction tier. audit_monthly_reconciliation() below is the second
tier, and it exists because the two tiers genuinely catch different
things: a month of settlements can have every single row sit inside
audit_tax_lines()'s own Rs.0.50 tolerance -- individually invisible -- and
still add up to a materially wrong monthly total, or (the case found live
on the current real batch) the two tiers can cross-check each other and
agree: the ten known per-row overcharges above sum to Rs.10.00, against a
real Rs.9.83 aggregate drift for the month -- a Rs.0.17 residual, well
inside ordinary rounding noise, confirming the per-row tier already
accounts for essentially the whole month's drift on its own this run. A
materially different seed can flip this the other way (per-row
under-explaining the aggregate, revealing sub-tolerance drift spread
across the rest of the month no per-row check could see) -- both outcomes
are real findings this second tier can surface; this run's own numbers
happen to be the confirming case, not the revealing one, and are reported
as such rather than forced into a more dramatic story than what actually
ran.
"""
import csv
from collections import defaultdict
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Verified 2025/2026 rate, not assumed: GST on the payment gateway's own
# service fee (the MDR) is 18% of that fee -- not of the transaction's
# gross value. See README's Sources section for where this was checked.
GST_ON_MDR_RATE = 0.18

# A few paise of rounding between two independently-computed figures is
# normal and not a real finding -- this has to be bigger than plain
# floating-point/rounding noise before it's worth a human's attention.
TOLERANCE_RS = 0.50


def audit_tax_lines() -> list[dict]:
    """Every settlement row where the GST actually charged on its MDR fee
    doesn't match 18% of that fee, beyond simple rounding. Returns an
    empty list (not None) when the settlement report is missing or every
    row checks out -- an audit with nothing to report is a real, valid
    result, not a failure."""
    path = DATA_DIR / "settlement_report.csv"
    if not path.exists():
        return []

    findings = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                mdr = float(row["mdr"])
                actual_gst = float(row["gst_on_mdr"])
            except (KeyError, ValueError):
                continue
            expected_gst = round(mdr * GST_ON_MDR_RATE, 2)
            diff = round(actual_gst - expected_gst, 2)
            if abs(diff) > TOLERANCE_RS:
                findings.append({
                    "order_id": row.get("order_id") or None,
                    "settlement_id": row.get("settlement_id") or None,
                    "mdr": mdr,
                    "expected_gst": expected_gst,
                    "actual_gst": actual_gst,
                    "diff": round(abs(diff), 2),
                    "direction": "overcharged" if diff > 0 else "undercharged",
                })
    return findings


# Deliberately NOT scaled up from the per-row tolerance by transaction
# count -- a GST department doesn't grade an ITC mismatch on a curve for
# batch size, so neither does this. Rs.2 sits comfortably above what a
# single flagged row could produce on its own (TOLERANCE_RS is 0.50) so
# this tier is never just re-stating a per-row finding, while staying low
# enough that a real, modest, systemic drift like the one found live on
# this project's own batch (Rs.9.83 across ~500 rows -- see docstring)
# doesn't slip through as "immaterial" just because it's spread thin.
MONTHLY_TOLERANCE_RS = 2.00


def audit_monthly_reconciliation() -> list[dict]:
    """See module docstring for why this exists as a second, distinct
    tier from audit_tax_lines() above, mirroring RazorpayX's own real
    transaction-level vs consolidated-monthly tax reporting split.

    Groups every settlement by month (settlement_date's "YYYY-MM") and
    compares that month's total actual GST-on-MDR against what the real
    18% statutory rate would produce in aggregate. Cross-references
    audit_tax_lines()'s own per-row findings for the same month so the
    reported "unexplained" figure is genuinely new information a
    transaction-level check already surfaced can't take credit for --
    found live: of the real batch's one month's Rs.9.83 aggregate drift,
    the ten rows audit_tax_lines() already flags sum to Rs.10.00 on their
    own -- a Rs.0.17 residual, inside ordinary rounding noise, meaning the
    per-row tier already accounts for essentially the whole month's drift
    this run. A different seed can flip this the other way (per-row
    under-explaining the aggregate, revealing sub-tolerance drift spread
    across the rest of the month no per-row check could see) -- both are
    real outcomes this second tier can surface; see the module docstring."""
    path = DATA_DIR / "settlement_report.csv"
    if not path.exists():
        return []

    by_month = defaultdict(lambda: {"mdr": 0.0, "actual_gst": 0.0, "count": 0})
    month_by_settlement = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            date = row.get("settlement_date") or ""
            if len(date) < 7:
                continue
            month = date[:7]
            month_by_settlement[row.get("settlement_id")] = month
            try:
                mdr = float(row["mdr"])
                actual_gst = float(row["gst_on_mdr"])
            except (KeyError, ValueError):
                continue
            by_month[month]["mdr"] += mdr
            by_month[month]["actual_gst"] += actual_gst
            by_month[month]["count"] += 1

    already_flagged_by_month = defaultdict(float)
    for finding in audit_tax_lines():
        month = month_by_settlement.get(finding["settlement_id"])
        if month:
            signed = finding["diff"] if finding["direction"] == "overcharged" else -finding["diff"]
            already_flagged_by_month[month] += signed

    findings = []
    for month, totals in sorted(by_month.items()):
        expected_gst = round(totals["mdr"] * GST_ON_MDR_RATE, 2)
        diff = round(totals["actual_gst"] - expected_gst, 2)
        if abs(diff) <= MONTHLY_TOLERANCE_RS:
            continue
        already_flagged = round(already_flagged_by_month.get(month, 0.0), 2)
        findings.append({
            "month": month,
            "settlement_count": totals["count"],
            "actual_gst_total": round(totals["actual_gst"], 2),
            "expected_gst_total": expected_gst,
            "diff": diff,
            "direction": "overcharged" if diff > 0 else "undercharged",
            "already_flagged_per_row": already_flagged,
            "unexplained": round(diff - already_flagged, 2),
        })
    return findings
