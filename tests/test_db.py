"""
Unit tests for db.py's SQLite persistence layer. Each test points db.DB_PATH
at a fresh temporary file so tests never touch the real data/reconcile.db.
"""
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import db  # noqa: E402


def make_result(order_id, status, category=None, narration="", net=100.0, match_key=None,
                 gross=None, mdr=None, utr=None, settlement_date=None, method=None, dispute_id=None):
    return {
        "order_id": order_id, "settlement_id": f"setl_{order_id}", "net": net,
        "gross": gross, "mdr": mdr, "utr": utr, "settlement_date": settlement_date,
        "method": method, "dispute_id": dispute_id,
        "match_key": match_key or f"settlement:setl_{order_id}",
        "status": status, "category": category, "reason": "test reason",
        "narration": narration, "stage": ["test stage"],
    }


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = Path(self._tmpdir.name) / "test_reconcile.db"

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        self._tmpdir.cleanup()


class TestPersistResults(DbTestCase):
    def test_persist_and_read_back(self):
        results = [
            make_result("order_1", "MATCHED"),
            make_result("order_2", "EXCEPTION", category="UNEXPLAINED"),
        ]
        db.persist_results(results, run_id="run-1")
        all_rows = db.get_all_exceptions()
        self.assertEqual(len(all_rows), 2)
        open_rows = db.get_open_exceptions()
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(open_rows[0]["order_id"], "order_2")

    def test_rerunning_same_match_key_updates_in_place_not_duplicates(self):
        """Regression test: an earlier version deleted the whole table on
        every run (wiping human decisions); a version before that deleted
        only same-run_id rows (never matched anything, so re-runs
        duplicated instead). Neither happens now -- same match_key across
        two runs is the same row, always."""
        db.persist_results([make_result("order_1", "EXCEPTION", category="UNEXPLAINED")], run_id="run-1")
        db.persist_results([make_result("order_1", "EXCEPTION", category="UNEXPLAINED")], run_id="run-2")
        all_rows = db.get_all_exceptions()
        self.assertEqual(len(all_rows), 1)
        self.assertEqual(all_rows[0]["run_id"], "run-2")

    def test_open_row_is_refreshed_by_a_later_run(self):
        db.persist_results([make_result("order_1", "MATCHED_LOW_CONFIDENCE", category="FUZZY_MATCH_NEEDS_REVIEW")], run_id="run-1")
        db.persist_results([make_result("order_1", "MATCHED_LEARNED_PATTERN")], run_id="run-2")
        rows = db.get_all_exceptions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "MATCHED_LEARNED_PATTERN")

    def test_confirmed_row_is_frozen_against_a_later_run(self):
        """The actual idempotency guarantee: once a human confirms a match,
        a later run recomputing a different result for the same match_key
        must not silently overwrite that decision."""
        db.persist_results([make_result("order_1", "MATCHED_LOW_CONFIDENCE", category="FUZZY_MATCH_NEEDS_REVIEW")], run_id="run-1")
        row_id = db.get_open_exceptions()[0]["id"]
        db.resolve_exception(row_id, "confirm")

        db.persist_results([make_result("order_1", "EXCEPTION", category="UNEXPLAINED")], run_id="run-2")

        rows = db.get_all_exceptions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "MATCHED_LOW_CONFIDENCE")
        self.assertEqual(rows[0]["category"], "FUZZY_MATCH_NEEDS_REVIEW")
        self.assertEqual(rows[0]["resolution_status"], "CONFIRMED")

    def test_rejected_row_is_frozen_against_a_later_run(self):
        db.persist_results([make_result("order_1", "MATCHED_LOW_CONFIDENCE", category="FUZZY_MATCH_NEEDS_REVIEW")], run_id="run-1")
        row_id = db.get_open_exceptions()[0]["id"]
        db.resolve_exception(row_id, "reject")

        db.persist_results([make_result("order_1", "MATCHED_AI_ASSISTED")], run_id="run-2")

        rows = db.get_all_exceptions()
        self.assertEqual(rows[0]["status"], "MATCHED_LOW_CONFIDENCE")
        self.assertEqual(rows[0]["resolution_status"], "REJECTED")

    def test_matched_rows_are_not_actionable(self):
        db.persist_results([make_result("order_1", "MATCHED")], run_id="run-1")
        self.assertEqual(db.get_open_exceptions(), [])
        self.assertEqual(len(db.get_all_exceptions()), 1)

    def test_missing_match_key_raises(self):
        bad_result = make_result("order_1", "EXCEPTION")
        del bad_result["match_key"]
        with self.assertRaises(ValueError):
            db.persist_results([bad_result], run_id="run-1")

    def test_gross_mdr_utr_settlement_date_round_trip(self):
        """Real fields reconcile.py now carries through (see its module
        docstring) so settlement_qa.py's date lookup and "next settlement"
        answers have real data, not just the net figure matching already
        used -- persisted and read back exactly, not silently dropped."""
        db.persist_results(
            [make_result("order_1", "MATCHED", gross=999.0, mdr=19.98, utr="UTR001",
                          settlement_date="2026-08-05")],
            run_id="run-1",
        )
        row = db.get_all_exceptions()[0]
        self.assertEqual(row["gross_amount"], 999.0)
        self.assertEqual(row["mdr_amount"], 19.98)
        self.assertEqual(row["utr"], "UTR001")
        self.assertEqual(row["settlement_date"], "2026-08-05")

    def test_gross_mdr_utr_settlement_date_default_to_none(self):
        """A ledger-only orphan (reconcile.py's final pass) never carries
        these keys at all -- must persist as NULL, not raise a KeyError."""
        db.persist_results([make_result("order_1", "EXCEPTION", category="UNEXPLAINED")], run_id="run-1")
        row = db.get_all_exceptions()[0]
        self.assertIsNone(row["gross_amount"])
        self.assertIsNone(row["mdr_amount"])
        self.assertIsNone(row["utr"])
        self.assertIsNone(row["settlement_date"])

    def test_method_and_dispute_id_round_trip(self):
        """Real fields on Razorpay's own settlement recon line (see
        ingest.py's module docstring), newly threaded through -- method is
        what Razorpay's own 2026 Settlement Transparency merchant playbook
        names as a required field on a reconciliation-ready report;
        dispute_id is what backs the new DISPUTED category."""
        db.persist_results(
            [make_result("order_1", "EXCEPTION", category="DISPUTED", method="UPI", dispute_id="disp_abc123")],
            run_id="run-1",
        )
        row = db.get_all_exceptions()[0]
        self.assertEqual(row["method"], "UPI")
        self.assertEqual(row["dispute_id"], "disp_abc123")

    def test_method_and_dispute_id_default_to_none(self):
        db.persist_results([make_result("order_1", "MATCHED")], run_id="run-1")
        row = db.get_all_exceptions()[0]
        self.assertIsNone(row["method"])
        self.assertIsNone(row["dispute_id"])


