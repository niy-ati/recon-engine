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


class TestRenderPassBar(unittest.TestCase):
    """Regression test for a real bug found live: this bar's middle bucket
    was labeled "AI-assisted", the exact same word the Records page's own
    status filter uses for a narrower, different thing -- literally only
    the MATCHED_AI_ASSISTED status (which stays at 0 rows today, since
    AUTO_APPLY_TRUSTED_TIERS is empty by design). This bucket also folds
    in MATCHED_LOW_CONFIDENCE, so the bar showed a nonzero percentage
    while filtering Records by "AI-assisted" showed zero rows -- looked
    like a contradiction, not two different scopes sharing a label."""

    def test_bucket_is_labeled_ai_touched_not_ai_assisted(self):
        rows = [{"status": "MATCHED"}, {"status": "MATCHED_LOW_CONFIDENCE"}, {"status": "EXCEPTION"}]
        html = review_server.render_pass_bar(rows)
        self.assertIn("AI-touched", html)
        self.assertNotIn("AI-assisted", html)

    def test_bucket_includes_both_low_confidence_and_auto_applied(self):
        rows = (
            [{"status": "MATCHED"}] * 5
            + [{"status": "MATCHED_LOW_CONFIDENCE"}] * 2
            + [{"status": "MATCHED_AI_ASSISTED"}] * 1
        )
        html = review_server.render_pass_bar(rows)
        self.assertIn("<b>38%</b>", html)  # 3 of 8 rows, matching the actual filterable statuses combined


def make_row(status, category=None, resolution_status="OPEN", needs_action=None, **overrides):
    row = {
        "id": 1, "order_id": "order_1", "settlement_id": "setl_1", "net_amount": 100.0,
        "status": status, "category": category, "reason": "test reason", "narration": "",
        "replay_log": "[]", "resolution_status": resolution_status,
        "resolution_note": None,
        "needs_action": needs_action if needs_action is not None else ("yes" if status in ("EXCEPTION", "MATCHED_LOW_CONFIDENCE") else "no"),
    }
    row.update(overrides)
    return row


class TestRenderRowResolutionColumn(unittest.TestCase):
    """Regression tests for a real bug found live: the Records page (which
    calls render_row with show_actions=False) rendered resolution_status
    verbatim -- and that field defaults to OPEN for every row, changing
    only when a human clicks Confirm/Reject in the Queue. A clean MATCHED
    row never appears in the Queue at all (needs_action is "no" for it),
    so nobody ever acts on it and it stays OPEN forever -- identically to
    a genuine FUZZY_MATCH_NEEDS_REVIEW row still awaiting a decision. Both
    showed the bare word "OPEN", making an already-resolved row and a row
    genuinely needing review look the same in this column."""

    def test_clean_match_shows_auto_resolved_not_open(self):
        row = make_row("MATCHED")
        html = review_server.render_row(row, show_actions=False)
        self.assertIn("Auto-resolved", html)
        self.assertNotIn(">OPEN<", html)

    def test_genuine_pending_exception_shows_awaiting_decision_not_bare_open(self):
        row = make_row("EXCEPTION", category="FUZZY_MATCH_NEEDS_REVIEW")
        html = review_server.render_row(row, show_actions=False)
        self.assertIn("Awaiting decision", html)
        self.assertNotIn(">OPEN<", html)

    def test_resolution_column_wording_differs_from_status_columns_needs_review(self):
        """MATCHED_LOW_CONFIDENCE's own Status-column label is also
        "Needs review" (STATUS_LABELS) -- a different axis (the
        pipeline's classification, not whether a human has acted). Using
        the same words in both columns on the same row would read as a
        duplicate rather than two distinct facts, so the Resolution
        column must not reuse that exact phrase."""
        row = make_row("MATCHED_LOW_CONFIDENCE", category="FUZZY_MATCH_NEEDS_REVIEW")
        html = review_server.render_row(row, show_actions=False)
        self.assertIn("Awaiting decision", html)
        self.assertEqual(html.count("Needs review"), 1)  # only the Status column's own label

    def test_auto_resolved_and_awaiting_decision_are_visually_distinct(self):
        """The actual regression: these two rows used to render an
        identical "OPEN" pill despite being in completely different
        states. They must not collide."""
        clean = review_server.render_row(make_row("MATCHED"), show_actions=False)
        pending = review_server.render_row(
            make_row("EXCEPTION", category="FUZZY_MATCH_NEEDS_REVIEW"), show_actions=False
        )
        self.assertNotEqual(clean, pending)
        self.assertIn("pill positive", clean)
        self.assertIn("pill notice", pending)

    def test_confirmed_row_still_shows_confirmed(self):
        row = make_row("EXCEPTION", category="DUPLICATE", resolution_status="CONFIRMED", needs_action="yes")
        html = review_server.render_row(row, show_actions=False)
        self.assertIn("CONFIRMED", html)
        self.assertIn("pill positive", html)

    def test_rejected_row_still_shows_rejected(self):
        row = make_row("EXCEPTION", category="UNEXPLAINED", resolution_status="REJECTED", needs_action="yes")
        html = review_server.render_row(row, show_actions=False)
        self.assertIn("REJECTED", html)
        self.assertIn("pill negative", html)


class TestReadableCategory(unittest.TestCase):
    """Regression tests for a real UI complaint: category cards showed the
    raw enum value (FUZZY_MATCH_NEEDS_REVIEW, ON_HOLD_BY_RAZORPAY) verbatim
    instead of a readable label."""

    def test_every_real_category_has_a_hand_written_label(self):
        for cat in review_server.CATEGORY_TONES:
            self.assertIn(cat, review_server.CATEGORY_LABELS, f"no CATEGORY_LABELS entry for {cat}")

    def test_acronyms_stay_uppercase_not_title_cased(self):
        """A generic underscore-to-title-case transform would produce "Utr
        Mismatch" and "Afa Mandate Hold" -- real acronyms, wrong case."""
        self.assertEqual(review_server.readable_category("UTR_LEVEL_MISMATCH"), "UTR mismatch")
        self.assertEqual(review_server.readable_category("AFA_MANDATE_HOLD"), "AFA mandate hold")

    def test_multi_word_category_reads_as_a_sentence_not_shouted_caps(self):
        self.assertEqual(review_server.readable_category("FUZZY_MATCH_NEEDS_REVIEW"), "Needs manual review")
        self.assertEqual(review_server.readable_category("ON_HOLD_BY_RAZORPAY"), "On hold by Razorpay")

    def test_unlisted_category_degrades_to_title_case_not_a_crash(self):
        """A category added later without a CATEGORY_LABELS entry must
        still render something readable, not raise or show raw enum text
        forever until someone remembers to add it here."""
        self.assertEqual(review_server.readable_category("SOME_NEW_CATEGORY"), "Some New Category")

    def test_output_is_html_escaped(self):
        result = review_server.readable_category("<script>DUPLICATE")
        self.assertNotIn("<script>", result)


if __name__ == "__main__":
    unittest.main()
