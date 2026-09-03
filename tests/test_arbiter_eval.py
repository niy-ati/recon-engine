"""
Unit tests for arbiter_eval.py. The stats/report logic is tested against
faked ArbiterResult objects (deterministic, no model call) -- the same
split test_validation_gate.py already uses between its own logic tests
and its @unittest.skipUnless(ollama_is_running()) live integration test.
"""
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import arbiter_eval  # noqa: E402
from arbiter_eval import EvalCase, AMBIGUOUS, run_eval, render_report  # noqa: E402
from llm_matcher import ArbiterResult  # noqa: E402


def ollama_is_running():
    try:
        urllib.request.urlopen("http://127.0.0.1:11434", timeout=2)
        return True
    except Exception:
        return False


def fake_arbiter(responses):
    """responses: {narration: ArbiterResult} -- call_llm_arbiter patched to
    return the canned result for each case's own narration, so a test can
    assert on run_eval()'s scoring logic without a real model call."""
    def _call(narration, shortlist):
        return responses[narration]
    return _call


class TestRunEvalScoring(unittest.TestCase):
    def test_correct_and_wrong_cases_scored_against_expected(self):
        cases = [
            EvalCase("case_a", "narration a", ["order_1", "order_2"], "order_1", "test"),
            EvalCase("case_b", "narration b", ["order_3", "order_4"], "order_3", "test"),
        ]
        responses = {
            "narration a": ArbiterResult("order_1", 0.95, "picked 1", False, tier="ollama:test"),
            "narration b": ArbiterResult("order_4", 0.80, "picked 4", False, tier="ollama:test"),
        }
        with patch("arbiter_eval.call_llm_arbiter", side_effect=fake_arbiter(responses)):
            report = run_eval(cases)

        self.assertEqual(report["scored_case_count"], 2)
        self.assertEqual(report["accuracy_pct"], 50.0)
        self.assertEqual(report["mean_confidence_when_correct"], 0.95)
        self.assertEqual(report["mean_confidence_when_wrong"], 0.80)
        self.assertEqual(report["ambiguous_case_count"], 0)

    def test_ambiguous_case_is_never_scored_as_correct_or_wrong(self):
        """A case with no real ground truth (AMBIGUOUS) must be excluded
        from accuracy entirely -- there is no candidate that would make it
        "correct," so folding it into the accuracy denominator would be a
        fabricated signal, not a real one."""
        cases = [EvalCase("case_a", "narration a", ["order_1", "order_2"], AMBIGUOUS, "test")]
        responses = {"narration a": ArbiterResult("order_1", 0.99, "picked 1", False, tier="ollama:test")}
        with patch("arbiter_eval.call_llm_arbiter", side_effect=fake_arbiter(responses)):
            report = run_eval(cases)

        self.assertEqual(report["scored_case_count"], 0)
        self.assertIsNone(report["accuracy_pct"])
        self.assertEqual(report["ambiguous_case_count"], 1)

    def test_flags_high_confidence_with_no_real_signal(self):
        """The specific failure this harness exists to catch: an ambiguous
        case (no real answer to be right about) answered with >=90%
        confidence anyway -- the positional-bias finding
        validation_gate.AUTO_APPLY_TRUSTED_TIERS is empty because of."""
        cases = [EvalCase("case_a", "narration a", ["order_1", "order_2"], AMBIGUOUS, "test")]
        responses = {"narration a": ArbiterResult("order_1", 0.95, "picked 1", False, tier="ollama:test")}
        with patch("arbiter_eval.call_llm_arbiter", side_effect=fake_arbiter(responses)):
            report = run_eval(cases)
        self.assertEqual(report["overconfident_on_no_signal_count"], 1)

    def test_low_confidence_ambiguous_case_is_not_flagged(self):
        cases = [EvalCase("case_a", "narration a", ["order_1", "order_2"], AMBIGUOUS, "test")]
        responses = {"narration a": ArbiterResult("order_1", 0.4, "unsure", False, tier="ollama:test")}
        with patch("arbiter_eval.call_llm_arbiter", side_effect=fake_arbiter(responses)):
            report = run_eval(cases)
        self.assertEqual(report["overconfident_on_no_signal_count"], 0)

    def test_stand_in_fallback_is_counted_not_silently_averaged_in(self):
        """When Ollama isn't reachable, llm_matcher.call_llm_arbiter falls
        back to the deterministic stand-in (tier="stand-in") -- this must
        be visibly flagged, not silently folded into an accuracy number
        that would misrepresent it as a real model evaluation."""
        cases = [EvalCase("case_a", "narration a", ["order_1", "order_2"], "order_1", "test")]
        responses = {"narration a": ArbiterResult("order_1", 0.72, "stand-in guess", False, tier="stand-in")}
        with patch("arbiter_eval.call_llm_arbiter", side_effect=fake_arbiter(responses)):
            report = run_eval(cases)
        self.assertEqual(report["stand_in_fallback_count"], 1)

    def test_no_scored_cases_does_not_divide_by_zero(self):
        cases = [EvalCase("case_a", "narration a", ["order_1", "order_2"], AMBIGUOUS, "test")]
        responses = {"narration a": ArbiterResult("order_1", 0.5, "unsure", False, tier="ollama:test")}
        with patch("arbiter_eval.call_llm_arbiter", side_effect=fake_arbiter(responses)):
            report = run_eval(cases)  # must not raise ZeroDivisionError
        self.assertIsNone(report["mean_confidence_when_correct"])
        self.assertIsNone(report["mean_confidence_when_wrong"])


class TestRenderReport(unittest.TestCase):
    def test_report_mentions_stand_in_fallback_warning(self):
        cases = [EvalCase("case_a", "narration a", ["order_1", "order_2"], "order_1", "test")]
        responses = {"narration a": ArbiterResult("order_1", 0.72, "stand-in guess", False, tier="stand-in")}
        with patch("arbiter_eval.call_llm_arbiter", side_effect=fake_arbiter(responses)):
            report = run_eval(cases)
        markdown = render_report(report)
        self.assertIn("NOT a real model evaluation", markdown)

    def test_report_includes_every_case_id(self):
        cases = [
            EvalCase("case_a", "narration a", ["order_1", "order_2"], "order_1", "test"),
            EvalCase("case_b", "narration b", ["order_3"], AMBIGUOUS, "test"),
        ]
        responses = {
            "narration a": ArbiterResult("order_1", 0.9, "r1", False, tier="ollama:test"),
            "narration b": ArbiterResult("order_3", 0.9, "r2", False, tier="ollama:test"),
        }
        with patch("arbiter_eval.call_llm_arbiter", side_effect=fake_arbiter(responses)):
            report = run_eval(cases)
        markdown = render_report(report)
        self.assertIn("case_a", markdown)
        self.assertIn("case_b", markdown)


@unittest.skipUnless(ollama_is_running(), "Ollama is not running on 127.0.0.1:11434")
class TestRealOllamaIntegration(unittest.TestCase):
    """Only runs if Ollama is actually reachable -- a genuine end-to-end
    run of the real harness against the real local model, skipped (not
    faked) otherwise, same discipline as test_validation_gate.py's own
    TestRealOllamaIntegration."""

    def test_default_cases_run_without_error_and_score_at_least_one(self):
        report = run_eval()
        self.assertEqual(report["stand_in_fallback_count"], 0)
        self.assertGreater(report["scored_case_count"], 0)


if __name__ == "__main__":
    unittest.main()
