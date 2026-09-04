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
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from reconcile import reconcile, summarize  # noqa: E402
import db  # noqa: E402
import reconcile as reconcile_module  # noqa: E402 -- for patching resolve_with_gate below
from llm_matcher import ArbiterResult  # noqa: E402


SETTLEMENT_HEADER = ["settlement_id", "payment_id", "order_id", "gross", "mdr",
                      "gst_on_mdr", "net", "utr", "settlement_date", "on_hold",
                      "method", "dispute_id"]
# method/dispute_id trail every existing fixture row above -- a row with
# fewer values than the header reads back as None for those columns
# (csv.DictReader's own documented behavior), so no existing fixture in
# this file needs updating; only a test that cares about method/dispute_id
# passes those two extra positional values explicitly.
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

    def test_gross_mdr_utr_settlement_date_carried_through(self):
        """Regression test: these real settlement fields must reach the
        persisted result -- settlement_qa.py's date lookup and "next
        settlement" answers, and the Records page's own row display, all
        depend on them being here, not silently dropped the way they used
        to be (only net/gst_on_mdr made it through before)."""
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[["setl_1", "pay_1", "order_1", 999, 19.98, 3.60, 975.42, "UTR001", "2026-08-15", False]],
                bank_rows=[["UTR001", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"]],
                ledger_rows=[["INV-1", "order_1", "Alice", 999, "Payment received order order_1 - Alice", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            r = find(results, "order_1")
            self.assertEqual(r["gross"], 999.0)
            self.assertEqual(r["mdr"], 19.98)
            self.assertEqual(r["utr"], "UTR001")
            self.assertEqual(r["settlement_date"], "2026-08-15")

    def test_orphan_bank_row_carries_its_own_utr_and_date(self):
        """A bank credit with no settlement report line at all has no real
        gross/MDR to report (nothing backs that breakdown), but its own UTR
        and value_date ARE real and must still come through -- honest
        partial data, not silently dropped alongside the fields that
        genuinely don't exist for this row."""
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[],
                bank_rows=[["UTR999", 500.00, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_ghost"]],
                ledger_rows=[],
            )
            results = reconcile(data_dir=tmp)
            orphan = [r for r in results if r.get("category") == "UNEXPLAINED" and r.get("settlement_id") is None][0]
            self.assertEqual(orphan["utr"], "UTR999")
            self.assertEqual(orphan["settlement_date"], "2026-08-15")
            self.assertIsNone(orphan.get("gross"))
            self.assertIsNone(orphan.get("mdr"))


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

    def test_rounding_row_carries_a_stage_entry(self):
        """Regression test for a real bug: this branch set category/reason
        but never appended to record["stage"], so the Records page's replay
        log showed "(no stages recorded)" for a row that had a real reason."""
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[["setl_1", "pay_1", "order_1", 999, 19.98, 3.60, 975.42, "UTR001", "2026-08-15", False]],
                bank_rows=[["UTR001", 974.92, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"]],
                ledger_rows=[["INV-1", "order_1", "Alice", 999, "Payment received order order_1 - Alice", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            r = find(results, "order_1")
            self.assertTrue(r["stage"], "ROUNDING row must have a non-empty replay log")


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

    def test_duplicate_row_carries_a_stage_entry(self):
        """Regression test for a real bug: DUPLICATE rows had a real `reason`
        but an empty `stage` list, so the Records page's replay log rendered
        "(no stages recorded)" even though the detection logic had a concrete
        basis (the matching base settlement_id) worth showing."""
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
            dup = [r for r in results if r.get("category") == "DUPLICATE"][0]
            self.assertTrue(dup["stage"], "DUPLICATE row must have a non-empty replay log")
            self.assertIn("setl_1", dup["stage"][0]["detail"])

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


class TestDisputed(unittest.TestCase):
    def test_dispute_id_overrides_a_clean_bank_match(self):
        """A disputed settlement typically still has a real, clean bank
        credit -- the money genuinely arrived. DISPUTED must still win over
        the plain MATCHED outcome the bank-matching logic would otherwise
        produce, since the dispute is the more urgent fact a merchant
        needs surfaced."""
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[["setl_1", "pay_1", "order_1", 999, 19.98, 3.60, 975.42, "UTR001",
                                   "2026-08-15", False, "UPI", "disp_abc123"]],
                bank_rows=[["UTR001", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"]],
                ledger_rows=[["INV-1", "order_1", "Alice", 999, "Payment received order order_1 - Alice", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            r = find(results, "order_1")
            self.assertEqual(r["category"], "DISPUTED")
            self.assertEqual(r["status"], "EXCEPTION")
            self.assertIn("disp_abc123", r["reason"])

    def test_disputed_row_still_carries_a_stage_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[["setl_1", "pay_1", "order_1", 999, 19.98, 3.60, 975.42, "UTR001",
                                   "2026-08-15", False, "UPI", "disp_abc123"]],
                bank_rows=[["UTR001", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"]],
                ledger_rows=[["INV-1", "order_1", "Alice", 999, "Payment received order order_1 - Alice", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            r = find(results, "order_1")
            self.assertTrue(r["stage"], "DISPUTED row must have a non-empty replay log")

    def test_disputed_bank_row_is_still_claimed_not_left_as_a_phantom_orphan(self):
        """Regression guard: the dispute override must run AFTER the normal
        bank-matching logic, not instead of it -- otherwise the disputed
        settlement's own bank row never gets added to matched_bank_rows and
        would wrongly resurface as a second, phantom UNEXPLAINED orphan for
        money that has already been accounted for once."""
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[["setl_1", "pay_1", "order_1", 999, 19.98, 3.60, 975.42, "UTR001",
                                   "2026-08-15", False, "UPI", "disp_abc123"]],
                bank_rows=[["UTR001", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"]],
                ledger_rows=[["INV-1", "order_1", "Alice", 999, "Payment received order order_1 - Alice", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            orphans = [r for r in results if r.get("settlement_id") is None and r.get("category") == "UNEXPLAINED"]
            self.assertEqual(orphans, [])

    def test_no_dispute_id_is_unaffected(self):
        """An empty dispute_id (the CSV default for every non-disputed row)
        must not trip the override -- confirms falsy-but-present values
        ("" from the synthetic generator, not just a missing column) are
        handled the same as a genuinely absent one."""
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[["setl_1", "pay_1", "order_1", 999, 19.98, 3.60, 975.42, "UTR001",
                                   "2026-08-15", False, "UPI", ""]],
                bank_rows=[["UTR001", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"]],
                ledger_rows=[["INV-1", "order_1", "Alice", 999, "Payment received order order_1 - Alice", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            r = find(results, "order_1")
            self.assertEqual(r["status"], "MATCHED")
            self.assertIsNone(r["category"])


class TestPaymentMethod(unittest.TestCase):
    def test_method_is_carried_through_to_the_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[["setl_1", "pay_1", "order_1", 999, 19.98, 3.60, 975.42, "UTR001",
                                   "2026-08-15", False, "UPI", ""]],
                bank_rows=[["UTR001", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"]],
                ledger_rows=[["INV-1", "order_1", "Alice", 999, "Payment received order order_1 - Alice", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            r = find(results, "order_1")
            self.assertEqual(r["method"], "UPI")

    def test_missing_method_column_is_none_not_a_crash(self):
        """Every pre-existing fixture in this file writes rows without a
        method column at all -- confirms that keeps working exactly like
        it already implicitly does everywhere else in this file, not a new
        behavior introduced only for the tests that pass it explicitly."""
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[["setl_1", "pay_1", "order_1", 999, 19.98, 3.60, 975.42, "UTR001", "2026-08-15", False]],
                bank_rows=[["UTR001", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"]],
                ledger_rows=[["INV-1", "order_1", "Alice", 999, "Payment received order order_1 - Alice", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            r = find(results, "order_1")
            self.assertIsNone(r["method"])


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

    def test_unexplained_settlement_row_carries_a_stage_entry(self):
        """Same bug as DUPLICATE/ROUNDING: a settlement-side UNEXPLAINED row
        must have a non-empty replay log, not just a `reason` string."""
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[["setl_1", "pay_1", "order_1", 999, 19.98, 3.60, 975.42, "UTR001", "2026-08-15", False]],
                bank_rows=[],
                ledger_rows=[["INV-1", "order_1", "Alice", 999, "Payment received order order_1 - Alice", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            r = find(results, "order_1")
            self.assertEqual(r["category"], "UNEXPLAINED")
            self.assertTrue(r["stage"], "UNEXPLAINED settlement row must have a non-empty replay log")


class TestUTRLevelMismatch(unittest.TestCase):
    def test_amount_date_match_under_different_utr_resolves_as_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[["setl_1", "pay_1", "order_1", 999, 19.98, 3.60, 975.42, "UTR001", "2026-08-15", False]],
                # Bank credit matches amount+date exactly, but under a UTR
                # the settlement report never mentions -- the real two-tier
                # UTR quirk, not a missing payout.
                bank_rows=[["UTR999", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"]],
                ledger_rows=[["INV-1", "order_1", "Alice", 999, "Payment received order order_1 - Alice", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            r = find(results, "order_1")
            self.assertEqual(r["status"], "MATCHED_WITH_VARIANCE")
            self.assertEqual(r["category"], "UTR_LEVEL_MISMATCH")
            self.assertIn("UTR999", r["reason"])

    def test_ambiguous_cross_utr_candidates_does_not_guess(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[["setl_1", "pay_1", "order_1", 999, 19.98, 3.60, 975.42, "UTR001", "2026-08-15", False]],
                # TWO different bank rows match amount+date under two
                # different UTRs -- a genuine same-amount/same-day
                # coincidence, not a resolvable mismatch.
                bank_rows=[
                    ["UTR888", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT other_a"],
                    ["UTR999", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT other_b"],
                ],
                ledger_rows=[["INV-1", "order_1", "Alice", 999, "Payment received order order_1 - Alice", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            r = find(results, "order_1")
            self.assertEqual(r["status"], "EXCEPTION")
            self.assertEqual(r["category"], "UNEXPLAINED")


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


class TestNarrationTemplates(unittest.TestCase):
    """Pass 2.6: a template learned from one confirmed narration
    (db.resolve_exception, see test_db.py for the write side) generalizing
    beyond the exact-string narration_rules match. Uses an isolated
    DB_PATH, same pattern as test_db.py's DbTestCase, since reconcile()
    reads narration_templates for real -- not mocked here, the actual
    lookup runs against a real (temporary) SQLite file."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = Path(self._tmpdir.name) / "test_reconcile.db"

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        self._tmpdir.cleanup()

    def _seed_template(self, template):
        conn = db.get_connection()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO narration_templates (template, confirmed_at, source) VALUES (?, ?, ?)",
                    (template, "2026-01-01T00:00:00+00:00", "human_review"),
                )
        finally:
            conn.close()

    def test_template_disambiguates_a_narration_pass_2_75_would_decline(self):
        """The exact same ambiguous narration shape as
        test_ambiguous_double_digit_hit_falls_through_to_arbiter -- two
        real order numbers both appear as substrings, so Pass 2.75 alone
        would decline. A template anchored to the correct position
        (learned from an earlier, unrelated confirm that also ended in
        'original order 1042') disambiguates which number is actually
        this row's reference."""
        self._seed_template("refund for order {REF}, original order 1042")
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
                ledger_rows=[["INV-X", "", "Alice", 999, "refund for order 1077, original order 1042", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            r = find(results, "order_1077")
            self.assertEqual(r["status"], "MATCHED_LEARNED_PATTERN")

    def test_template_match_with_no_valid_candidate_does_not_guess(self):
        """The template's fixed text matches, but the captured reference
        doesn't correspond to any currently-unmatched order -- must not
        invent a match."""
        self._seed_template("pymt rcvd Alice ord#{REF} thx")
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[["setl_1", "pay_1", "order_9999", 999, 19.98, 3.60, 975.42, "UTR001", "2026-08-15", False]],
                bank_rows=[["UTR001", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"]],
                # captures "1234" -- not this batch's order at all
                ledger_rows=[["INV-1", "", "Alice", 999, "pymt rcvd Alice ord#1234 thx", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            r = find(results, "order_9999")
            self.assertNotEqual(r["status"], "MATCHED_LEARNED_PATTERN")


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

    def test_cash_clarity_splits_resolved_from_still_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[["setl_1", "pay_1", "order_1", 999, 19.98, 3.60, 975.42, "UTR001", "2026-08-15", False]],
                bank_rows=[
                    # Rs.0.50 drift -> ROUNDING, resolved (MATCHED_WITH_VARIANCE).
                    ["UTR001", 974.92, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"],
                    # No matching settlement at all -> UNEXPLAINED, genuinely still open.
                    ["UTR999", 500.00, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_ghost"],
                ],
                ledger_rows=[["INV-1", "order_1", "Alice", 999, "Payment received order order_1 - Alice", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            summary = summarize(results)
            self.assertAlmostEqual(summary["cash_at_risk"], 1475.42, places=2)
            self.assertAlmostEqual(summary["cash_resolved"], 975.42, places=2)
            self.assertAlmostEqual(summary["cash_still_open"], 500.00, places=2)
            self.assertAlmostEqual(
                summary["cash_resolved_pct"], round(100 * 975.42 / 1475.42, 1), places=1,
            )

    def test_matched_low_confidence_is_pending_review_not_resolved(self):
        """Regression test for a real bug: MATCHED_LOW_CONFIDENCE is an
        unconfirmed arbiter candidate sitting in the human review queue --
        db.py's own needs_action rule already treats it like EXCEPTION, not
        like a resolved row. summarize() used to fold it into both
        "resolved" (row count) and "cash_resolved" (Rs. amount), letting
        the headline number count a human's not-yet-made decision as done.
        Tests summarize()'s aggregation directly with constructed rows --
        decoupled from coaxing a specific status out of the full matching
        pipeline via a fragile fixture."""
        results = [
            {"status": "MATCHED_LOW_CONFIDENCE", "category": "FUZZY_MATCH_NEEDS_REVIEW", "net": 300.0},
            {"status": "MATCHED", "category": None, "net": 100.0},
        ]
        summary = summarize(results)
        self.assertEqual(summary["cash_resolved"], 0.0)
        self.assertEqual(summary["cash_pending_review"], 300.0)
        self.assertEqual(summary["cash_at_risk"], 300.0)
        # Row-count "resolved" must not count the low-confidence row either --
        # only the plain MATCHED row (category=None, never touches cash_at_risk
        # since it never hit an exception/variance path) counts as resolved.
        self.assertEqual(summary["overall_resolved_pct"], 50.0)

    def test_duplicate_excluded_from_every_cash_bucket(self):
        """Regression test for a real bug: a DUPLICATE row is a second
        settlement-report ENTRY for a transaction whose real money already
        cleared under its sibling row (see
        test_duplicate_settlement_id_flagged_symmetrically). Counting its
        net amount as separate at-risk cash double-counts money that
        already landed -- the opposite of honest disclosure."""
        results = [
            {"status": "EXCEPTION", "category": "DUPLICATE", "net": 999.0},
            {"status": "EXCEPTION", "category": "UNEXPLAINED", "net": 200.0},
        ]
        summary = summarize(results)
        self.assertEqual(summary["cash_at_risk"], 200.0)
        self.assertEqual(summary["cash_still_open"], 200.0)
        self.assertEqual(summary["cash_resolved"], 0.0)
        self.assertEqual(summary["cash_pending_review"], 0.0)


class TestStructuredLogging(unittest.TestCase):
    def test_stage_entries_are_structured_not_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[["setl_1", "pay_1", "order_1", 999, 19.98, 3.60, 975.42, "UTR001", "2026-08-15", False]],
                bank_rows=[["UTR001", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"]],
                ledger_rows=[["INV-1", "order_1", "Alice", 999, "Payment received order order_1 - Alice", 3.60]],
            )
            results = reconcile(data_dir=tmp)
            r = find(results, "order_1")
            self.assertGreater(len(r["stage"]), 0)
            for entry in r["stage"]:
                self.assertIsInstance(entry, dict)
                for key in ("pass", "action", "detail", "confidence", "timestamp", "correlation_id"):
                    self.assertIn(key, entry)

    def test_every_row_shares_one_correlation_id_per_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[
                    ["setl_1", "pay_1", "order_1", 999, 19.98, 3.60, 975.42, "UTR001", "2026-08-15", False],
                    ["setl_2", "pay_2", "order_2", 999, 19.98, 3.60, 975.42, "UTR002", "2026-08-15", False],
                ],
                bank_rows=[
                    ["UTR001", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"],
                    ["UTR002", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_2"],
                ],
                ledger_rows=[
                    ["INV-1", "order_1", "Alice", 999, "Payment received order order_1 - Alice", 3.60],
                    ["INV-2", "order_2", "Bob", 999, "Payment received order order_2 - Bob", 3.60],
                ],
            )
            results = reconcile(data_dir=tmp, correlation_id="fixed-test-id")
            ids = {entry["correlation_id"] for r in results for entry in r["stage"]}
            self.assertEqual(ids, {"fixed-test-id"})

    def test_explicit_correlation_id_is_honored(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(
                tmp,
                settlement_rows=[["setl_1", "pay_1", "order_1", 999, 19.98, 3.60, 975.42, "UTR001", "2026-08-15", False]],
                bank_rows=[["UTR001", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1"]],
                ledger_rows=[["INV-1", "order_1", "Alice", 999, "Payment received order order_1 - Alice", 3.60]],
            )
            results = reconcile(data_dir=tmp, correlation_id="my-run-42")
            r = find(results, "order_1")
            self.assertEqual(r["stage"][0]["correlation_id"], "my-run-42")


class TestPass4AmountSanity(unittest.TestCase):
    """A confident, trusted-tier arbiter result still says nothing about
    whether the amounts involved actually agree -- narration similarity
    and net amount are independent signals. resolve_with_gate is patched
    directly here (not exercised through the real Ollama/stand-in
    arbiter) so the scenario is deterministic: a fixed, already-"trusted"
    ArbiterResult is what this check has to override, not a real model's
    unpredictable output."""

    def _fixture(self, tmp, ledger_amount):
        write_fixture(
            tmp,
            settlement_rows=[["setl_1090", "pay_1090", "order_1090", 999, 19.98, 3.60, 975.42, "UTR1090", "2026-08-15", False]],
            bank_rows=[["UTR1090", 975.42, "2026-08-15", "NEFT CR RAZORPAY SETTLEMENT setl_1090"]],
            # Blank order_ref and no exact digit reference to order_1090's
            # own suffix -- forces this row through Pass 3/4 (fuzzy) for
            # the ledger side, verified separately: difflib's own
            # get_close_matches(cutoff=0.3) matches this narration against
            # "order order_1090" despite sharing no digits at all.
            ledger_rows=[["INV-X", "", "Someone", ledger_amount, "payment received thanks a lot for your order", 3.60]],
        )

    def test_amount_mismatch_overrides_auto_applied(self):
        fake_result = ArbiterResult("order_1090", 0.95, "test: high confidence", True, tier="trusted-tier")
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(tmp, ledger_amount=5000)  # settlement net is 975.42 -- wildly different
            with patch.object(reconcile_module, "resolve_with_gate", return_value=fake_result):
                results = reconcile(data_dir=tmp)
            r = find(results, "order_1090")
            self.assertNotEqual(r["status"], "MATCHED_AI_ASSISTED")  # would only be set if auto_applied stayed True
            self.assertIn("differ by", r["reason"])
            # The real bug this test itself caught during development: the
            # reason text must actually say why it's held (the amounts
            # disagree) -- not silently fall through to the "tier not
            # trusted" text, which would tell a human reviewer, wrongly,
            # that the match looks fine and this is purely a policy hold.
            self.assertNotIn("not because the match itself looks wrong", r["reason"])

    def test_amount_agreement_leaves_auto_applied_alone(self):
        """Same fixed arbiter result, but this time the ledger amount
        genuinely agrees with the settlement's own net -- the new check
        must not touch a case it has no reason to."""
        fake_result = ArbiterResult("order_1090", 0.95, "test: high confidence", True, tier="trusted-tier")
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(tmp, ledger_amount=975.42)
            with patch.object(reconcile_module, "resolve_with_gate", return_value=fake_result):
                results = reconcile(data_dir=tmp)
            r = find(results, "order_1090")
            self.assertEqual(r["status"], "MATCHED_AI_ASSISTED")
            self.assertNotIn("amount check", r["reason"])

    def test_small_drift_within_tolerance_is_not_a_finding(self):
        """A rupee or so of drift is normal noise, not a real mismatch --
        must stay under PASS4_AMOUNT_SANITY_TOLERANCE_RS."""
        fake_result = ArbiterResult("order_1090", 0.95, "test: high confidence", True, tier="trusted-tier")
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(tmp, ledger_amount=976.00)  # 0.58 off the real 975.42
            with patch.object(reconcile_module, "resolve_with_gate", return_value=fake_result):
                results = reconcile(data_dir=tmp)
            r = find(results, "order_1090")
            self.assertEqual(r["status"], "MATCHED_AI_ASSISTED")


if __name__ == "__main__":
    unittest.main()
