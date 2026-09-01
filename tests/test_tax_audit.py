"""
Unit tests for tax_audit.py. Uses a temp settlement_report.csv rather than
the real one -- these need exact, controlled MDR/GST figures to prove the
18% math and the tolerance boundary, not whatever happens to be in the
real demo batch at test time.
"""
import csv
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import tax_audit  # noqa: E402


def write_settlement_csv(path, rows):
    fieldnames = ["settlement_id", "payment_id", "order_id", "gross", "mdr",
                  "gst_on_mdr", "net", "utr", "settlement_date", "on_hold"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            full = {k: "" for k in fieldnames}
            full.update(row)
            writer.writerow(full)


class TestAuditTaxLines(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._data_dir = Path(self._tmpdir.name)
        self._orig_data_dir = tax_audit.DATA_DIR
        tax_audit.DATA_DIR = self._data_dir
        self.addCleanup(setattr, tax_audit, "DATA_DIR", self._orig_data_dir)

    def _write(self, rows):
        write_settlement_csv(self._data_dir / "settlement_report.csv", rows)

    def test_correct_rate_produces_no_finding(self):
        """MDR 100.00 * 18% = 18.00 exactly -- a row charging exactly that
        must not be flagged."""
        self._write([{"settlement_id": "setl_1", "order_id": "order_1", "mdr": "100.00", "gst_on_mdr": "18.00"}])
        self.assertEqual(tax_audit.audit_tax_lines(), [])

    def test_overcharged_gst_is_flagged(self):
        self._write([{"settlement_id": "setl_1", "order_id": "order_1", "mdr": "100.00", "gst_on_mdr": "19.00"}])
        findings = tax_audit.audit_tax_lines()
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["order_id"], "order_1")
        self.assertEqual(f["expected_gst"], 18.00)
        self.assertEqual(f["actual_gst"], 19.00)
        self.assertEqual(f["diff"], 1.00)
        self.assertEqual(f["direction"], "overcharged")

    def test_undercharged_gst_is_flagged(self):
        self._write([{"settlement_id": "setl_1", "order_id": "order_1", "mdr": "100.00", "gst_on_mdr": "16.00"}])
        findings = tax_audit.audit_tax_lines()
        self.assertEqual(findings[0]["direction"], "undercharged")

    def test_tiny_rounding_difference_is_not_a_finding(self):
        """A few paise of float rounding noise is normal, not a real
        finding -- must stay under TOLERANCE_RS."""
        self._write([{"settlement_id": "setl_1", "order_id": "order_1", "mdr": "100.00", "gst_on_mdr": "18.10"}])
        self.assertEqual(tax_audit.audit_tax_lines(), [])

    def test_multiple_rows_only_flags_the_wrong_ones(self):
        self._write([
            {"settlement_id": "setl_1", "order_id": "order_1", "mdr": "100.00", "gst_on_mdr": "18.00"},
            {"settlement_id": "setl_2", "order_id": "order_2", "mdr": "50.00", "gst_on_mdr": "10.00"},
            {"settlement_id": "setl_3", "order_id": "order_3", "mdr": "200.00", "gst_on_mdr": "36.00"},
        ])
        findings = tax_audit.audit_tax_lines()
        self.assertEqual([f["order_id"] for f in findings], ["order_2"])

    def test_missing_settlement_report_returns_empty_not_an_error(self):
        self.assertEqual(tax_audit.audit_tax_lines(), [])

    def test_malformed_row_is_skipped_not_fatal(self):
        self._write([{"settlement_id": "setl_1", "order_id": "order_1", "mdr": "not-a-number", "gst_on_mdr": "18.00"}])
        self.assertEqual(tax_audit.audit_tax_lines(), [])


class TestAuditMonthlyReconciliation(unittest.TestCase):
    """Mirrors RazorpayX's own real transaction-level vs consolidated-
    monthly tax reporting split -- see tax_audit.py's module docstring.
    This tier exists specifically to catch a month where every row sits
    inside audit_tax_lines()'s own per-row tolerance yet the aggregate
    still drifts materially -- so every fixture here is built to be
    individually clean by that check on purpose."""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._data_dir = Path(self._tmpdir.name)
        self._orig_data_dir = tax_audit.DATA_DIR
        tax_audit.DATA_DIR = self._data_dir
        self.addCleanup(setattr, tax_audit, "DATA_DIR", self._orig_data_dir)

    def _write(self, rows):
        write_settlement_csv(self._data_dir / "settlement_report.csv", rows)

    def test_correctly_charged_month_produces_no_finding(self):
        self._write([
            {"settlement_id": "setl_1", "mdr": "100.00", "gst_on_mdr": "18.00", "settlement_date": "2026-08-01"},
            {"settlement_id": "setl_2", "mdr": "200.00", "gst_on_mdr": "36.00", "settlement_date": "2026-08-15"},
        ])
        self.assertEqual(tax_audit.audit_monthly_reconciliation(), [])

    def test_sub_tolerance_rows_still_accumulate_to_a_real_finding(self):
        """Each row here is 0.40 over -- comfortably inside
        audit_tax_lines()'s own Rs.0.50 tolerance, so none of them are
        individually flagged -- but ten of them together drift Rs.4.00,
        clearing MONTHLY_TOLERANCE_RS. The exact gap a per-row check is
        structurally unable to see."""
        rows = [
            {"settlement_id": f"setl_{i}", "mdr": "100.00", "gst_on_mdr": "18.40", "settlement_date": "2026-08-01"}
            for i in range(10)
        ]
        self._write(rows)
        self.assertEqual(tax_audit.audit_tax_lines(), [])
        findings = tax_audit.audit_monthly_reconciliation()
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["month"], "2026-08")
        self.assertEqual(f["settlement_count"], 10)
        self.assertEqual(f["diff"], 4.00)
        self.assertEqual(f["direction"], "overcharged")
        self.assertEqual(f["already_flagged_per_row"], 0.0)
        self.assertEqual(f["unexplained"], 4.00)

    def test_known_per_row_finding_is_not_double_counted_as_unexplained(self):
        """A row already caught by audit_tax_lines() must not also read as
        fresh, unexplained drift at the monthly tier -- "unexplained"
        means genuinely new information, not a restatement."""
        rows = [
            {"settlement_id": "setl_1", "order_id": "order_1", "mdr": "100.00",
             "gst_on_mdr": "23.00", "settlement_date": "2026-08-01"},
        ]
        self._write(rows)
        per_row = tax_audit.audit_tax_lines()
        self.assertEqual(len(per_row), 1)
        findings = tax_audit.audit_monthly_reconciliation()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["diff"], findings[0]["already_flagged_per_row"])
        self.assertEqual(findings[0]["unexplained"], 0.0)

    def test_two_months_are_reported_independently(self):
        """July's own two rows charge exactly the right rate -- only
        August's drifting rows should ever be reported."""
        rows = [
            {"settlement_id": "setl_jul_1", "mdr": "100.00", "gst_on_mdr": "18.00", "settlement_date": "2026-07-01"},
            {"settlement_id": "setl_jul_2", "mdr": "100.00", "gst_on_mdr": "18.00", "settlement_date": "2026-07-15"},
        ] + [
            {"settlement_id": f"setl_aug_{i}", "mdr": "100.00", "gst_on_mdr": "18.40", "settlement_date": "2026-08-01"}
            for i in range(10)
        ]
        self._write(rows)
        months = {f["month"] for f in tax_audit.audit_monthly_reconciliation()}
        self.assertEqual(months, {"2026-08"})

    def test_missing_settlement_report_returns_empty_not_an_error(self):
        self.assertEqual(tax_audit.audit_monthly_reconciliation(), [])


if __name__ == "__main__":
    unittest.main()
