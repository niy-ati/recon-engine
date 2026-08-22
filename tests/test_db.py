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


def make_result(order_id, status, category=None, narration="", net=100.0):
    return {
        "order_id": order_id, "settlement_id": f"setl_{order_id}", "net": net,
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

    def test_second_run_replaces_first_run_entirely(self):
        """Regression test: persist_results must not accumulate rows across
        runs. A prior bug deleted only same-run_id rows (run_id is always a
        fresh timestamp, so nothing ever matched), causing every re-run to
        double the queue instead of replacing it."""
        db.persist_results([make_result("order_1", "EXCEPTION", category="UNEXPLAINED")], run_id="run-1")
        db.persist_results([make_result("order_2", "EXCEPTION", category="UNEXPLAINED")], run_id="run-2")
        all_rows = db.get_all_exceptions()
        self.assertEqual(len(all_rows), 1)
        self.assertEqual(all_rows[0]["order_id"], "order_2")
        self.assertEqual(all_rows[0]["run_id"], "run-2")

    def test_matched_rows_are_not_actionable(self):
        db.persist_results([make_result("order_1", "MATCHED")], run_id="run-1")
        self.assertEqual(db.get_open_exceptions(), [])
        self.assertEqual(len(db.get_all_exceptions()), 1)


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
                          narration="pymt rcvd customer thx")],
            run_id="run-1",
        )
        row_id = db.get_open_exceptions()[0]["id"]
        db.resolve_exception(row_id, "confirm")
        rule = db.get_narration_rule("pymt rcvd customer thx")
        self.assertIsNotNone(rule)
        self.assertEqual(rule["order_id"], "order_1")
        self.assertEqual(rule["source"], "human_review")

    def test_rejecting_fuzzy_match_does_not_write_narration_rule(self):
        db.persist_results(
            [make_result("order_1", "MATCHED_LOW_CONFIDENCE", category="FUZZY_MATCH_NEEDS_REVIEW",
                          narration="pymt rcvd customer thx")],
            run_id="run-1",
        )
        row_id = db.get_open_exceptions()[0]["id"]
        db.resolve_exception(row_id, "reject")
        self.assertIsNone(db.get_narration_rule("pymt rcvd customer thx"))

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


if __name__ == "__main__":
    unittest.main()