class TestResolveException(DbTestCase):
    def test_confirm_closes_the_row(self):
        db.persist_results([make_result("order_1", "EXCEPTION", category="UNEXPLAINED")], run_id="run-1")
        row_id = db.get_open_exceptions()[0]["id"]
        db.resolve_exception(row_id, "confirm")
        self.assertEqual(db.get_open_exceptions(), [])
        all_rows = db.get_all_exceptions()
        self.assertEqual(all_rows[0]["resolution_status"], "CONFIRMED")
        self.assertIsNotNone(all_rows[0]["resolved_at"])

    def test_second_resolve_on_an_already_resolved_row_is_a_no_op(self):
        """review_server.py serves resolve_exception over a ThreadingHTTPServer,
        so a double click or two open tabs can genuinely race two requests for
        the same row. Simulates the losing request arriving after the row is
        already terminal: it must not flip resolution_status/resolution_note/
        resolved_at, and -- the sharper risk -- a stale 'confirm' arriving after
        a real 'reject' must not write a narration_rules entry for a match a
        human actually rejected."""
        db.persist_results(
            [make_result("order_1", "MATCHED_LOW_CONFIDENCE", category="FUZZY_MATCH_NEEDS_REVIEW",
                          narration="pymt rcvd customer ord#1 thx")],
            run_id="run-1",
        )
        row_id = db.get_open_exceptions()[0]["id"]
        db.resolve_exception(row_id, "reject", note="not this order")
        rejected_row = db.get_all_exceptions()[0]

        db.resolve_exception(row_id, "confirm")  # the losing, stale request

        row_after = db.get_all_exceptions()[0]
        self.assertEqual(row_after["resolution_status"], "REJECTED")
        self.assertEqual(row_after["resolution_note"], rejected_row["resolution_note"])
        self.assertEqual(row_after["resolved_at"], rejected_row["resolved_at"])
        self.assertIsNone(db.get_narration_rule("pymt rcvd customer ord#1 thx"))

    def test_confirming_fuzzy_match_writes_narration_rule(self):
        db.persist_results(
            [make_result("order_1", "MATCHED_LOW_CONFIDENCE", category="FUZZY_MATCH_NEEDS_REVIEW",
                          narration="pymt rcvd customer ord#1 thx")],
            run_id="run-1",
        )
        row_id = db.get_open_exceptions()[0]["id"]
        db.resolve_exception(row_id, "confirm")
        rule = db.get_narration_rule("pymt rcvd customer ord#1 thx")
        self.assertIsNotNone(rule)
        self.assertEqual(rule["order_id"], "order_1")
        self.assertEqual(rule["source"], "human_review")

    def test_confirming_fuzzy_match_also_writes_a_narration_template(self):
        """Same confirm as above, checking the generalized side: the
        order's own digit run gets replaced with {REF}, so a differently
        numbered narration from the same recurring template can resolve
        later without a fresh confirm (see test_reconcile.py's
        TestNarrationTemplates for the read side)."""
        db.persist_results(
            [make_result("order_1042", "MATCHED_LOW_CONFIDENCE", category="FUZZY_MATCH_NEEDS_REVIEW",
                          narration="pymt rcvd Alice ord#1042 thx")],
            run_id="run-1",
        )
        row_id = db.get_open_exceptions()[0]["id"]
        db.resolve_exception(row_id, "confirm")
        templates = db.get_narration_templates()
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]["template"], "pymt rcvd Alice ord#{REF} thx")
        self.assertEqual(templates[0]["source"], "human_review")

    def test_rejecting_fuzzy_match_does_not_write_a_narration_template(self):
        db.persist_results(
            [make_result("order_1042", "MATCHED_LOW_CONFIDENCE", category="FUZZY_MATCH_NEEDS_REVIEW",
                          narration="pymt rcvd Alice ord#1042 thx")],
            run_id="run-1",
        )
        row_id = db.get_open_exceptions()[0]["id"]
        db.resolve_exception(row_id, "reject")
        self.assertEqual(db.get_narration_templates(), [])

    def test_non_specific_narration_writes_neither_rule_nor_template(self):
        """A narration that fails the specificity guard (see
        test_confirming_fuzzy_match_with_no_digits_does_not_write_rule)
        must not produce a template either -- the template derivation only
        ever runs after that same guard already passed."""
        db.persist_results(
            [make_result("order_1042", "MATCHED_LOW_CONFIDENCE", category="FUZZY_MATCH_NEEDS_REVIEW",
                          narration="payment received thanks")],
            run_id="run-1",
        )
        row_id = db.get_open_exceptions()[0]["id"]
        db.resolve_exception(row_id, "confirm")
        self.assertEqual(db.get_narration_templates(), [])

    def test_rejecting_fuzzy_match_does_not_write_narration_rule(self):
        db.persist_results(
            [make_result("order_1", "MATCHED_LOW_CONFIDENCE", category="FUZZY_MATCH_NEEDS_REVIEW",
                          narration="pymt rcvd customer ord#1 thx")],
            run_id="run-1",
        )
        row_id = db.get_open_exceptions()[0]["id"]
        db.resolve_exception(row_id, "reject")
        self.assertIsNone(db.get_narration_rule("pymt rcvd customer ord#1 thx"))

    def test_confirming_fuzzy_match_with_no_digits_does_not_write_rule(self):
        """A narration with no digit reference at all could plausibly
        describe any order -- refuses to memoize it rather than let it
        auto-apply to some future unrelated transaction. See README,
        'the learned-pattern store can be poisoned by an overly generic
        confirm.'"""
        db.persist_results(
            [make_result("order_1", "MATCHED_LOW_CONFIDENCE", category="FUZZY_MATCH_NEEDS_REVIEW",
                          narration="payment received thanks")],
            run_id="run-1",
        )
        row_id = db.get_open_exceptions()[0]["id"]
        db.resolve_exception(row_id, "confirm")
        self.assertIsNone(db.get_narration_rule("payment received thanks"))

    def test_confirming_fuzzy_match_with_shared_digit_suffix_does_not_write_rule(self):
        """The narration's digit run matches order_1's own suffix, but
        another order in the same batch (return_1) shares that exact
        suffix -- no longer a unique fingerprint, so it's refused."""
        db.persist_results(
            [
                make_result("order_1", "MATCHED_LOW_CONFIDENCE", category="FUZZY_MATCH_NEEDS_REVIEW",
                            narration="pymt rcvd customer ref 1 thx"),
                make_result("return_1", "EXCEPTION", category="UNEXPLAINED"),
            ],
            run_id="run-1",
        )
        row_id = next(r["id"] for r in db.get_open_exceptions() if r["order_id"] == "order_1")
        db.resolve_exception(row_id, "confirm")
        self.assertIsNone(db.get_narration_rule("pymt rcvd customer ref 1 thx"))

    def test_unknown_action_raises(self):
        db.persist_results([make_result("order_1", "EXCEPTION")], run_id="run-1")
        row_id = db.get_open_exceptions()[0]["id"]
        with self.assertRaises(ValueError):
            db.resolve_exception(row_id, "approve_with_extreme_prejudice")

    def test_resolving_missing_id_raises(self):
        with self.assertRaises(KeyError):
            db.resolve_exception(99999, "confirm")


