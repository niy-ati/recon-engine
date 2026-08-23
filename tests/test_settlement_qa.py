"""
Unit tests for settlement_qa.py. Seeds a temporary database with known
rows and checks every answer against that known state -- never against
the real data/reconcile.db.
"""
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import db  # noqa: E402
import settlement_qa as qa  # noqa: E402


def make_result(order_id, status, category=None, narration="", net=100.0):
    return {
        "order_id": order_id, "settlement_id": f"setl_{order_id}", "net": net,
        "match_key": f"settlement:setl_{order_id}",
        "status": status, "category": category, "reason": f"test reason for {order_id}",
        "narration": narration, "stage": ["test stage"],
    }


class SettlementQaTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = Path(self._tmpdir.name) / "test_reconcile.db"
        db.persist_results([
            make_result("order_1", "MATCHED"),
            make_result("order_2", "EXCEPTION", category="DUPLICATE"),
            make_result("order_3", "EXCEPTION", category="DUPLICATE"),
            make_result("order_4", "EXCEPTION", category="ON_HOLD_BY_RAZORPAY"),
            make_result("order_5", "MATCHED_LOW_CONFIDENCE", category="FUZZY_MATCH_NEEDS_REVIEW"),
        ], run_id="run-1")

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        self._tmpdir.cleanup()


class TestOrderLookup(SettlementQaTestCase):
    def test_known_order_returns_real_status_and_reason(self):
        result = qa.answer("what happened to order_2")
        self.assertIn("DUPLICATE", result)
        self.assertIn("test reason for order_2", result)

    def test_unknown_order_is_honest_not_fabricated(self):
        result = qa.answer("what happened to order_999")
        self.assertIn("No record of order_999", result)

    def test_confirmed_order_shows_human_decision(self):
        row_id = [r for r in db.get_all_exceptions() if r["order_id"] == "order_2"][0]["id"]
        db.resolve_exception(row_id, "confirm", note="looks right")
        result = qa.answer("why is order_2 unresolved")
        self.assertIn("CONFIRMED", result)
        self.assertIn("looks right", result)


class TestCategoryCount(SettlementQaTestCase):
    def test_duplicate_count_matches_real_data(self):
        result = qa.answer("how many DUPLICATE exceptions")
        self.assertIn("2 row(s)", result)

    def test_on_hold_phrasing_variant(self):
        result = qa.answer("what's on hold right now")
        self.assertIn("1 settlement(s) on hold", result)


class TestOpenCount(SettlementQaTestCase):
    def test_open_count_matches_needs_action_rows(self):
        result = qa.answer("how many are open")
        expected = len(db.get_open_exceptions())
        self.assertIn(f"{expected} row(s)", result)


class TestResolutionRate(SettlementQaTestCase):
    def test_resolution_rate_matches_manual_calculation(self):
        rows = db.get_all_exceptions()
        resolved = sum(1 for r in rows if r["status"] in qa.RESOLVED_STATUSES)
        expected_pct = round(100 * resolved / len(rows), 1)
        result = qa.answer("what's my resolution rate")
        self.assertIn(f"{expected_pct}%", result)

    def test_resolution_rate_excludes_unconfirmed_fuzzy_matches(self):
        """Regression test for the same metrics bug already fixed in
        reconcile.py and review_server.py, caught here too:
        MATCHED_LOW_CONFIDENCE must not count as resolved. Of the 5 seeded
        rows, only order_1 (plain MATCHED) is genuinely resolved with zero
        human input -- 1 of 5, 20.0%, computed independently of
        RESOLVED_STATUSES so a reintroduced bug can't pass by agreeing
        with itself."""
        result = qa.answer("what's my resolution rate")
        self.assertIn("20.0%", result)
        self.assertNotIn("MATCHED_LOW_CONFIDENCE", qa.RESOLVED_STATUSES)


class TestUnknownQuestion(SettlementQaTestCase):
    def test_unrecognized_question_admits_it_not_guesses(self):
        result = qa.answer("what is the capital of France")
        self.assertIn("don't have a way to answer", result)


class TestResolutionGuidance(SettlementQaTestCase):
    def test_guidance_for_explicit_order(self):
        result = qa.answer("how can order_2 be resolved")
        self.assertIn("DUPLICATE", result)
        self.assertIn("excluded from cash totals", result)

    def test_guidance_for_explicit_category(self):
        result = qa.answer("how can a DUPLICATE be resolved")
        self.assertIn("excluded from cash totals", result)

    def test_guidance_without_any_referent_asks_for_one(self):
        result = qa.answer("how can it be resolved")
        self.assertIn("Tell me which order or category", result)

    def test_unknown_order_in_guidance_question_is_honest(self):
        result = qa.answer("how can order_999 be resolved")
        self.assertIn("No record of order_999", result)


class TestFollowUpContext(SettlementQaTestCase):
    def test_follow_up_resolves_using_prior_order(self):
        _, ctx = qa.answer_with_context("what happened to order_2")
        result, ctx2 = qa.answer_with_context("how can it be resolved", ctx)
        self.assertIn("order_2", result)
        self.assertIn("DUPLICATE", result)
        self.assertEqual(ctx2["last_order_id"], "order_2")

    def test_follow_up_resolves_using_prior_category(self):
        _, ctx = qa.answer_with_context("how many DUPLICATE exceptions")
        result, _ = qa.answer_with_context("how can it be resolved", ctx)
        self.assertIn("DUPLICATE", result)
        self.assertIn("excluded from cash totals", result)

    def test_stateless_answer_has_no_memory_across_calls(self):
        qa.answer("what happened to order_2")
        result = qa.answer("how can it be resolved")
        self.assertIn("Tell me which order or category", result)


if __name__ == "__main__":
    unittest.main()
