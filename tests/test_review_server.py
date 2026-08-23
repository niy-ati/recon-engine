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


class TestRenderDonut(unittest.TestCase):
    def test_resolved_pct_excludes_matched_low_confidence(self):
        """Regression test for a real bug: the donut's headline resolved_pct
        (the single most visible number on the Overview page) used to
        exclude only EXCEPTION status, silently counting an unconfirmed
        MATCHED_LOW_CONFIDENCE candidate as resolved. db.py's own
        needs_action rule treats it like EXCEPTION -- this must too."""
        rows = [
            {"status": "MATCHED"},
            {"status": "MATCHED"},
            {"status": "MATCHED_LOW_CONFIDENCE"},
            {"status": "EXCEPTION"},
        ]
        html = review_server.render_donut(rows)
        self.assertIn("<b>50.0%</b>", html)  # 2 of 4 rows genuinely resolved, not 3 of 4


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

    def test_matched_low_confidence_is_pending_review_not_resolved(self):
        """Regression test for a real bug: MATCHED_LOW_CONFIDENCE (an
        unconfirmed arbiter candidate) was folded into "resolved" here,
        same bug as reconcile.py's summarize() -- fixed in both places,
        kept in sync. db.py's own needs_action rule already treats this
        status like EXCEPTION, not like done."""
        rows = [self._row(300.0, "FUZZY_MATCH_NEEDS_REVIEW", "MATCHED_LOW_CONFIDENCE")]
        result = review_server.compute_cash_clarity(rows)
        self.assertEqual(result["resolved"], 0.0)
        self.assertEqual(result["pending_review"], 300.0)
        self.assertEqual(result["at_risk"], 300.0)

    def test_duplicate_excluded_from_every_cash_bucket(self):
        """Regression test: a DUPLICATE row's net amount already cleared
        under its sibling row -- counting it separately double-counts
        money that isn't actually at risk."""
        rows = [
            self._row(999.0, "DUPLICATE", "EXCEPTION"),
            self._row(200.0, "UNEXPLAINED", "EXCEPTION"),
        ]
        result = review_server.compute_cash_clarity(rows)
        self.assertEqual(result["at_risk"], 200.0)
        self.assertEqual(result["still_open"], 200.0)
        self.assertEqual(result["resolved"], 0.0)
        self.assertEqual(result["pending_review"], 0.0)


if __name__ == "__main__":
    unittest.main()
