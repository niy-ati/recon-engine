"""
Unit tests for llm_matcher.py's raw model-calling logic. Gate tests live
in test_validation_gate.py.
"""
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import llm_matcher  # noqa: E402
import validation_gate  # noqa: E402


class TestStandInArbiter(unittest.TestCase):
    def test_picks_first_candidate_with_moderate_confidence(self):
        result = llm_matcher._stand_in_arbiter("some narration", ["order_1", "order_2"])
        self.assertEqual(result.candidate_id, "order_1")
        self.assertLess(result.confidence, validation_gate.CONFIDENCE_AUTO_ACCEPT)
        self.assertEqual(result.tier, "stand-in")
        self.assertFalse(result.auto_applied)


class TestParseArbiterJson(unittest.TestCase):
    def test_valid_response_parses(self):
        result = llm_matcher._parse_arbiter_json(
            '{"candidate_id": "order_1", "confidence": 0.95, "reason": "matches"}',
            ["order_1", "order_2"], source="test",
        )
        self.assertEqual(result.candidate_id, "order_1")
        self.assertEqual(result.confidence, 0.95)
        self.assertFalse(result.auto_applied)

    def test_malformed_json_is_rejected_not_crashed(self):
        result = llm_matcher._parse_arbiter_json("not json at all", ["order_1"], source="test")
        self.assertIsNone(result.candidate_id)
        self.assertEqual(result.confidence, 0.0)

    def test_candidate_outside_shortlist_is_rejected(self):
        result = llm_matcher._parse_arbiter_json(
            '{"candidate_id": "order_999", "confidence": 0.99, "reason": "x"}',
            ["order_1", "order_2"], source="test",
        )
        self.assertIsNone(result.candidate_id)


class TestCallLlmArbiter(unittest.TestCase):
    def test_empty_shortlist_returns_no_candidate(self):
        result = llm_matcher.call_llm_arbiter("narration", [])
        self.assertIsNone(result.candidate_id)
        self.assertFalse(result.auto_applied)

    def test_reconcile_module_has_no_import_path_to_this_module(self):
        """Regression test for Razorpay's own Agent Studio guardrail: "a
        two-layer system: agents operate on verified merchant data, and
        the platform independently validates every action before it goes
        through." reconcile.py must reach the arbiter only through
        validation_gate.py, never by importing llm_matcher directly."""
        reconcile_path = SRC / "reconcile.py"
        source = reconcile_path.read_text()
        self.assertNotIn("llm_matcher", source)
        self.assertIn("validation_gate", source)


if __name__ == "__main__":
    unittest.main()
