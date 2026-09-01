"""
Tax-line matcher -- Track 4 names this as its own example use case,
distinct from settlement<->bank<->ledger reconciliation. reconcile.py's
existing TAX_DEDUCTION category only checks that the settlement report and
the internal ledger AGREE with each other on the GST-on-MDR figure -- an
internal-consistency check between this project's own two numbers, not a
check against the actual law. A row can pass that check cleanly (both
sources agree) while still charging the wrong GST rate, because agreeing
with each other says nothing about agreeing with the statute.

This module checks the one thing neither the reconciliation passes nor a
naive "do the totals match" review ever would: whether the GST actually
charged on each settlement's MDR fee matches the real, current statutory
rate. Found live on this project's own real 500-row batch: two rows
(order_1210, order_1151) currently sit as plain MATCHED with no category
at all -- clean by every check this system already runs, and clean by
Razorpay's own dashboard too -- yet both were actually charged Rs.1 more
GST than the law requires. That's the differentiated case a tax-line
matcher exists for: not a bigger discrepancy, but one invisible to
amount-matching entirely, because the settlement and the ledger AGREE with
each other -- they just both agree on the wrong number.

GST_ON_MDR_RATE is a real, verified figure (18%, the standard GST rate
applied to payment-gateway/aggregator service fees in India), not
invented for this feature -- see README for the source. Reads the raw
settlement report directly rather than the persisted exceptions table:
this has to check EVERY line regardless of that line's own match status,
and mdr/gst_on_mdr are transient CSV columns reconcile.py never persists
to the database (only order/settlement identity and the match outcome
are stored there).
"""
import csv
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