class TestAddNote(DbTestCase):
    def test_note_does_not_close_the_row(self):
        db.persist_results([make_result("order_1", "EXCEPTION", category="UNEXPLAINED")], run_id="run-1")
        row_id = db.get_open_exceptions()[0]["id"]
        db.add_note(row_id, "waiting on merchant reply")
        open_rows = db.get_open_exceptions()
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(open_rows[0]["resolution_note"], "waiting on merchant reply")


class TestMigration(DbTestCase):
    def test_legacy_table_without_match_key_gets_migrated(self):
        """Simulates a data/reconcile.db created before match_key existed --
        confirms get_connection() backfills it instead of crashing."""
        import sqlite3
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute("""
            CREATE TABLE exceptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL, order_id TEXT, settlement_id TEXT,
                net_amount REAL, status TEXT NOT NULL, category TEXT,
                reason TEXT, narration TEXT, needs_action TEXT NOT NULL,
                replay_log TEXT NOT NULL,
                resolution_status TEXT NOT NULL DEFAULT 'OPEN',
                resolution_note TEXT, resolved_at TEXT
            )
        """)
        conn.execute(
            "INSERT INTO exceptions (run_id, settlement_id, status, needs_action, replay_log) "
            "VALUES ('old-run', 'setl_legacy', 'EXCEPTION', 'yes', '[]')"
        )
        conn.commit()
        conn.close()

        rows = db.get_all_exceptions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["match_key"], "setl_legacy")

        db.persist_results([make_result("order_new", "EXCEPTION", category="UNEXPLAINED")], run_id="run-new")
        self.assertEqual(len(db.get_all_exceptions()), 2)

    def test_legacy_table_without_new_settlement_fields_gets_migrated(self):
        """Simulates a data/reconcile.db created before gross_amount/
        mdr_amount/utr/settlement_date existed -- confirms get_connection()
        backfills the columns (as NULL on the pre-existing row) instead of
        crashing, and a fresh persist_results() can write real values into
        them afterward."""
        import sqlite3
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute("""
            CREATE TABLE exceptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_key TEXT, run_id TEXT NOT NULL, order_id TEXT, settlement_id TEXT,
                net_amount REAL, status TEXT NOT NULL, category TEXT,
                reason TEXT, narration TEXT, needs_action TEXT NOT NULL,
                replay_log TEXT NOT NULL,
                resolution_status TEXT NOT NULL DEFAULT 'OPEN',
                resolution_note TEXT, resolved_at TEXT
            )
        """)
        conn.execute(
            "INSERT INTO exceptions (match_key, run_id, settlement_id, status, needs_action, replay_log) "
            "VALUES ('setl_legacy', 'old-run', 'setl_legacy', 'EXCEPTION', 'yes', '[]')"
        )
        conn.commit()
        conn.close()

        rows = db.get_all_exceptions()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["utr"])
        self.assertIsNone(rows[0]["method"])
        self.assertIsNone(rows[0]["dispute_id"])

        db.persist_results(
            [make_result("order_new", "MATCHED", utr="UTR002", settlement_date="2026-08-06",
                          method="Card", dispute_id="disp_xyz")],
            run_id="run-new",
        )
        new_row = next(r for r in db.get_all_exceptions() if r["order_id"] == "order_new")
        self.assertEqual(new_row["utr"], "UTR002")
        self.assertEqual(new_row["method"], "Card")
        self.assertEqual(new_row["dispute_id"], "disp_xyz")
        self.assertEqual(new_row["settlement_date"], "2026-08-06")


