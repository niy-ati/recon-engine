"""
Proof that the confidence gate generalizes to a different matching
topology, not a second product. Standalone: no DB, no review server.

Settlement reconciliation (reconcile.py) is one-to-one: one settlement row
maps to one order. Virtual account collections are many-to-one: many
payers send money into one shared account, each payment attributed from
whatever weak signal exists -- a registered payer id if lucky, an
amount+narration guess otherwise.

The gate is imported directly from validation_gate.py, not reimplemented --
same threshold, same trusted-tier rule, the same function reconcile.py
itself is restricted to.
"""
import sys
import os
import random
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from validation_gate import resolve_with_gate  # noqa: E402  (the actual reuse)

random.seed(7)

# ---------------------------------------------------------------------------
# Synthetic data: one shared virtual account, many payers, many outstanding
# dues (e.g. a school collecting fees, or a lender collecting EMIs).
# ---------------------------------------------------------------------------

CUSTOMERS = [f"cust_{i:03d}" for i in range(1, 21)]

# Outstanding dues -- what SHOULD be collected
dues = []
for i, cust in enumerate(CUSTOMERS):
    dues.append({
        "invoice_id": f"INV-{2000+i}",
        "customer": cust,
        "expected_amount": random.choice([2500, 5000, 7500, 10000]),
        "registered_vpa": f"{cust}@okhdfcbank" if random.random() > 0.3 else None,  # not everyone pre-registers
    })

# Incoming credits into the shared virtual account -- what ACTUALLY arrived
credits = []
for i, due in enumerate(dues):
    case = random.random()
    if case < 0.55:
        # clean: paid from registered VPA, exact amount
        credits.append({"id": f"cr_{i}", "payer_vpa": due["registered_vpa"] or f"{due['customer']}@okaxis",
                         "amount": due["expected_amount"], "narration": f"Fee payment {due['customer']}"})
    elif case < 0.70:
        # paid from an UNREGISTERED vpa, but narration mentions invoice id -- needs fuzzy match
        credits.append({"id": f"cr_{i}", "payer_vpa": f"randomfriend{i}@paytm",
                         "amount": due["expected_amount"], "narration": f"payment for {due['invoice_id']} pls confirm"})
    elif case < 0.82:
        # underpaid -- partial EMI/fee payment
        partial = round(due["expected_amount"] * 0.6, 2)
        credits.append({"id": f"cr_{i}", "payer_vpa": due["registered_vpa"] or f"{due['customer']}@okaxis",
                         "amount": partial, "narration": f"partial payment {due['customer']}"})
    elif case < 0.92:
        # overpaid -- paid extra (common when someone rounds up or double-pays a fee)
        over = due["expected_amount"] + random.choice([100, 500])
        credits.append({"id": f"cr_{i}", "payer_vpa": due["registered_vpa"] or f"{due['customer']}@okaxis",
                         "amount": over, "narration": f"Fee payment {due['customer']}"})
    else:
        # genuinely unknown payer, no usable signal -- the honest unresolved case
        credits.append({"id": f"cr_{i}", "payer_vpa": "stranger9981@ybl",
                         "amount": due["expected_amount"], "narration": "payment"})

# The trap: two different payers sending the EXACT same amount, both with
# generic narrations -- proves the system doesn't guess when it shouldn't.
dues.append({"invoice_id": "INV-TRAP-A", "customer": "cust_trapA", "expected_amount": 5000, "registered_vpa": None})
dues.append({"invoice_id": "INV-TRAP-B", "customer": "cust_trapB", "expected_amount": 5000, "registered_vpa": None})
credits.append({"id": "cr_trapA", "payer_vpa": "unknown1@ybl", "amount": 5000, "narration": "payment"})
credits.append({"id": "cr_trapB", "payer_vpa": "unknown2@ybl", "amount": 5000, "narration": "payment"})


