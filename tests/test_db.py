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


def make_result(order_id, status, category=None, narration="", net=100.0, match_key=None):
    return {
        "order_id": order_id, "settlement_id": f"setl_{order_id}", "net": net,
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


class TestResolveException(DbTestCase):
    def test_confirm_closes_the_row(self):
        db.persist_results([make_result("order_1", "EXCEPTION", category="UNEXPLAINED")], run_id="run-1")
        row_id = db.get_open_exceptions()[0]["id"]
        db.resolve_exception(row_id, "confirm")
        self.assertEqual(db.get_open_exceptions(), [])
        all_rows = db.get_all_exceptions()
        self.assertEqual(all_rows[0]["resolution_status"], "CONFIRMED")
        self.assertIsNotNone(all_rows[0]["resolved_at"])

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


if __name__ == "__main__":
    unittest.main()
