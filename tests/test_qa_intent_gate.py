"""
Unit tests for qa_intent_gate.py. Mirrors test_validation_gate.py's own
structure: fake results test the gate's logic in isolation, and a separate
class runs a genuine end-to-end call against Ollama if it's reachable,
skipped otherwise.
"""
import sys
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

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
        """Non-empty TRUSTED_TIERS that simply doesn't include the
        reporting tier -- distinct from the empty-set case below, which
        short-circuits before ever checking tier membership at all. This
        one must still reach and fail that check."""
        original_trusted = qa_intent_gate.TRUSTED_TIERS
        original_route = qa_intent_router.route
        try:
            qa_intent_gate.TRUSTED_TIERS = {"some-other-tier"}
            fake = qa_intent_router.RoutedIntent("open_count", 1.0, tier="ollama:test-model")
            qa_intent_router.route = lambda *a, **k: fake
            result = qa_intent_gate.route_gated("how many are open")
            self.assertIsNone(result)
        finally:
            qa_intent_gate.TRUSTED_TIERS = original_trusted
            qa_intent_router.route = original_route

    def test_empty_trusted_tiers_skips_the_network_call_entirely(self):
        """Regression test for a real latency bug found live: with
        TRUSTED_TIERS empty, no result from qa_intent_router.route() could
        ever pass the check below it, so calling it at all was pure
        wasted time -- 2-5s warm, up to ~80s cold -- on every question
        that missed the deterministic keyword match. Confirmed here by
        asserting route() is never even invoked, not just that the
        result comes back None."""
        original_trusted = qa_intent_gate.TRUSTED_TIERS
        original_route = qa_intent_router.route
        try:
            qa_intent_gate.TRUSTED_TIERS = set()
            mock_route = MagicMock()
            qa_intent_router.route = mock_route
            result = qa_intent_gate.route_gated("how many are open")
            self.assertIsNone(result)
            mock_route.assert_not_called()
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

    def test_empty_trusted_tiers_never_reaches_ollama_even_when_reachable(self):
        """The default, shipped state: even with Ollama actually running
        right now, route_gated must never call it at all while
        TRUSTED_TIERS is empty. Timed against the real model, not just
        asserted logically -- a future change that accidentally dropped
        the short-circuit would show up here as an unexpectedly slow
        test, not only as a failed assertion."""
        started = time.time()
        result = qa_intent_gate.route_gated("how many are open")
        elapsed = time.time() - started
        self.assertIsNone(result)
        self.assertLess(elapsed, 1.0, "route_gated took over a second with TRUSTED_TIERS empty -- did it call Ollama anyway?")

    def test_real_call_through_a_trusted_tier(self):
        """Temporarily trusts the real 'ollama' tier to exercise a
        genuine, live round trip through the gate end to end -- distinct
        from the short-circuit test above, which never reaches Ollama at
        all. Restores the real, empty trusted set afterward regardless of
        outcome. Either a canonical string or None is a legitimate result
        here (the model's own confidence still gates it); the point of
        this test is only that a live call through the gate completes
        without error."""
        original_trusted = qa_intent_gate.TRUSTED_TIERS
        try:
            qa_intent_gate.TRUSTED_TIERS = {"ollama"}
            result = qa_intent_gate.route_gated("how many are open")
            self.assertTrue(result is None or isinstance(result, str))
        finally:
            qa_intent_gate.TRUSTED_TIERS = original_trusted


if __name__ == "__main__":
    unittest.main()
