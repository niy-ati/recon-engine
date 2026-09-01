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


if __name__ == "__main__":
    unittest.main()
