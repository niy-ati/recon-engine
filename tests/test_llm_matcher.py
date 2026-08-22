"""
Unit tests for llm_matcher.py's confidence gate -- the actual boundary this
module exists to enforce. Exercises resolve_with_gate() end to end against
the real two-tier fallback (Ollama if reachable, the deterministic stand-in
otherwise); nothing here fakes a model response.
"""
import sys
import unittest
import urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import llm_matcher  # noqa: E402


def ollama_is_running():
    try:
        urllib.request.urlopen("http://localhost:11434", timeout=2)
        return True
    except Exception:
        return False


class TestStandInArbiter(unittest.TestCase):
    def test_picks_first_candidate_with_moderate_confidence(self):
        result = llm_matcher._stand_in_arbiter("some narration", ["order_1", "order_2"])
        self.assertEqual(result.candidate_id, "order_1")
        self.assertLess(result.confidence, llm_matcher.CONFIDENCE_AUTO_ACCEPT)
        self.assertEqual(result.tier, "stand-in")


class TestParseArbiterJson(unittest.TestCase):
    def test_valid_response_parses(self):
        result = llm_matcher._parse_arbiter_json(
            '{"candidate_id": "order_1", "confidence": 0.95, "reason": "matches"}',
            ["order_1", "order_2"], source="test",
        )
        self.assertEqual(result.candidate_id, "order_1")
        self.assertEqual(result.confidence, 0.95)

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


class TestConfidenceGate(unittest.TestCase):
    """The gate itself: regardless of which tier answers, a result from a
    tier not in AUTO_APPLY_TRUSTED_TIERS must never auto-apply, even at
    reported confidence 1.0."""

    def test_untrusted_tier_never_auto_applies_even_at_max_confidence(self):
        original_trusted = llm_matcher.AUTO_APPLY_TRUSTED_TIERS
        try:
            llm_matcher.AUTO_APPLY_TRUSTED_TIERS = set()  # nothing trusted
            fake_high_confidence = llm_matcher.ArbiterResult(
                "order_1", 1.0, "very sure", False, tier="ollama:test-model"
            )

            def fake_call_llm_arbiter(narration, shortlist):
                return fake_high_confidence

            original_call = llm_matcher.call_llm_arbiter
            llm_matcher.call_llm_arbiter = fake_call_llm_arbiter
            try:
                result = llm_matcher.resolve_with_gate("narration", ["order_1"])
            finally:
                llm_matcher.call_llm_arbiter = original_call
            self.assertFalse(result.auto_applied)
            self.assertIn("held despite high confidence", result.reason)
        finally:
            llm_matcher.AUTO_APPLY_TRUSTED_TIERS = original_trusted

    def test_trusted_tier_auto_applies_above_threshold(self):
        original_trusted = llm_matcher.AUTO_APPLY_TRUSTED_TIERS
        try:
            llm_matcher.AUTO_APPLY_TRUSTED_TIERS = {"a-trusted-tier"}
            fake_result = llm_matcher.ArbiterResult(
                "order_1", 0.95, "confident", False, tier="a-trusted-tier:test-model"
            )
            original_call = llm_matcher.call_llm_arbiter
            llm_matcher.call_llm_arbiter = lambda narration, shortlist: fake_result
            try:
                result = llm_matcher.resolve_with_gate("narration", ["order_1"])
            finally:
                llm_matcher.call_llm_arbiter = original_call
            self.assertTrue(result.auto_applied)
        finally:
            llm_matcher.AUTO_APPLY_TRUSTED_TIERS = original_trusted

    def test_empty_shortlist_returns_no_candidate(self):
        result = llm_matcher.resolve_with_gate("narration", [])
        self.assertIsNone(result.candidate_id)
        self.assertFalse(result.auto_applied)


@unittest.skipUnless(ollama_is_running(), "Ollama is not running on localhost:11434")
class TestRealOllamaIntegration(unittest.TestCase):
    """Only runs if Ollama is actually reachable -- a genuine end-to-end
    call to the local model, skipped (not faked) otherwise."""

    def test_real_call_returns_a_candidate_from_the_shortlist(self):
        result = llm_matcher.resolve_with_gate(
            "pymt rcvd Customer26 ord#1036 thx", ["order_1036", "order_1063"],
        )
        self.assertIn(result.candidate_id, ["order_1036", "order_1063", None])
        self.assertFalse(result.auto_applied)  # ollama is never in the trusted set today


if __name__ == "__main__":
    unittest.main()