class TestSummarizeReplay(unittest.TestCase):
    """Moved here from test_review_server.py when summarize_replay() itself
    moved from review_server.py to db.py, same reason and same precedent
    as TestComputeCashClarity below: settlement_qa.py's chat answers need
    the exact same function, and both modules already import db
    unconditionally, so this avoids a circular import between them."""

    def test_builds_capitalized_semicolon_joined_line_from_details(self):
        replay_log = [
            {"pass": "1", "action": "matched", "detail": "settlement<->bank matched on UTR+amount+date",
             "confidence": None, "timestamp": "x", "correlation_id": "run-1"},
            {"pass": "2", "action": "matched", "detail": "settlement<->ledger matched on order_id",
             "confidence": None, "timestamp": "x", "correlation_id": "run-1"},
        ]
        summary = db.summarize_replay(replay_log)
        self.assertEqual(
            summary,
            "Settlement<->bank matched on UTR+amount+date; Settlement<->ledger matched on order_id",
        )

    def test_empty_replay_log_returns_empty_string(self):
        self.assertEqual(db.summarize_replay([]), "")

    def test_entries_with_no_detail_return_empty_string(self):
        replay_log = [{"pass": "1", "action": "matched", "detail": "", "confidence": None,
                        "timestamp": "x", "correlation_id": "run-1"}]
        self.assertEqual(db.summarize_replay(replay_log), "")

    def test_legacy_plain_string_entries_are_ignored(self):
        replay_log = ["PASS1: settlement<->bank matched on UTR+amount+date"]
        self.assertEqual(db.summarize_replay(replay_log), "")


