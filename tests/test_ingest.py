"""
Unit tests for ingest.py's normalization layer: the boundary between real
Razorpay API shapes / synthetic CSVs and reconcile.py's canonical schema.
"""
import csv
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import ingest  # noqa: E402


class TestNormalizeReconLine(unittest.TestCase):
    def test_maps_all_fields_correctly(self):
        raw = {
            "entity_id": "pay_Nk8x2FhLp9QeRt", "amount": 4999, "fee": 88.98, "tax": 16.02,
            "credit": 4881.02, "settled": True, "on_hold": False,
            "settled_at": 1786742400, "settlement_id": "setl_Mq3nP7vXaB1c2D",
            "settlement_utr": "UTR2026081512345", "order_id": "order_1015",
        }
        n = ingest.normalize_recon_line(raw)
        self.assertEqual(n["order_id"], "order_1015")
        self.assertEqual(n["net"], 4881.02)
        self.assertEqual(n["gross"], 4999)
        self.assertEqual(n["mdr"], 88.98)
        self.assertEqual(n["gst_on_mdr"], 16.02)
        self.assertEqual(n["utr"], "UTR2026081512345")
        self.assertEqual(n["settlement_date"], "2026-08-14")
        self.assertIs(n["on_hold"], False)

    def test_settlement_date_comes_from_settled_at_not_created_at(self):
        """Regression check for a corrected assumption during development:
        the recon line's own settled_at is the settlement date, not a
        separate batch-level created_at join."""
        raw = {"settled_at": 1786742400, "created_at": 1786656000, "order_id": "order_1"}
        n = ingest.normalize_recon_line(raw)
        self.assertEqual(n["settlement_date"], "2026-08-14")

    def test_missing_settled_at_gives_none_date(self):
        raw = {"settled_at": None, "on_hold": True, "order_id": "order_1"}
        n = ingest.normalize_recon_line(raw)
        self.assertIsNone(n["settlement_date"])
        self.assertIs(n["on_hold"], True)

    def test_on_hold_defaults_false_when_absent(self):
        raw = {"order_id": "order_1"}
        n = ingest.normalize_recon_line(raw)
        self.assertIs(n["on_hold"], False)


class TestLoadSettlementsSynthetic(unittest.TestCase):
    def _write_csv(self, tmpdir, rows, header):
        path = Path(tmpdir) / "settlement_report.csv"
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        return path

    def test_loads_and_casts_numeric_fields(self):
        header = ["settlement_id", "payment_id", "order_id", "gross", "mdr",
                  "gst_on_mdr", "net", "utr", "settlement_date", "on_hold"]
        with tempfile.TemporaryDirectory() as tmp:
            self._write_csv(tmp, [["s1", "p1", "order_1", "999", "19.98", "3.6", "975.42", "UTR1", "2026-08-15", "False"]], header)
            rows = ingest.load_settlements(source="synthetic", data_dir=tmp)
            self.assertEqual(len(rows), 1)
            self.assertIsInstance(rows[0]["net"], float)
            self.assertEqual(rows[0]["net"], 975.42)
            self.assertIs(rows[0]["on_hold"], False)

    def test_on_hold_true_parsed_case_insensitively(self):
        header = ["settlement_id", "payment_id", "order_id", "gross", "mdr",
                  "gst_on_mdr", "net", "utr", "settlement_date", "on_hold"]
        with tempfile.TemporaryDirectory() as tmp:
            self._write_csv(tmp, [["s1", "p1", "order_1", "999", "19.98", "3.6", "975.42", "UTR1", "2026-08-15", "TRUE"]], header)
            rows = ingest.load_settlements(source="synthetic", data_dir=tmp)
            self.assertIs(rows[0]["on_hold"], True)

    def test_missing_on_hold_column_value_defaults_false_not_crash(self):
        """Regression test: a row with fewer columns than the header (e.g.
        appended by a script written before the on_hold column existed)
        gives on_hold=None (an existing key, no value) -- must not crash,
        must default to False."""
        header = ["settlement_id", "payment_id", "order_id", "gross", "mdr",
                  "gst_on_mdr", "net", "utr", "settlement_date", "on_hold"]
        path = None
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settlement_report.csv"
            with open(path, "w", newline="") as f:
                f.write(",".join(header) + "\n")
                f.write("s1,p1,order_1,999,19.98,3.6,975.42,UTR1,2026-08-15\n")  # no on_hold value
            rows = ingest.load_settlements(source="synthetic", data_dir=tmp)
            self.assertIs(rows[0]["on_hold"], False)

    def test_unknown_source_raises(self):
        with self.assertRaises(ValueError):
            ingest.load_settlements(source="not_a_real_source")


if __name__ == "__main__":
    unittest.main()
