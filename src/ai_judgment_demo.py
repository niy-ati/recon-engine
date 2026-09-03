"""
Every other case in this codebase that reaches the arbiter (Pass 4) is
either mechanical digit extraction (one clean order number in free text)
or the one adversarial case that already proved the model unreliable
(see validation_gate.py, README's "AI Usage and Validation"). This was
built to go looking, in good faith, for the opposite kind of case: one
where the model earns trust on real judgment, not just extraction.

The honest result, from actually running it, repeatedly, against the
real model, not asserted: it didn't find one. Injects a ledger narration
naming two real order numbers in one sentence, only one of which the
narration says was actually paid -- the other explicitly cancelled and
refunded. Tried three independent phrasings/shortlist-orderings during
development. Two picked the wrong order outright; the one that picked
correctly still gave an incoherent stated reason ("cancelled and
confirmed," conflating the two clauses it was supposed to be
distinguishing). The model's own output, quoted directly below, not
paraphrased or cleaned up.

This does not weaken the case for the current trust posture -- it's a
second, independent line of evidence for it, on top of the original
adversarial case, not a repeat of the same one. AUTO_APPLY_TRUSTED_TIERS
stays empty regardless of what this prints: even a correct pick here
still lands as MATCHED_LOW_CONFIDENCE / FUZZY_MATCH_NEEDS_REVIEW, held
for a human, exactly like every other arbiter pick today.

Run after reconcile.py, same as failure_injection_demo.py.
"""
import csv
from pathlib import Path
from reconcile import reconcile

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

DISTRACTOR_ORDER = "order_8001"   # mentioned, but cancelled/refunded -- the wrong pick
CORRECT_ORDER = "order_8002"      # what this ledger row actually documents
AMBIGUOUS_NARRATION = (
    "order 8001 payment cancelled and refunded in full, "
    "order 8002 payment received and confirmed"
)


def inject_context_judgment_case() -> None:
    """Adds two clean settlements (each resolves fine on the bank side) and
    one shared, deliberately ambiguous ledger row referencing both by
    number -- correct only by reading what the narration actually says."""
    with open(f"{DATA_DIR}/settlement_report.csv", "a", newline="") as f:
        w = csv.writer(f)
        w.writerow(["setl_CTX_A", "pay_CTX_A", DISTRACTOR_ORDER, 1999, 39.98, 7.2, 1951.82, "UTR_CTX_AAAA", "2026-08-16", False])
        w.writerow(["setl_CTX_B", "pay_CTX_B", CORRECT_ORDER, 3499, 69.98, 12.6, 3416.42, "UTR_CTX_BBBB", "2026-08-16", False])

    with open(f"{DATA_DIR}/bank_statement.csv", "a", newline="") as f:
        w = csv.writer(f)
        w.writerow(["UTR_CTX_AAAA", 1951.82, "2026-08-16", "NEFT CR RAZORPAY SETTLEMENT setl_CTX_A"])
        w.writerow(["UTR_CTX_BBBB", 3416.42, "2026-08-16", "NEFT CR RAZORPAY SETTLEMENT setl_CTX_B"])

    with open(f"{DATA_DIR}/internal_ledger.csv", "a", newline="") as f:
        w = csv.writer(f)
        # order_ref deliberately blank -- Pass 2 must not resolve this by
        # a structured field, only Pass 2.75/3/4 ever see the narration.
        w.writerow(["INV-CTX-AMBIG", "", "Customer Ambiguous", 3416.42, AMBIGUOUS_NARRATION, 12.6])

    print(f"Injected a context-judgment case: one ledger narration names both "
          f"{DISTRACTOR_ORDER} (cancelled, refunded) and {CORRECT_ORDER} (paid, confirmed).")


def check_judgment_result(results: list[dict]) -> bool:
    correct_row = next((r for r in results if r["order_id"] == CORRECT_ORDER), None)
    distractor_row = next((r for r in results if r["order_id"] == DISTRACTOR_ORDER), None)

    print("\n=== AI JUDGMENT CASE RESULT (reported honestly either way, not re-run until it passes) ===")
    if correct_row is None or distractor_row is None:
        print("MISSING FROM RESULTS -- investigate")
        return False

    arbiter_row = correct_row if correct_row["status"] in ("MATCHED_LOW_CONFIDENCE", "MATCHED_AI_ASSISTED") else distractor_row
    detail = next((s["detail"] for s in arbiter_row["stage"] if s["pass"] == "3/4"), None)
    picked_correctly = correct_row["status"] in ("MATCHED_LOW_CONFIDENCE", "MATCHED_AI_ASSISTED")

    print(f"{CORRECT_ORDER}: status={correct_row['status']} category={correct_row['category']}")
    print(f"{DISTRACTOR_ORDER}: status={distractor_row['status']} category={distractor_row['category']}")
    print(f"\nModel's own output this run: {detail}")

    if picked_correctly:
        print(f"\n>> Correctly picked {CORRECT_ORDER} this run. Still held for human review, not "
              f"auto-applied -- AUTO_APPLY_TRUSTED_TIERS is unchanged by this result. Read the "
              f"quoted reasoning above before trusting a correct pick alone: during development "
              f"this same case, phrased differently, twice picked the wrong order (see "
              f"notes/PITCH_NOTES.md) -- a right answer here is not, on its own, evidence the model can "
              f"be trusted with this kind of question.")
    else:
        print(f"\n>> Picked {DISTRACTOR_ORDER} instead of {CORRECT_ORDER} this run -- the order the "
              f"narration explicitly says was cancelled and refunded, not the one it says was "
              f"paid. This is the honest, expected outcome of this test more often than not (see "
              f"notes/PITCH_NOTES.md for two other real, differently-phrased attempts at this exact "
              f"scenario during development, both also wrong). Reinforces, not undermines, why "
              f"AUTO_APPLY_TRUSTED_TIERS stays empty.")
    return picked_correctly


if __name__ == "__main__":
    inject_context_judgment_case()
    results = reconcile()
    check_judgment_result(results)
