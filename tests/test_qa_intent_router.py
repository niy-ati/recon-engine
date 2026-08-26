"""
Unit tests for qa_intent_router.py.

Mirrors test_llm_matcher.py's choice: the raw Ollama-calling function
(_call_ollama / route) is never exercised over a real or mocked network
call here -- only its pure, deterministic logic (response parsing,
canonical-phrasing reconstruction) is tested directly. Integration with
settlement_qa.py's fallback wiring (mocking qa_intent_gate.route_gated)
lives in test_settlement_qa.py; gate logic lives in test_qa_intent_gate.py.
"""
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import db  # noqa: E402
import qa_intent_router as router  # noqa: E402
import settlement_qa as qa  # noqa: E402


def make_result(order_id, status, category=None, narration="", net=100.0):
    return {
        "order_id": order_id, "settlement_id": f"setl_{order_id}", "net": net,
        "match_key": f"settlement:setl_{order_id}",
        "status": status, "category": category, "reason": f"test reason for {order_id}",
        "narration": narration, "stage": ["test stage"],
    }


class TestParseResponse(unittest.TestCase):
    def test_valid_response_parses(self):
        r = router._parse_response('{"intent": "open_count", "confidence": 0.95}')
        self.assertEqual(r, ("open_count", 0.95))

    def test_malformed_json_is_rejected_not_crashed(self):
        self.assertIsNone(router._parse_response("not json at all"))

    def test_missing_required_field_is_rejected(self):
        self.assertIsNone(router._parse_response('{"intent": "open_count"}'))

    def test_unknown_intent_value_is_rejected(self):
        """The model is only asked to pick from RESPONSE_SCHEMA's enum, but
        nothing stops a misbehaving response from naming something else --
        must not be trusted just because the JSON otherwise parses."""
        r = router._parse_response('{"intent": "delete_everything", "confidence": 0.99}')
        self.assertIsNone(r)


class TestCanonicalQuestions(unittest.TestCase):
    def test_unknown_intent_yields_no_canonical_question(self):
        r = router.RoutedIntent("unknown", 0.99, "test")
        self.assertIsNone(router.to_canonical_question(r))

    def test_every_non_unknown_intent_has_a_canonical_question(self):
        for intent in router.INTENTS:
            if intent == "unknown":
                continue
            self.assertIn(intent, router.CANONICAL_QUESTIONS, f"missing canonical question for {intent}")


class TestCanonicalPhrasingMatchesRealHandlers(unittest.TestCase):
    """Feeds each intent's canonical string through the REAL
    settlement_qa.answer(), on real seeded data -- catches drift between
    this router's phrasing and settlement_qa.py's own keyword matching
    (e.g. a canonical string that no longer matches after a handler's
    trigger phrases change), not just this file's own logic."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = Path(self._tmpdir.name) / "test_reconcile.db"
        db.persist_results([
            make_result("order_2", "EXCEPTION", category="DUPLICATE"),
            make_result("order_4", "EXCEPTION", category="ON_HOLD_BY_RAZORPAY"),
        ], run_id="run-1")

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        self._tmpdir.cleanup()

    def _canonical(self, intent):
        return router.to_canonical_question(router.RoutedIntent(intent, 0.95, "test"))

    def test_open_count(self):
        self.assertNotEqual(qa.answer(self._canonical("open_count")), qa.FALLBACK_MESSAGE)

    def test_resolution_rate(self):
        self.assertIn("%", qa.answer(self._canonical("resolution_rate")))

    def test_category_breakdown(self):
        self.assertIn("DUPLICATE", qa.answer(self._canonical("category_breakdown")))

    def test_confirmed_count(self):
        self.assertIn("confirmed", qa.answer(self._canonical("confirmed_count")))

    def test_rejected_count(self):
        self.assertIn("rejected", qa.answer(self._canonical("rejected_count")))

    def test_needs_clarification_count(self):
        self.assertIn("clarification", qa.answer(self._canonical("needs_clarification_count")))

    def test_cash_value_overall(self):
        self.assertNotEqual(qa.answer(self._canonical("cash_value_overall")), qa.FALLBACK_MESSAGE)


class TestEntityBearingIntentsWereDeliberatelyDropped(unittest.TestCase):
    """Regression guard for the finding documented in qa_intent_router.py's
    module docstring: any question naming an order_id, settlement_id, or
    category is always fully answered before settlement_qa.py's _answer()
    ever reaches the LLM fallback, so entity-dependent intents would be
    dead code. Pins that finding against the real deterministic handlers
    so a future settlement_qa.py change that broke it (and made the
    router's entity-free assumption wrong) would be caught here."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = Path(self._tmpdir.name) / "test_reconcile.db"
        db.persist_results([
            make_result("order_2", "EXCEPTION", category="DUPLICATE"),
        ], run_id="run-1")

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        self._tmpdir.cleanup()

    def test_order_id_question_never_reaches_the_fallback(self):
        import qa_intent_gate
        from unittest.mock import patch
        with patch.object(qa_intent_gate, "route_gated") as mock_gate:
            qa.answer("what happened to order_2")
            qa.answer("how can order_2 be resolved")
            qa.answer("any similar orders to order_2")
        mock_gate.assert_not_called()

    def test_category_question_never_reaches_the_fallback(self):
        import qa_intent_gate
        from unittest.mock import patch
        with patch.object(qa_intent_gate, "route_gated") as mock_gate:
            qa.answer("list DUPLICATE orders")
            qa.answer("how many DUPLICATE exceptions")
            qa.answer("how much money is in DUPLICATE")
        mock_gate.assert_not_called()

    def test_settlement_id_question_never_reaches_the_fallback(self):
        import qa_intent_gate
        from unittest.mock import patch
        with patch.object(qa_intent_gate, "route_gated") as mock_gate:
            qa.answer("what happened to setl_abc123")
        mock_gate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
