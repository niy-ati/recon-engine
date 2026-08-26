"""
Unit tests for qa_intent_gate.py. Mirrors test_validation_gate.py's own
structure: fake results test the gate's logic in isolation, and a separate
class runs a genuine end-to-end call against Ollama if it's reachable,
skipped otherwise.
"""
import sys
import unittest
import urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import qa_intent_gate  # noqa: E402
import qa_intent_router  # noqa: E402


def ollama_is_running():
    try:
        urllib.request.urlopen("http://127.0.0.1:11434", timeout=2)
        return True
    except Exception:
        return False


class TestRouteGated(unittest.TestCase):
    def test_untrusted_tier_never_passes_even_at_max_confidence(self):
        original_trusted = qa_intent_gate.TRUSTED_TIERS
        original_route = qa_intent_router.route
        try:
            qa_intent_gate.TRUSTED_TIERS = set()
            fake = qa_intent_router.RoutedIntent("open_count", 1.0, tier="ollama:test-model")
            qa_intent_router.route = lambda *a, **k: fake
            result = qa_intent_gate.route_gated("how many are open")
            self.assertIsNone(result)
        finally:
            qa_intent_gate.TRUSTED_TIERS = original_trusted
            qa_intent_router.route = original_route

    def test_trusted_tier_above_threshold_passes(self):
        original_trusted = qa_intent_gate.TRUSTED_TIERS
        original_route = qa_intent_router.route
        try:
            qa_intent_gate.TRUSTED_TIERS = {"a-trusted-tier"}
            fake = qa_intent_router.RoutedIntent("open_count", 0.95, tier="a-trusted-tier:test-model")
            qa_intent_router.route = lambda *a, **k: fake
            result = qa_intent_gate.route_gated("how many are open")
            self.assertEqual(result, "how many are open")
        finally:
            qa_intent_gate.TRUSTED_TIERS = original_trusted
            qa_intent_router.route = original_route

    def test_trusted_tier_below_threshold_still_held(self):
        original_trusted = qa_intent_gate.TRUSTED_TIERS
        original_route = qa_intent_router.route
        try:
            qa_intent_gate.TRUSTED_TIERS = {"a-trusted-tier"}
            fake = qa_intent_router.RoutedIntent("open_count", 0.5, tier="a-trusted-tier:test-model")
            qa_intent_router.route = lambda *a, **k: fake
            result = qa_intent_gate.route_gated("how many are open")
            self.assertIsNone(result)
        finally:
            qa_intent_gate.TRUSTED_TIERS = original_trusted
            qa_intent_router.route = original_route

    def test_unreachable_ollama_returns_none(self):
        original_route = qa_intent_router.route
        try:
            qa_intent_router.route = lambda *a, **k: None
            result = qa_intent_gate.route_gated("how many are open")
            self.assertIsNone(result)
        finally:
            qa_intent_router.route = original_route

    def test_unknown_intent_never_passes_regardless_of_tier(self):
        """"unknown" has no canonical phrasing (to_canonical_question
        returns None for it) -- even a trusted, high-confidence "unknown"
        classification must fall through, not be forced into some
        nearest-match question."""
        original_trusted = qa_intent_gate.TRUSTED_TIERS
        original_route = qa_intent_router.route
        try:
            qa_intent_gate.TRUSTED_TIERS = {"a-trusted-tier"}
            fake = qa_intent_router.RoutedIntent("unknown", 0.99, tier="a-trusted-tier:test-model")
            qa_intent_router.route = lambda *a, **k: fake
            result = qa_intent_gate.route_gated("what is the capital of France")
            self.assertIsNone(result)
        finally:
            qa_intent_gate.TRUSTED_TIERS = original_trusted
            qa_intent_router.route = original_route


@unittest.skipUnless(ollama_is_running(), "Ollama is not running on 127.0.0.1:11434")
class TestRealOllamaIntegration(unittest.TestCase):
    """Only runs if Ollama is actually reachable -- a genuine end-to-end
    call to the local model, skipped (not faked) otherwise."""

    def test_real_call_never_auto_trusts_today(self):
        """Regression guard for the actual empirical finding documented in
        qa_intent_gate.py: TRUSTED_TIERS is empty today because the real
        model this project runs was shown unreliable for this task, so a
        real call must always come back held, whatever it classifies."""
        result = qa_intent_gate.route_gated("how many are open")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