class TestComputeCashClarity(unittest.TestCase):
    """Moved here from test_review_server.py when compute_cash_clarity()
    itself moved from review_server.py to db.py, so
    settlement_qa.py's cash-value chat questions can call the exact same
    function the Overview page's cash-position panel uses without a
    circular import (both review_server.py and settlement_qa.py already
    import db unconditionally)."""

    def _row(self, net_amount, category, status):
        return {"net_amount": net_amount, "category": category, "status": status}

    def test_splits_resolved_from_still_open(self):
        rows = [
            self._row(975.42, "ROUNDING", "MATCHED_WITH_VARIANCE"),
            self._row(500.00, "UNEXPLAINED", "EXCEPTION"),
            self._row(200.00, None, "MATCHED"),  # no category -- outside the at-risk universe entirely
        ]
        result = db.compute_cash_clarity(rows)
        self.assertAlmostEqual(result["at_risk"], 1475.42, places=2)
        self.assertAlmostEqual(result["resolved"], 975.42, places=2)
        self.assertAlmostEqual(result["still_open"], 500.00, places=2)
        self.assertAlmostEqual(result["resolved_pct"], round(100 * 975.42 / 1475.42, 1), places=1)

    def test_no_categorized_rows_returns_zero_without_dividing_by_zero(self):
        rows = [self._row(100.0, None, "MATCHED")]
        result = db.compute_cash_clarity(rows)
        self.assertEqual(result["at_risk"], 0)
        self.assertEqual(result["resolved_pct"], 0.0)

    def test_matched_low_confidence_is_pending_review_not_resolved(self):
        """Regression test for a real bug: MATCHED_LOW_CONFIDENCE (an
        unconfirmed arbiter candidate) was folded into "resolved" here,
        same bug as reconcile.py's summarize() -- fixed in both places,
        kept in sync. db.py's own needs_action rule already treats this
        status like EXCEPTION, not like done."""
        rows = [self._row(300.0, "FUZZY_MATCH_NEEDS_REVIEW", "MATCHED_LOW_CONFIDENCE")]
        result = db.compute_cash_clarity(rows)
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
        result = db.compute_cash_clarity(rows)
        self.assertEqual(result["at_risk"], 200.0)
        self.assertEqual(result["still_open"], 200.0)
        self.assertEqual(result["resolved"], 0.0)
        self.assertEqual(result["pending_review"], 0.0)


if __name__ == "__main__":
    unittest.main()
