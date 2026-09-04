"""
Produces output/reconciliation_report.md and output/exceptions.csv, and
persists every result row into SQLite (db.py) so review_server.py reads
from a live, inspectable database instead of a CSV re-upload.
"""
import argparse
import csv
import time
from pathlib import Path

import db
from reconcile import reconcile, summarize, new_correlation_id

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

def generate(settlement_source: str = "synthetic", fresh_batch: bool = False) -> None:
    if fresh_batch:
        # See db.reset_batch()'s own docstring for why this exists: a
        # brand-new synthetic (or live) dataset means every ID persist_
        # results() is about to upsert is unrelated to whatever's already
        # in the database, so the old rows would otherwise just accumulate
        # forever instead of being replaced.
        db.reset_batch()
    if settlement_source == "live":
        print("Using LIVE Razorpay settlement data (test-mode keys). Test-mode "
              "transactions never generate settlements, so an authenticated "
              "call returning zero items is expected, not a failure. Bank "
              "statement and internal ledger remain synthetic (no live "
              "bank/Tally API is available here).")

    # One id generated here, before matching starts, threaded into every
    # structured log entry reconcile() builds AND into the row's own
    # run_id column in SQLite -- one identifier, not two competing ones.
    run_id = new_correlation_id()

    start = time.perf_counter()
    results = reconcile(settlement_source=settlement_source, correlation_id=run_id)
    elapsed_seconds = time.perf_counter() - start

    if settlement_source == "live" and not any(r.get("settlement_id") for r in results):
        print("Confirmed: zero live settlements returned, so every bank and "
              "ledger row shows as an exception below -- the expected shape "
              "of a live test-mode run. Re-run without --live for the full "
              "synthetic batch.")

    summary = summarize(results)

    db.persist_results(results, run_id)
    print(f"Persisted {len(results)} rows to SQLite (run_id={run_id}, "
          f"settlement_source={settlement_source}) -- "
          f"open with review_server.py for the live queue.")

    # "needs_action" separates rows a human must review (EXCEPTION, or an
    # arbiter proposal the confidence gate held) from rows already fully
    # explained or auto-resolved.
    ACTIONABLE = {"EXCEPTION", "MATCHED_LOW_CONFIDENCE"}
    with open(OUTPUT_DIR / "exceptions.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["order_id", "settlement_id", "net_amount", "status", "category", "reason", "narration", "needs_action"])
        for r in results:
            if r["status"] != "MATCHED":
                w.writerow([r.get("order_id"), r.get("settlement_id"), r.get("net"),
                            r["status"], r.get("category"), r.get("reason"),
                            r.get("narration", ""), "yes" if r["status"] in ACTIONABLE else "no"])

    lines = []
    lines.append("# Settlement Reconciliation Report\n")
    lines.append(f"**Settlement data source:** {settlement_source}"
                 + (" (real Razorpay API call, test-mode keys)" if settlement_source == "live" else " (generated, see generate_data.py)") + "\n")
    lines.append(f"**Total rows processed:** {summary['total_rows']}\n")

    arbiter_invoked = (summary["ai_assisted_auto_applied_pct"] + summary["fuzzy_matched_needs_review_pct"]) > 0
    throughput = summary["total_rows"] / elapsed_seconds if elapsed_seconds > 0 else float("inf")
    lines.append(f"**Throughput:** {summary['total_rows']} rows in {elapsed_seconds:.2f}s "
                 f"({throughput:.1f} rows/sec)"
                 + (" -- includes LLM arbiter call(s); Pass 1/2/2.5/2.75 alone are "
                    "sub-second, the arbiter call dominates this number when present" if arbiter_invoked else
                    " -- pure deterministic matching, no arbiter call needed this run")
                 + "\n")

    lines.append(f"**Clean deterministic match:** {summary['clean_match_pct']}%")
    lines.append(f"**Matched with explained variance:** {summary['matched_with_variance_pct']}%")
    lines.append(f"**Unambiguous exact reference (deterministic, no LLM call):** {summary['exact_reference_pct']}%")
    lines.append(f"**Resolved via learned pattern (human-confirmed before):** {summary['learned_pattern_pct']}%")
    lines.append(f"**AI-assisted, auto-applied (confidence >= 0.90 gate):** {summary['ai_assisted_auto_applied_pct']}%")
    lines.append(f"**Fuzzy-matched, flagged for human review:** {summary['fuzzy_matched_needs_review_pct']}%")
    lines.append(f"**Unresolved exceptions:** {summary['unresolved_exception_pct']}%")
    lines.append(f"\n**Overall resolved: {summary['overall_resolved_pct']}%** "
                 f"(industry baseline for manual VLOOKUP reconciliation: ~51%)\n")
    lines.append("## Exceptions by category\n")
    lines.append("| Category | Count | Meaning |")
    lines.append("|---|---|---|")
    meanings = {
        "UNEXPLAINED": "No counterpart found anywhere -- needs manual investigation",
        "DUPLICATE": "Same settlement reported twice, one real bank credit",
        "PARTIAL_PAYMENT": "Refund netted into settlement -- explained, not an error",
        "TAX_DEDUCTION": "GST-on-MDR variance -- check against monthly tax invoice",
        "ROUNDING": "Sub-rupee rounding drift -- explained, not an error",
        "FUZZY_MATCH_NEEDS_REVIEW": "Narration-based candidate match below auto-accept confidence -- human must confirm",
        "AFA_MANDATE_HOLD": "Subscription charge blocked by RBI e-mandate AFA threshold (>Rs.15,000) -- needs compliant step-up re-auth, not a blind retry",
        "ON_HOLD_BY_RAZORPAY": "Razorpay's own API reports on_hold=true for this settlement -- known, held for a reason, not a lost transaction or a normal delay",
        "UTR_LEVEL_MISMATCH": "Money arrived, but under a different UTR than the settlement report claims -- Razorpay's UTR is two-tier (batch vs. line), not a missing payout",
        "DISPUTED": "Settlement recon line carries an active dispute_id -- money arrived but is provisionally at risk of a chargeback clawback",
    }
    for cat, count in summary["exceptions_by_category"].items():
        lines.append(f"| {cat} | {count} | {meanings.get(cat, '')} |")

    if summary["cash_at_risk"] > 0:
        lines.append("\n## Cash-position clarity (not a forecast -- this run's own numbers)\n")
        lines.append(
            f"Rs.{summary['cash_at_risk']:,.2f} in settlement amounts touched some exception or "
            f"variance path this run (duplicate settlement exports excluded -- that money already "
            f"cleared under its sibling row) -- cash position a downstream tool like Cashflow "
            f"Forecaster would otherwise see as ambiguous. This engine resolved "
            f"Rs.{summary['cash_resolved']:,.2f} ({summary['cash_resolved_pct']}%) of it "
            f"deterministically or via a gated match, now trustworthy cash-position input. "
            f"Rs.{summary['cash_pending_review']:,.2f} has a candidate match held for a human to "
            f"confirm, not counted as done. Rs.{summary['cash_still_open']:,.2f} remains genuinely "
            f"open and is disclosed as such, not folded into the resolved figure."
        )

    with open(OUTPUT_DIR / "reconciliation_report.md", "w") as f:
        f.write("\n".join(lines))

    print("Wrote output/reconciliation_report.md and output/exceptions.csv")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                         help="Use the real Razorpay settlement API (test-mode keys) instead of synthetic data")
    parser.add_argument("--fresh-batch", action="store_true",
                         help="Clear all persisted rows first -- pass this whenever generate_data.py just "
                              "created a brand-new dataset (run_all.py does, unless --keep-data), never "
                              "when re-running against data/ already sitting on disk")
    args = parser.parse_args()
    generate(settlement_source="live" if args.live else "synthetic", fresh_batch=args.fresh_batch)
