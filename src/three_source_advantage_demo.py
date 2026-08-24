"""
Quantifies, on the real batch, exactly which rows this engine could only
resolve or explain because it reads the merchant's own ledger, the third
source. Razorpay's own published Intelligent Reconciliation Agent checks
two sources only, a bank statement screenshot against Razorpay's
settlement records, matched on UTR and amount, then flags whatever's
left as a bare discrepancy (see README.md's Scope section for the
citation, and the "Where this could actually live" note there for why
this matters beyond the demo).

Every (status, category) combination below was checked by hand against
this file's own reconcile.py, not inferred from a generic heuristic --
an earlier version of this script inferred "needed the ledger" from
which pass last touched a row's stage log, and that was wrong: pass 2
appends a confirmation log entry to almost every row regardless of
whether it changed anything, since it always looks up the ledger's
order_id entry for bookkeeping even when pass 1 already matched
cleanly. Read reconcile.py's own source before trusting a number
derived from it, the same discipline the rest of this codebase holds
itself to.
"""
from collections import defaultdict
from reconcile import reconcile, summarize

# Verified directly against reconcile.py: each key here is a (status,
# category) pair that ONLY ever arises from reading ledger narration
# text or a ledger-specific field, never from bank + settlement data
# alone. See the module docstring in reconcile.py's own passes for the
# line-by-line origin of each one.
LEDGER_DEPENDENT = {
    ("MATCHED_WITH_VARIANCE", "PARTIAL_PAYMENT"),   # ledger REFUND narration
    ("MATCHED_EXACT_REFERENCE", None),               # ledger narration digit match
    ("MATCHED_LOW_CONFIDENCE", "FUZZY_MATCH_NEEDS_REVIEW"),  # ledger narration + arbiter
    ("MATCHED_LEARNED_PATTERN", None),                # ledger narration, known pattern
    ("MATCHED_AI_ASSISTED", None),                     # ledger narration, arbiter auto applied
    ("EXCEPTION", "AFA_MANDATE_HOLD"),                # ledger row, no settlement/bank counterpart at all
    ("EXCEPTION", "UNEXPLAINED"),                      # only when the reason names the ledger, see below
}

# UNEXPLAINED has two distinct origins that share one category name: a
# bank-side no-match (bank + settlement only, not ledger-dependent) and
# this one, a settlement with no ledger/invoice entry at all -- checked
# by the exact reason text reconcile.py writes for this specific branch.
LEDGER_UNEXPLAINED_MARKER = "no corresponding ledger/invoice entry"


def is_ledger_dependent(r: dict) -> bool:
    key = (r["status"], r["category"])
    if key == ("EXCEPTION", "UNEXPLAINED"):
        return LEDGER_UNEXPLAINED_MARKER in (r["reason"] or "")
    return key in LEDGER_DEPENDENT


def print_report(results: list[dict]) -> None:
    total = len(results)
    ledger_rows = [r for r in results if is_ledger_dependent(r)]
    pct = round(100 * len(ledger_rows) / total, 1) if total else 0.0

    print("=== TWO SOURCE (BANK + SETTLEMENT, UTR/AMOUNT) VS THREE SOURCE ===")
    print(f"{len(ledger_rows)} of {total} rows ({pct}%) were resolved or explained "
          f"using the ledger, not just the bank statement and settlement report.\n")

    by_key = defaultdict(list)
    for r in ledger_rows:
        by_key[(r["status"], r["category"] or "(none)")].append(r)

    for (status, category), rows in sorted(by_key.items(), key=lambda kv: -len(kv[1])):
        example = rows[0]
        reason = example["reason"] or (example["stage"][-1]["detail"] if example["stage"] else "")
        print(f"{status} / {category}: {len(rows)} row(s)")
        print(f"  this engine, {example['order_id']}: {reason}\n")

    # The single most concrete example: a row a two-source tool would
    # have called done and never looked at again, that this engine
    # flags because the merchant's own books have no record of it.
    downgraded = [
        r for r in results
        if r["category"] == "UNEXPLAINED"
        and LEDGER_UNEXPLAINED_MARKER in (r["reason"] or "")
        and any(s["pass"] == "1" and s["action"] == "matched" for s in (r["stage"] or []))
        and len(r["stage"]) == 1
    ]
    print(f"Of those, {len(downgraded)} row(s) had a perfectly clean bank match, UTR, "
          f"amount, and date all agreeing, exactly what a two source tool would call "
          f"resolved and stop looking at. This engine still flags them, because the "
          f"merchant's own ledger has no record of the order at all:")
    for r in downgraded:
        print(f"  {r['order_id']}: {r['reason']}")


if __name__ == "__main__":
    results = reconcile()
    print_report(results)
    print("\n=== FULL SUMMARY ===")
    for k, v in summarize(results).items():
        print(f"{k}: {v}")
