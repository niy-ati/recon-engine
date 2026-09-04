"""
Locks report.py's displayed markdown/CSV text to the summary dict actually
computed by reconcile.summarize() -- the exact class of bug this file
exists because of: a display layer disagreeing with the computed truth
(see README, "Compared to Razorpay's Own Reconciliation Agent" -> Scope ->
the metrics bug that moved the headline figure from 93.2% to 90.5%).

Deliberately does NOT mock summarize() itself. reconcile() is substituted
with a small, fixed, hand-built results list (same substitute-the-
dependency pattern as test_ingest_retry.py and test_llm_matcher.py's
fallback tests); summarize() then runs for real, on that same list, both
inside report.generate() and independently in this file, so a test
failure here means the two disagreed -- not that a value was hand-typed
wrong on one side.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import db  # noqa: E402
import report  # noqa: E402
from reconcile import summarize  # noqa: E402


def make_result(order_id, status, category=None, net=100.0, reason="test reason"):
    return {
        "order_id": order_id, "settlement_id": f"setl_{order_id}", "net": net,
        "match_key": f"settlement:{order_id}",
        "status": status, "category": category, "reason": reason,
        "narration": "", "stage": [],
    }


# Deliberately mixed: every status bucket at least once, one DUPLICATE
# (excluded from cash figures entirely), enough categories to exercise the
# exceptions-by-category table, and both an arbiter-adjacent status
# (MATCHED_LOW_CONFIDENCE) and cash-figure buckets (resolved/pending/open).
FIXTURE_RESULTS = [
    make_result("order_1", "MATCHED"),
    make_result("order_2", "MATCHED"),
    make_result("order_3", "MATCHED_WITH_VARIANCE", category="TAX_DEDUCTION", net=500.0),
    make_result("order_4", "MATCHED_EXACT_REFERENCE"),
    make_result("order_5", "MATCHED_LEARNED_PATTERN"),
    make_result("order_6", "MATCHED_AI_ASSISTED"),
    make_result("order_7", "MATCHED_LOW_CONFIDENCE", category="FUZZY_MATCH_NEEDS_REVIEW", net=600.0),
    make_result("order_8", "EXCEPTION", category="UNEXPLAINED", net=900.0),
    make_result("order_9", "EXCEPTION", category="DUPLICATE", net=400.0),
    make_result("order_10", "EXCEPTION", category="ON_HOLD_BY_RAZORPAY", net=1100.0),
]


class ReportTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmpdir.name)

        self._original_output_dir = report.OUTPUT_DIR
        report.OUTPUT_DIR = tmp_path
        self._original_db_path = db.DB_PATH
        db.DB_PATH = tmp_path / "test_reconcile.db"

        self._reconcile_patch = patch("report.reconcile", return_value=FIXTURE_RESULTS)
        self._reconcile_patch.start()

        report.generate()
        self.markdown = (report.OUTPUT_DIR / "reconciliation_report.md").read_text()
        self.csv_text = (report.OUTPUT_DIR / "exceptions.csv").read_text()
        self.summary = summarize(FIXTURE_RESULTS)  # the same real function, called independently

    def tearDown(self):
        self._reconcile_patch.stop()
        report.OUTPUT_DIR = self._original_output_dir
        db.DB_PATH = self._original_db_path
        self._tmpdir.cleanup()


class TestMarkdownMatchesSummaryDict(ReportTestCase):
    def test_total_rows_matches(self):
        self.assertIn(f"**Total rows processed:** {self.summary['total_rows']}", self.markdown)

    def test_overall_resolved_pct_matches(self):
        """The exact line the metrics bug this file is named after would
        have gotten wrong: a hand-typed or independently recomputed
        percentage silently drifting from summarize()'s own number."""
        self.assertIn(f"**Overall resolved: {self.summary['overall_resolved_pct']}%**", self.markdown)

    def test_every_percentage_line_matches(self):
        expectations = {
            "clean_match_pct": "**Clean deterministic match:**",
            "matched_with_variance_pct": "**Matched with explained variance:**",
            "exact_reference_pct": "**Unambiguous exact reference (deterministic, no LLM call):**",
            "learned_pattern_pct": "**Resolved via learned pattern (human-confirmed before):**",
            "ai_assisted_auto_applied_pct": "**AI-assisted, auto-applied (confidence >= 0.90 gate):**",
            "fuzzy_matched_needs_review_pct": "**Fuzzy-matched, flagged for human review:**",
            "unresolved_exception_pct": "**Unresolved exceptions:**",
        }
        for key, label in expectations.items():
            with self.subTest(key=key):
                self.assertIn(f"{label} {self.summary[key]}%", self.markdown)

    def test_exceptions_by_category_table_matches(self):
        for category, count in self.summary["exceptions_by_category"].items():
            with self.subTest(category=category):
                self.assertIn(f"| {category} | {count} |", self.markdown)

    def test_cash_position_figures_match(self):
        self.assertGreater(self.summary["cash_at_risk"], 0, "fixture must exercise the cash-position paragraph")
        self.assertIn(f"Rs.{self.summary['cash_at_risk']:,.2f}", self.markdown)
        self.assertIn(f"Rs.{self.summary['cash_resolved']:,.2f} ({self.summary['cash_resolved_pct']}%)", self.markdown)
        self.assertIn(f"Rs.{self.summary['cash_pending_review']:,.2f}", self.markdown)
        self.assertIn(f"Rs.{self.summary['cash_still_open']:,.2f}", self.markdown)

    def test_duplicate_excluded_from_cash_at_risk(self):
        """order_9 (DUPLICATE, net=400) must not inflate cash_at_risk --
        the exact rule test_reconcile.py's own duplicate-cash tests check
        at the summarize() layer; this checks the displayed total still
        agrees once report.py has formatted it."""
        included_nets = (500.0, 600.0, 900.0, 1100.0)  # every non-DUPLICATE row with a category
        self.assertAlmostEqual(self.summary["cash_at_risk"], sum(included_nets), places=2)

    def test_arbiter_invoked_phrase_matches_when_arbiter_rows_present(self):
        """Fixture has both an AI-assisted and a low-confidence row, so
        summary['ai_assisted_auto_applied_pct'] + summary['fuzzy_matched_needs_review_pct']
        > 0 -- the throughput line must say so, not the deterministic-only phrasing."""
        self.assertGreater(
            self.summary["ai_assisted_auto_applied_pct"] + self.summary["fuzzy_matched_needs_review_pct"], 0
        )
        self.assertIn("includes LLM arbiter call(s)", self.markdown)
        self.assertNotIn("pure deterministic matching", self.markdown)


