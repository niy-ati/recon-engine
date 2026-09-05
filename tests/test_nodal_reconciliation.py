"""
Unit tests for nodal_reconciliation.py -- built against small, hand-built
CSV fixtures in a temp directory, never against the real synthetic batch,
so a future generate_data.py edit can't silently break these.
"""
import csv
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import nodal_reconciliation as nr  # noqa: E402


def write_settlement_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["settlement_id", "payment_id", "order_id", "gross", "mdr", "gst_on_mdr",
                    "net", "utr", "settlement_date", "on_hold", "method", "dispute_id"])
        for r in rows:
            w.writerow(r)


def settlement_row(settlement_id, net, settlement_date, on_hold=False):
    return [settlement_id, f"pay_{settlement_id}", f"order_{settlement_id}", net + 20, 15, 3,
            net, f"UTR{settlement_id}", settlement_date, on_hold, "UPI", ""]


class TestComputeDailyObligation(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_missing_file_returns_empty(self):
        self.assertEqual(nr.compute_daily_obligation(self.data_dir), {})

    def test_obligation_active_between_collection_and_settlement(self):
        """A single settlement_date of 2026-08-05 implies collection on
        2026-08-03 (T+2, matching generate_data.py's own rule) -- the
        obligation is owed from the collection date up to but not
        including the settlement date itself, when it's paid out."""
        write_settlement_csv(self.data_dir / "settlement_report.csv",
                              [settlement_row("s1", 1000.0, "2026-08-05")])
        obligation = nr.compute_daily_obligation(self.data_dir)
        self.assertNotIn("2026-08-02", obligation)  # before the range starts at all
        self.assertEqual(obligation["2026-08-03"], 1000.0)  # collected
        self.assertEqual(obligation["2026-08-04"], 1000.0)  # still in escrow
        self.assertEqual(obligation["2026-08-05"], 0.0)  # settled out, clears

    def test_on_hold_row_never_clears(self):
        write_settlement_csv(self.data_dir / "settlement_report.csv",
                              [settlement_row("s1", 500.0, "2026-08-05", on_hold=True)])
        obligation = nr.compute_daily_obligation(self.data_dir)
        # The horizon extends one day past the (never-reached) settlement
        # date -- on_hold money is still sitting there at the very end.
        self.assertEqual(obligation["2026-08-06"], 500.0)

    def test_duplicate_settlement_counted_once(self):
        """Same reasoning as reconcile.py's own DUPLICATE handling: only
        one real bank credit exists per base settlement_id, so counting
        both the original and its _dup sibling would double the real
        obligation for money that only moved once."""
        write_settlement_csv(self.data_dir / "settlement_report.csv", [
            settlement_row("s1", 1000.0, "2026-08-05"),
            settlement_row("s1_dup", 1000.0, "2026-08-05"),
        ])
        obligation = nr.compute_daily_obligation(self.data_dir)
        self.assertEqual(obligation["2026-08-03"], 1000.0)

    def test_overlapping_settlements_sum(self):
        write_settlement_csv(self.data_dir / "settlement_report.csv", [
            settlement_row("s1", 1000.0, "2026-08-05"),
            settlement_row("s2", 2000.0, "2026-08-06"),
        ])
        obligation = nr.compute_daily_obligation(self.data_dir)
        self.assertEqual(obligation["2026-08-04"], 3000.0)  # both in flight


class TestAuditNodalBalance(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)
        write_settlement_csv(self.data_dir / "settlement_report.csv",
                              [settlement_row("s1", 1000.0, "2026-08-05")])

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write_balance(self, rows):
        with open(self.data_dir / "nodal_balance.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "closing_balance"])
            w.writerows(rows)

    def test_missing_balance_file_returns_empty(self):
        self.assertEqual(nr.audit_nodal_balance(self.data_dir), [])

    def test_balance_matching_obligation_is_clean(self):
        self._write_balance([["2026-08-03", 1000.0], ["2026-08-04", 1000.0]])
        self.assertEqual(nr.audit_nodal_balance(self.data_dir), [])

    def test_balance_below_obligation_is_a_shortfall(self):
        """The real RBI-invariant breach: the escrow doesn't hold enough
        to cover what's owed to the merchant."""
        self._write_balance([["2026-08-03", 800.0]])
        findings = nr.audit_nodal_balance(self.data_dir)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["kind"], "SHORTFALL")
        self.assertEqual(findings[0]["diff"], 200.0)

    def test_balance_above_obligation_is_a_note_not_a_violation(self):
        """RBI's own text is a floor ('shall not be less than'), not an
        exact-equality requirement -- a surplus is reported as an
        informational note, not mislabeled as a compliance breach."""
        self._write_balance([["2026-08-03", 1500.0]])
        findings = nr.audit_nodal_balance(self.data_dir)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["kind"], "SURPLUS_NOTE")
        self.assertEqual(findings[0]["diff"], 500.0)

    def test_tiny_rounding_difference_is_not_flagged(self):
        self._write_balance([["2026-08-03", 1000.40]])
        self.assertEqual(nr.audit_nodal_balance(self.data_dir), [])

    def test_date_with_no_matching_obligation_is_skipped(self):
        """A balance-file date outside the settlement batch's own range
        has nothing to compare against -- skipped, not a false positive."""
        self._write_balance([["2099-01-01", 999999.0]])
        self.assertEqual(nr.audit_nodal_balance(self.data_dir), [])


class TestWriteSyntheticBalanceCsv(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_no_settlement_file_writes_nothing(self):
        nr.write_synthetic_balance_csv(self.data_dir)
        self.assertFalse((self.data_dir / "nodal_balance.csv").exists())

    def test_writes_one_row_per_day_and_flags_two_days(self):
        write_settlement_csv(self.data_dir / "settlement_report.csv", [
            settlement_row("s1", 1000.0, "2026-08-05"),
            settlement_row("s2", 2000.0, "2026-08-10"),
        ])
        nr.write_synthetic_balance_csv(self.data_dir)
        path = self.data_dir / "nodal_balance.csv"
        self.assertTrue(path.exists())
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertGreater(len(rows), 0)

        findings = nr.audit_nodal_balance(self.data_dir)
        kinds = sorted(f["kind"] for f in findings)
        self.assertEqual(kinds, ["SHORTFALL", "SURPLUS_NOTE"])

    def test_deterministic_across_runs(self):
        """No randomness involved -- same input produces byte-identical
        output every time, same discipline as the rest of the synthetic
        pipeline."""
        write_settlement_csv(self.data_dir / "settlement_report.csv",
                              [settlement_row("s1", 1000.0, "2026-08-05")])
        nr.write_synthetic_balance_csv(self.data_dir)
        first = (self.data_dir / "nodal_balance.csv").read_text()
        nr.write_synthetic_balance_csv(self.data_dir)
        second = (self.data_dir / "nodal_balance.csv").read_text()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
