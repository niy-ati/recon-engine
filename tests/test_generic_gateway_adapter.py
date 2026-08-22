"""
Unit tests for generic_gateway_adapter.py -- the config-driven column
mapping that proves reconcile.py isn't hardcoded to Razorpay's export shape.
"""
import csv
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import generic_gateway_adapter as gga  # noqa: E402


class TestNormalizeGatewayRow(unittest.TestCase):
    def test_maps_arbitrary_columns_to_canonical_schema(self):
        raw = {
            "txn_ref": "GWB-88213", "gateway_payment_id": "gwpay_9f3a1c",
            "merchant_order_id": "order_5001", "gross_amount": "2999",
            "processing_fee": "44.99", "gst_on_fee": "8.10",
            "net_settled": "2945.91", "bank_utr": "UTR2026081599999",
            "settlement_dt": "17-08-2026",
        }
        n = gga.normalize_gateway_row(raw, gga.GATEWAY_B_CONFIG, gga.GATEWAY_B_DATE_FORMAT)
        self.assertEqual(n["settlement_id"], "GWB-88213")
        self.assertEqual(n["order_id"], "order_5001")
        self.assertEqual(n["net"], 2945.91)
        self.assertEqual(n["settlement_date"], "2026-08-17")
        self.assertIs(n["on_hold"], False)

    def test_money_fields_are_cast_to_float(self):
        raw = {"gross_amount": "1000", "processing_fee": "10", "gst_on_fee": "1.8",
               "net_settled": "988.2", "txn_ref": "x", "gateway_payment_id": "y",
               "merchant_order_id": "order_1", "bank_utr": "u", "settlement_dt": "01-01-2026"}
        n = gga.normalize_gateway_row(raw, gga.GATEWAY_B_CONFIG, gga.GATEWAY_B_DATE_FORMAT)
        for field in ("gross", "mdr", "gst_on_mdr", "net"):
            self.assertIsInstance(n[field], float)


class TestLoadGatewayBExport(unittest.TestCase):
    def test_missing_file_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = gga.load_gateway_b_export(path=Path(tmp) / "does_not_exist.csv")
            self.assertEqual(rows, [])

    def test_reads_and_normalizes_real_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gateway_b_export.csv"
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["txn_ref", "gateway_payment_id", "merchant_order_id",
                            "gross_amount", "processing_fee", "gst_on_fee",
                            "net_settled", "bank_utr", "settlement_dt"])
                w.writerow(["GWB-1", "gwpay_1", "order_5001", "2999", "44.99", "8.10", "2945.91", "UTR1", "17-08-2026"])
            rows = gga.load_gateway_b_export(path=path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["order_id"], "order_5001")
            self.assertEqual(rows[0]["settlement_date"], "2026-08-17")


if __name__ == "__main__":
    unittest.main()