def reconcile_virtual_account() -> list[dict]:
    results = []
    matched_due_ids = set()

    for credit in credits:
        record = {"credit_id": credit["id"], "amount": credit["amount"], "status": None,
                   "category": None, "matched_invoice": None, "reason": None}

        # PASS 1: exact registered VPA match (deterministic, the "easy" case)
        vpa_match = next((d for d in dues if d["registered_vpa"] == credit["payer_vpa"]
                           and d["invoice_id"] not in matched_due_ids), None)
        if vpa_match:
            diff = credit["amount"] - vpa_match["expected_amount"]
            matched_due_ids.add(vpa_match["invoice_id"])
            record["matched_invoice"] = vpa_match["invoice_id"]
            if abs(diff) < 0.01:
                record["status"] = "MATCHED"
            elif diff < 0:
                record["status"] = "MATCHED_WITH_VARIANCE"
                record["category"] = "UNDERPAID"
                record["reason"] = f"Registered payer, but Rs.{abs(diff)} short of the Rs.{vpa_match['expected_amount']} due."
            else:
                record["status"] = "MATCHED_WITH_VARIANCE"
                record["category"] = "OVERPAID"
                record["reason"] = f"Registered payer, paid Rs.{diff} more than the Rs.{vpa_match['expected_amount']} due."
            results.append(record)
            continue

        # PASS 2: unambiguous exact-amount match against exactly one outstanding due
        amount_matches = [d for d in dues if abs(d["expected_amount"] - credit["amount"]) < 0.01
                           and d["invoice_id"] not in matched_due_ids]
        if len(amount_matches) == 1:
            d = amount_matches[0]
            matched_due_ids.add(d["invoice_id"])
            record["matched_invoice"] = d["invoice_id"]
            record["status"] = "MATCHED_LOW_CONFIDENCE"
            record["category"] = "AMOUNT_ONLY_MATCH_NEEDS_REVIEW"
            record["reason"] = f"Unregistered payer, but exactly one outstanding due (Rs.{d['expected_amount']}) matches the amount -- flagged for human confirm, not auto-applied."
            results.append(record)
            continue

        # PASS 3: narration mentions an invoice id explicitly -- narrow to shortlist
        shortlist = [d["invoice_id"] for d in dues
                     if d["invoice_id"] not in matched_due_ids and d["invoice_id"] in credit["narration"]]

        # PASS 4: THE REUSED GATE -- same primitive as the core settlement engine
        if shortlist or amount_matches:
            candidates = shortlist or [d["invoice_id"] for d in amount_matches]
            gate_result = resolve_with_gate(credit["narration"], candidates)
            if gate_result.auto_applied:
                matched_due_ids.add(gate_result.candidate_id)
                record["matched_invoice"] = gate_result.candidate_id
                record["status"] = "MATCHED"
                record["reason"] = f"Auto-resolved via narration match, confidence {gate_result.confidence}."
            else:
                record["status"] = "MATCHED_LOW_CONFIDENCE"
                record["category"] = "AMBIGUOUS_PAYER_NEEDS_REVIEW"
                record["reason"] = f"Candidate(s) {candidates} found, but confidence {gate_result.confidence} below the 0.90 gate -- held for human review, not guessed."
            results.append(record)
            continue

        # Nothing usable at all -- the honest unresolved case
        record["status"] = "EXCEPTION"
        record["category"] = "UNKNOWN_PAYER"
        record["reason"] = "No registered VPA, no unambiguous amount match, no invoice reference in narration -- genuinely unattributable without a phone call."
        results.append(record)

    return results


def summarize_and_print(results: list[dict]) -> None:
    total = len(results)
    by_status = defaultdict(int)
    for r in results:
        by_status[r["status"]] += 1

    print("=== SMART COLLECT RECONCILIATION (proof-of-generalization, not core pipeline) ===")
    print(f"Total credits processed: {total}")
    for status, count in by_status.items():
        print(f"  {status}: {count} ({100*count/total:.1f}%)")

    print("\n=== TRAP CASE CHECK (two payers, same amount, no other signal) ===")
    trap_results = [r for r in results if r["credit_id"] in ("cr_trapA", "cr_trapB")]
    for r in trap_results:
        print(f"  {r['credit_id']}: status={r['status']} category={r['category']} matched={r['matched_invoice']}")
    trap_ok = all(r["status"] in ("EXCEPTION", "MATCHED_LOW_CONFIDENCE") for r in trap_results)
    print("  >> PASS: correctly refused to guess between two ambiguous same-amount payers."
          if trap_ok else "  >> FAIL: guessed on an ambiguous case -- investigate.")

    print("\n=== SAMPLE EXCEPTIONS ===")
    for r in [r for r in results if r["status"] in ("EXCEPTION", "MATCHED_LOW_CONFIDENCE")][:6]:
        print(f"  {r['credit_id']} (Rs.{r['amount']}): {r['category']} -- {r['reason']}")


if __name__ == "__main__":
    results = reconcile_virtual_account()
    summarize_and_print(results)
