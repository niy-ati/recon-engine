"""
Unit tests for reconcile.py's matching engine. Each test builds its own
small, isolated fixture directory (settlement/bank/ledger CSVs) rather than
reusing generate_data.py's shared synthetic batch, so each scenario proves
the matching logic in isolation with a known, hand-checked expected result.
"""
import csv
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from reconcile import reconcile, summarize  # noqa: E402


SETTLEMENT_HEADER = ["settlement_id", "payment_id", "order_id", "gross", "mdr",
                      "gst_on_mdr", "net", "utr", "settlement_date", "on_hold"]
BANK_HEADER = ["utr", "credited_amount", "value_date", "narration"]
LEDGER_HEADER = ["invoice_id", "order_ref", "customer", "amount", "narration", "gst_line"]


def write_fixture(tmpdir, settlement_rows=(), bank_rows=(), ledger_rows=()):
    tmpdir = Path(tmpdir)
    with open(tmpdir / "settlement_report.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(SETTLEMENT_HEADER)
        w.writerows(settlement_rows)
    with open(tmpdir / "bank_statement.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(BANK_HEADER)
        w.writerows(bank_rows)
    with open(tmpdir / "internal_ledger.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(LEDGER_HEADER)
        w.writerows(ledger_rows)
    return tmpdir


def find(results, order_id):
    for r in results:
        if r.get("order_id") == order_id:
            return r
    raise AssertionError(f"no result for order_id={order_id!r} in {results}")


class TestCleanMatch(unittest.TestCase):
    def test_clean_match_resolves_on_utr_and_order_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[["setl_1", "pay_1", "order_1", 999, 19.98, 3.60, 975.42, "UTR001", "2026-08-15", False]],
                bank_rows=[["UTR001", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"]],
                ledger_rows=[["INV-1", "order_1", "Alice", 999, "Payment received order order_1 - Alice", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            r = find(results, "order_1")
            self.assertEqual(r["status"], "MATCHED")
            self.assertIsNone(r["category"])


class TestRoundingDrift(unittest.TestCase):
    def test_small_amount_drift_within_tolerance_is_rounding(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[["setl_1", "pay_1", "order_1", 999, 19.98, 3.60, 975.42, "UTR001", "2026-08-15", False]],
                # bank credited Rs.0.50 less than the settlement's net -- rounding drift
                bank_rows=[["UTR001", 974.92, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"]],
                ledger_rows=[["INV-1", "order_1", "Alice", 999, "Payment received order order_1 - Alice", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            r = find(results, "order_1")
            self.assertEqual(r["status"], "MATCHED_WITH_VARIANCE")
            self.assertEqual(r["category"], "ROUNDING")

    def test_picks_closest_candidate_not_last_iterated(self):
        """Regression test: the near-match loop must pick the closest amount,
        not whichever same-UTR row happens to be iterated last."""
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[["setl_1", "pay_1", "order_1", 999, 19.98, 3.60, 975.42, "UTR001", "2026-08-15", False]],
                bank_rows=[
                    # Listed in an order where the FARTHER candidate comes last --
                    # a naive "last one wins" loop would pick this one, which is wrong.
                    ["UTR001", 975.32, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"],   # diff 0.10, closest
                    ["UTR001", 973.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"],   # diff 2.00, farther
                ],
                ledger_rows=[["INV-1", "order_1", "Alice", 999, "Payment received order order_1 - Alice", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            r = find(results, "order_1")
            self.assertEqual(r["status"], "MATCHED_WITH_VARIANCE")
            self.assertEqual(r["category"], "ROUNDING")
            self.assertIn("0.10", r["reason"])


class TestDuplicateSettlement(unittest.TestCase):
    def test_duplicate_settlement_id_flagged_symmetrically(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[
                    ["setl_1", "pay_1", "order_1", 999, 19.98, 3.60, 975.42, "UTR001", "2026-08-15", False],
                    ["setl_1_dup", "pay_1", "order_1", 999, 19.98, 3.60, 975.42, "UTR001", "2026-08-15", False],
                ],
                bank_rows=[["UTR001", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"]],
                ledger_rows=[["INV-1", "order_1", "Alice", 999, "Payment received order order_1 - Alice", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            statuses = sorted(r["status"] for r in results if r.get("order_id") == "order_1")
            self.assertEqual(statuses, ["EXCEPTION", "MATCHED"])
            dup = [r for r in results if r.get("category") == "DUPLICATE"]
            self.assertEqual(len(dup), 1)

    def test_duplicate_does_not_leak_into_fuzzy_shortlist(self):
        """Regression test: an already-resolved DUPLICATE settlement must not
        remain eligible as a Pass 2.75/3/4 fuzzy-match candidate and steal a
        genuinely ambiguous order's ledger row."""
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[
                    ["setl_1", "pay_1", "order_1015", 999, 19.98, 3.60, 975.42, "UTR001", "2026-08-15", False],
                    ["setl_1_dup", "pay_1", "order_1015", 999, 19.98, 3.60, 975.42, "UTR001", "2026-08-15", False],
                    ["setl_2", "pay_2", "order_1057", 999, 19.98, 3.60, 975.42, "UTR002", "2026-08-15", False],
                ],
                bank_rows=[
                    ["UTR001", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"],
                    ["UTR002", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_2"],
                ],
                ledger_rows=[
                    ["INV-1015", "order_1015", "Alice", 999, "Payment received order order_1015 - Alice", 3.60],
                    # blank order_ref, exact digits for order_1057 -- must resolve to order_1057,
                    # not get stolen by the leftover order_1015 duplicate candidate.
                    ["INV-1057", "", "Bob", 999, "pymt rcvd Bob ord#1057 thx", 3.60],
                ],
            )
            results = reconcile(data_dir=tmp)
            r = find(results, "order_1057")
            self.assertEqual(r["status"], "MATCHED_EXACT_REFERENCE")
            dup = find(results, "order_1015")
            self.assertIn(dup["status"], ("MATCHED", "EXCEPTION"))


class TestOnHold(unittest.TestCase):
    def test_on_hold_categorized_before_unexplained(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[["setl_1", "pay_1", "order_1", 999, 19.98, 3.60, 975.42, "UTR001", "2026-08-15", True]],
                bank_rows=[],  # no bank credit -- the money hasn't moved yet
                ledger_rows=[["INV-1", "order_1", "Alice", 999, "Payment received order order_1 - Alice", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            r = find(results, "order_1")
            self.assertEqual(r["category"], "ON_HOLD_BY_RAZORPAY")
            self.assertEqual(r["status"], "EXCEPTION")


class TestUnexplained(unittest.TestCase):
    def test_bank_credit_with_no_settlement_is_unexplained(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[],
                bank_rows=[["UTR999", 500.00, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_ghost"]],
                ledger_rows=[],
            )
            results = reconcile(data_dir=tmp)
            orphan = [r for r in results if r.get("category") == "UNEXPLAINED" and r.get("settlement_id") is None]
            self.assertEqual(len(orphan), 1)
            self.assertEqual(orphan[0]["status"], "EXCEPTION")


class TestAfaMandateHold(unittest.TestCase):
    def test_afa_narration_categorized_distinctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[],
                bank_rows=[],
                ledger_rows=[["INV-1", "order_1", "Alice", 18500,
                               "Subscription renewal order order_1 - Alice - AFA_MANDATE_HOLD pending step-up auth", 55.62]],
            )
            results = reconcile(data_dir=tmp)
            r = find(results, "order_1")
            self.assertEqual(r["category"], "AFA_MANDATE_HOLD")
            self.assertEqual(r["status"], "EXCEPTION")


class TestPartialRefund(unittest.TestCase):
    def test_refund_narration_flags_partial_payment(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[["setl_1", "pay_1", "order_1", 999, 19.98, 3.60, 675.42, "UTR001", "2026-08-15", False]],
                bank_rows=[["UTR001", 675.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"]],
                ledger_rows=[["INV-1", "order_1", "Alice", 999,
                               "Payment received order order_1 - Alice PARTIAL REFUND 300.0", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            r = find(results, "order_1")
            self.assertEqual(r["category"], "PARTIAL_PAYMENT")
            self.assertEqual(r["status"], "MATCHED_WITH_VARIANCE")


class TestExactDigitReference(unittest.TestCase):
    def test_single_exact_digit_hit_resolves_without_arbiter(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[["setl_1", "pay_1", "order_1042", 999, 19.98, 3.60, 975.42, "UTR001", "2026-08-15", False]],
                bank_rows=[["UTR001", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"]],
                ledger_rows=[["INV-1042", "", "Alice", 999, "pymt rcvd Alice ord#1042 thx", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            r = find(results, "order_1042")
            self.assertEqual(r["status"], "MATCHED_EXACT_REFERENCE")

    def test_ambiguous_double_digit_hit_falls_through_to_arbiter(self):
        """Two unmatched orders whose digits both appear in one narration is
        genuine ambiguity -- Pass 2.75 must NOT guess, and must leave it for
        Pass 3/4 instead."""
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[
                    ["setl_1", "pay_1", "order_1042", 999, 19.98, 3.60, 975.42, "UTR001", "2026-08-15", False],
                    ["setl_2", "pay_2", "order_1077", 999, 19.98, 3.60, 975.42, "UTR002", "2026-08-15", False],
                ],
                bank_rows=[
                    ["UTR001", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"],
                    ["UTR002", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_2"],
                ],
                # both order numbers appear in this single narration -- ambiguous by construction
                ledger_rows=[["INV-X", "", "Alice", 999, "refund for order 1077, original order 1042", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            r1 = find(results, "order_1042")
            r2 = find(results, "order_1077")
            # Neither should have been resolved via the deterministic exact-digit
            # pass, since the narration matched both of them.
            self.assertNotEqual(r1["status"], "MATCHED_EXACT_REFERENCE")
            self.assertNotEqual(r2["status"], "MATCHED_EXACT_REFERENCE")


class TestSummarize(unittest.TestCase):
    def test_percentages_sum_to_total_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[["setl_1", "pay_1", "order_1", 999, 19.98, 3.60, 975.42, "UTR001", "2026-08-15", False]],
                bank_rows=[["UTR001", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"]],
                ledger_rows=[["INV-1", "order_1", "Alice", 999, "Payment received order order_1 - Alice", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            summary = summarize(results)
            self.assertEqual(summary["total_rows"], 1)
            self.assertEqual(summary["overall_resolved_pct"], 100.0)


if __name__ == "__main__":
    unittest.main()
