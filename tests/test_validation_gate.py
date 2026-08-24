"""
Unit tests for validation_gate.py. Exercises resolve_with_gate() against
the real two-tier fallback (Ollama if reachable, the stand-in otherwise),
and separately against faked arbiter results to test the gate's own logic.
"""
import sys
import unittest
import urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import llm_matcher  # noqa: E402
import validation_gate  # noqa: E402


def ollama_is_running():
    # 127.0.0.1, not "localhost" -- see llm_matcher.py's OLLAMA_URL comment,
    # the hostname costs ~2s per call on Windows for no reason.
    try:
        urllib.request.urlopen("http://127.0.0.1:11434", timeout=2)
        return True
    except Exception:
        return False


class TestConfidenceGate(unittest.TestCase):
    """Regardless of which tier answers, a result from a tier not in
    AUTO_APPLY_TRUSTED_TIERS must never auto-apply, even at reported
    confidence 1.0."""

    def test_untrusted_tier_never_auto_applies_even_at_max_confidence(self):
        original_trusted = validation_gate.AUTO_APPLY_TRUSTED_TIERS
        original_call = validation_gate.call_llm_arbiter
        try:
            validation_gate.AUTO_APPLY_TRUSTED_TIERS = set()
            fake_high_confidence = llm_matcher.ArbiterResult(
                "order_1", 1.0, "very sure", False, tier="ollama:test-model"
            )
            validation_gate.call_llm_arbiter = lambda narration, shortlist: fake_high_confidence
            result = validation_gate.resolve_with_gate("narration", ["order_1"])
            self.assertFalse(result.auto_applied)
            self.assertIn("held despite high confidence", result.reason)
        finally:
            validation_gate.AUTO_APPLY_TRUSTED_TIERS = original_trusted
            validation_gate.call_llm_arbiter = original_call

    def test_trusted_tier_auto_applies_above_threshold(self):
        original_trusted = validation_gate.AUTO_APPLY_TRUSTED_TIERS
        original_call = validation_gate.call_llm_arbiter
        try:
            validation_gate.AUTO_APPLY_TRUSTED_TIERS = {"a-trusted-tier"}
            fake_result = llm_matcher.ArbiterResult(
                "order_1", 0.95, "confident", False, tier="a-trusted-tier:test-model"
            )
            validation_gate.call_llm_arbiter = lambda narration, shortlist: fake_result
            result = validation_gate.resolve_with_gate("narration", ["order_1"])
            self.assertTrue(result.auto_applied)
        finally:
            validation_gate.AUTO_APPLY_TRUSTED_TIERS = original_trusted
            validation_gate.call_llm_arbiter = original_call

    def test_trusted_tier_below_threshold_still_held(self):
        original_trusted = validation_gate.AUTO_APPLY_TRUSTED_TIERS
        original_call = validation_gate.call_llm_arbiter
        try:
            validation_gate.AUTO_APPLY_TRUSTED_TIERS = {"a-trusted-tier"}
            fake_result = llm_matcher.ArbiterResult(
                "order_1", 0.5, "not sure", False, tier="a-trusted-tier:test-model"
            )
            validation_gate.call_llm_arbiter = lambda narration, shortlist: fake_result
            result = validation_gate.resolve_with_gate("narration", ["order_1"])
            self.assertFalse(result.auto_applied)
        finally:
            validation_gate.AUTO_APPLY_TRUSTED_TIERS = original_trusted
            validation_gate.call_llm_arbiter = original_call

    def test_empty_shortlist_returns_no_candidate(self):
        result = validation_gate.resolve_with_gate("narration", [])
        self.assertIsNone(result.candidate_id)
        self.assertFalse(result.auto_applied)


@unittest.skipUnless(ollama_is_running(), "Ollama is not running on 127.0.0.1:11434")
class TestRealOllamaIntegration(unittest.TestCase):
    """Only runs if Ollama is actually reachable -- a genuine end-to-end
    call to the local model, skipped (not faked) otherwise."""

    def test_real_call_returns_a_candidate_from_the_shortlist(self):
        result = validation_gate.resolve_with_gate(
            "pymt rcvd Customer26 ord#1036 thx", ["order_1036", "order_1063"],
        )
        self.assertIn(result.candidate_id, ["order_1036", "order_1063", None])
        self.assertFalse(result.auto_applied)  # ollama is never in the trusted set today


if __name__ == "__main__":
    unittest.main()
