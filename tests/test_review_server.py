"""
Unit tests for review_server.py's log-entry rendering -- confirms both the
current structured format and pre-migration plain-string entries render
without crashing.
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import unittest  # noqa: E402
import review_server  # noqa: E402


class TestRenderLogEntry(unittest.TestCase):
    def test_structured_entry_renders_pass_action_detail(self):
        entry = {"pass": "1", "action": "matched", "detail": "settlement<->bank matched",
                  "confidence": None, "timestamp": "2026-08-23T00:00:00Z", "correlation_id": "run-1"}
        html = review_server.render_log_entry(entry)
        self.assertIn("pass 1", html)
        self.assertIn("matched", html)
        self.assertIn("settlement", html)

    def test_structured_entry_with_confidence_shows_it(self):
        entry = {"pass": "3/4", "action": "arbiter_picked", "detail": "picked order_1",
                  "confidence": 0.87, "timestamp": "2026-08-23T00:00:00Z", "correlation_id": "run-1"}
        html = review_server.render_log_entry(entry)
        self.assertIn("0.87", html)

    def test_legacy_plain_string_entry_still_renders(self):
        html = review_server.render_log_entry("PASS1: settlement<->bank matched on UTR+amount+date")
        self.assertIn("settlement", html)

    def test_output_is_html_escaped(self):
        entry = {"pass": "1", "action": "matched", "detail": "a <script>alert(1)</script> b",
                  "confidence": None, "timestamp": "x", "correlation_id": "run-1"}
        html = review_server.render_log_entry(entry)
        self.assertNotIn("<script>", html)


class TestSummarizeReplay(unittest.TestCase):
    def test_builds_capitalized_semicolon_joined_line_from_details(self):
        replay_log = [
            {"pass": "1", "action": "matched", "detail": "settlement<->bank matched on UTR+amount+date",
             "confidence": None, "timestamp": "x", "correlation_id": "run-1"},
            {"pass": "2", "action": "matched", "detail": "settlement<->ledger matched on order_id",
             "confidence": None, "timestamp": "x", "correlation_id": "run-1"},
        ]
        summary = review_server.summarize_replay(replay_log)
        self.assertEqual(
            summary,
            "Settlement<->bank matched on UTR+amount+date; Settlement<->ledger matched on order_id",
        )

    def test_empty_replay_log_returns_empty_string(self):
        self.assertEqual(review_server.summarize_replay([]), "")

    def test_entries_with_no_detail_return_empty_string(self):
        replay_log = [{"pass": "1", "action": "matched", "detail": "", "confidence": None,
                        "timestamp": "x", "correlation_id": "run-1"}]
        self.assertEqual(review_server.summarize_replay(replay_log), "")

    def test_legacy_plain_string_entries_are_ignored(self):
        replay_log = ["PASS1: settlement<->bank matched on UTR+amount+date"]
        self.assertEqual(review_server.summarize_replay(replay_log), "")


class TestComputeCashClarity(unittest.TestCase):
    def _row(self, net_amount, category, status):
        return {"net_amount": net_amount, "category": category, "status": status}

    def test_splits_resolved_from_still_open(self):
        rows = [
            self._row(975.42, "ROUNDING", "MATCHED_WITH_VARIANCE"),
            self._row(500.00, "UNEXPLAINED", "EXCEPTION"),
            self._row(200.00, None, "MATCHED"),  # no category -- outside the at-risk universe entirely
        ]
        result = review_server.compute_cash_clarity(rows)
        self.assertAlmostEqual(result["at_risk"], 1475.42, places=2)
        self.assertAlmostEqual(result["resolved"], 975.42, places=2)
        self.assertAlmostEqual(result["still_open"], 500.00, places=2)
        self.assertAlmostEqual(result["resolved_pct"], round(100 * 975.42 / 1475.42, 1), places=1)

    def test_no_categorized_rows_returns_zero_without_dividing_by_zero(self):
        rows = [self._row(100.0, None, "MATCHED")]
        result = review_server.compute_cash_clarity(rows)
        self.assertEqual(result["at_risk"], 0)
        self.assertEqual(result["resolved_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()