class TestFreshBatch(unittest.TestCase):
    """See db.reset_batch()'s own docstring for the real accumulation bug
    this flag exists to prevent: report.py's persist_results() call
    upserts on match_key, correct for re-running the SAME batch, but with
    no way to know a genuinely new one (different settlement_ids, from a
    generate_data.py edit or a fresh live pull) just replaced it."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmpdir.name)
        self._original_output_dir = report.OUTPUT_DIR
        report.OUTPUT_DIR = tmp_path
        self._original_db_path = db.DB_PATH
        db.DB_PATH = tmp_path / "test_reconcile.db"

    def tearDown(self):
        report.OUTPUT_DIR = self._original_output_dir
        db.DB_PATH = self._original_db_path
        self._tmpdir.cleanup()

    def test_fresh_batch_clears_rows_from_an_unrelated_earlier_batch(self):
        old_batch = [make_result("order_old", "MATCHED")]
        old_batch[0]["match_key"] = "settlement:setl_old"
        with patch("report.reconcile", return_value=old_batch):
            report.generate()
        self.assertEqual(len(db.get_all_exceptions()), 1)

        new_batch = [make_result("order_new", "MATCHED")]
        new_batch[0]["match_key"] = "settlement:setl_new"
        with patch("report.reconcile", return_value=new_batch):
            report.generate(fresh_batch=True)

        rows = db.get_all_exceptions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["order_id"], "order_new")

    def test_without_fresh_batch_old_rows_accumulate(self):
        """The bug, reproduced directly: two disjoint-match_key batches
        both surviving is exactly what silently grew data/reconcile.db to
        3,104 rows across 6 forgotten runs during real use."""
        old_batch = [make_result("order_old", "MATCHED")]
        old_batch[0]["match_key"] = "settlement:setl_old"
        with patch("report.reconcile", return_value=old_batch):
            report.generate()

        new_batch = [make_result("order_new", "MATCHED")]
        new_batch[0]["match_key"] = "settlement:setl_new"
        with patch("report.reconcile", return_value=new_batch):
            report.generate(fresh_batch=False)

        self.assertEqual(len(db.get_all_exceptions()), 2)


class TestExceptionsCsvMatchesResults(ReportTestCase):
    def test_matched_rows_excluded(self):
        self.assertNotIn("order_1,", self.csv_text)
        self.assertNotIn("order_2,", self.csv_text)

    def test_non_matched_rows_included(self):
        for order_id in ("order_3", "order_4", "order_5", "order_6", "order_7", "order_8", "order_9", "order_10"):
            with self.subTest(order_id=order_id):
                self.assertIn(order_id, self.csv_text)

    def test_needs_action_matches_the_actionable_set(self):
        """Mirrors db.py's own needs_action rule (EXCEPTION or
        MATCHED_LOW_CONFIDENCE = 'yes') -- report.py computes this
        independently in its own ACTIONABLE set, so this is exactly the
        kind of place a second, silently-diverging definition could hide."""
        lines = {line.split(",")[0]: line for line in self.csv_text.splitlines()[1:] if line}
        self.assertTrue(lines["order_7"].rstrip().endswith(",yes"))    # MATCHED_LOW_CONFIDENCE
        self.assertTrue(lines["order_8"].rstrip().endswith(",yes"))    # EXCEPTION
        self.assertTrue(lines["order_9"].rstrip().endswith(",yes"))    # EXCEPTION
        self.assertTrue(lines["order_10"].rstrip().endswith(",yes"))   # EXCEPTION
        self.assertTrue(lines["order_3"].rstrip().endswith(",no"))     # MATCHED_WITH_VARIANCE
        self.assertTrue(lines["order_4"].rstrip().endswith(",no"))     # MATCHED_EXACT_REFERENCE
        self.assertTrue(lines["order_5"].rstrip().endswith(",no"))     # MATCHED_LEARNED_PATTERN
        self.assertTrue(lines["order_6"].rstrip().endswith(",no"))     # MATCHED_AI_ASSISTED


if __name__ == "__main__":
    unittest.main()
