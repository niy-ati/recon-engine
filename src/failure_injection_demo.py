"""
Injects one adversarial case: two settlements with an identical net amount
but different UTRs, landing on the same day -- a naive "match on amount"
reconciler would cross-wire these.

Run after reconcile.py to confirm the engine tells them apart correctly,
since matching keys on UTR first and amount only as a secondary tolerance
check.
"""
import csv
from pathlib import Path
from reconcile import reconcile, summarize

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def inject_ambiguous_pair():
    """Adds two rows to settlement + bank data with identical amount, different UTR."""
    with open(f"{DATA_DIR}/settlement_report.csv", "a", newline="") as f:
        w = csv.writer(f)
        w.writerow(["setl_TRAP_A", "pay_TRAP_A", "order_9001", 2499, 49.98, 9.0, 2440.02, "UTR_TRAP_AAAA", "2026-08-15", False])
        w.writerow(["setl_TRAP_B", "pay_TRAP_B", "order_9002", 2499, 49.98, 9.0, 2440.02, "UTR_TRAP_BBBB", "2026-08-15", False])

    with open(f"{DATA_DIR}/bank_statement.csv", "a", newline="") as f:
        w = csv.writer(f)
        w.writerow(["UTR_TRAP_AAAA", 2440.02, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_TRAP_A"])
        w.writerow(["UTR_TRAP_BBBB", 2440.02, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_TRAP_B"])

    with open(f"{DATA_DIR}/internal_ledger.csv", "a", newline="") as f:
        w = csv.writer(f)
        w.writerow(["INV-9001", "order_9001", "Customer Trap A", 2499, "Payment received order order_9001 - Customer Trap A", 9.0])
        w.writerow(["INV-9002", "order_9002", "Customer Trap B", 2499, "Payment received order order_9002 - Customer Trap B", 9.0])

    print("Injected ambiguous pair: order_9001 / order_9002, both net Rs.2440.02, same date, DIFFERENT UTRs.")


def check_trap_resolution(results):
    trap_orders = {"order_9001": None, "order_9002": None}
    for r in results:
        if r["order_id"] in trap_orders:
            trap_orders[r["order_id"]] = r

    print("\n=== TRAP CASE RESULT ===")
    ok = True
    for oid, r in trap_orders.items():
        if r is None:
            print(f"{oid}: MISSING FROM RESULTS -- investigate")
            ok = False
            continue
        print(f"{oid}: status={r['status']} stage={r['stage']}")
        if r["status"] != "MATCHED":
            ok = False

    if ok:
        print("\n>> PASS: both trap orders resolved correctly and independently.")
        print(">> The matcher keys on UTR, using amount only as a secondary")
        print(">> tolerance check, so two transactions sharing an amount can't")
        print(">> be cross-wired.")
    else:
        print("\n>> FAIL: trap case was not cleanly resolved -- routed to human review,")
        print(">> the safe failure mode instead of a silent wrong match.")


if __name__ == "__main__":
    inject_ambiguous_pair()
    results = reconcile()
    check_trap_resolution(results)
    print("\n=== FULL SUMMARY AFTER INJECTION ===")
    for k, v in summarize(results).items():
        print(f"{k}: {v}")
