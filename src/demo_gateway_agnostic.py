"""
Demonstrates that reconcile.py's matching logic doesn't know or care which
gateway a settlement came from.

Appends 3 orders that settle via a differently-shaped export ("Gateway B",
see generic_gateway_adapter.py) to the existing bank statement and internal
ledger, writes their settlement data in Gateway B's own column names and
date format, then runs reconcile() with settlement_source="with_gateway_b"
-- zero changes to Pass 1-4.

Run after generate_data.py (needs the base synthetic batch to exist):

    python demo_gateway_agnostic.py
"""
import csv
from pathlib import Path

from reconcile import reconcile

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Deliberately outside generate_data.py's order_1000-1079 range, so there's
# no ambiguity about which source resolved which order.
GATEWAY_B_ORDERS = [
    {"order_id": "order_5001", "customer": "Customer GWB-1", "gross": 2999,
     "mdr": 44.99, "gst": 8.10, "net": 2945.91, "utr": "UTR2026081599901",
     "date": "17-08-2026"},
    {"order_id": "order_5002", "customer": "Customer GWB-2", "gross": 1499,
     "mdr": 22.49, "gst": 4.05, "net": 1472.46, "utr": "UTR2026081599902",
     "date": "18-08-2026"},
    {"order_id": "order_5003", "customer": "Customer GWB-3", "gross": 4999,
     "mdr": 74.99, "gst": 13.50, "net": 4910.51, "utr": "UTR2026081599903",
     "date": "19-08-2026"},
]


def inject_gateway_b_orders() -> None:
    """Writes data/gateway_b_export.csv in Gateway B's own column layout and
    date format, and appends the corresponding bank credit and ledger entry."""
    with open(DATA_DIR / "gateway_b_export.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["txn_ref", "gateway_payment_id", "merchant_order_id",
                    "gross_amount", "processing_fee", "gst_on_fee",
                    "net_settled", "bank_utr", "settlement_dt"])
        for o in GATEWAY_B_ORDERS:
            w.writerow([f"GWB-{o['order_id'][-4:]}", f"gwpay_{o['order_id'][-4:]}",
                        o["order_id"], o["gross"], o["mdr"], o["gst"], o["net"],
                        o["utr"], o["date"]])

    with open(DATA_DIR / "bank_statement.csv", "a", newline="") as f:
        w = csv.writer(f)
        for o in GATEWAY_B_ORDERS:
            iso_date = "-".join(reversed(o["date"].split("-")))  # DD-MM-YYYY -> YYYY-MM-DD
            w.writerow([o["utr"], o["net"], iso_date, f"NEFT CR GATEWAY-B SETTLEMENT {o['utr']}"])

    with open(DATA_DIR / "internal_ledger.csv", "a", newline="") as f:
        w = csv.writer(f)
        for o in GATEWAY_B_ORDERS:
            w.writerow([f"INV-{o['order_id'][-4:]}", o["order_id"], o["customer"],
                        o["gross"], f"Payment received order {o['order_id']} - {o['customer']}", o["gst"]])

    print(f"Injected {len(GATEWAY_B_ORDERS)} orders settling via 'Gateway B' "
          f"(data/gateway_b_export.csv) -- bank and ledger entries appended.")


def check_gateway_b_resolution(results: list[dict]) -> None:
    target_ids = {o["order_id"] for o in GATEWAY_B_ORDERS}
    matched = {r["order_id"]: r for r in results if r.get("order_id") in target_ids}

    print("\n=== GATEWAY B ORDER RESOLUTION ===")
    all_matched = True
    for order_id in target_ids:
        r = matched.get(order_id)
        if r is None:
            print(f"{order_id}: MISSING FROM RESULTS -- investigate")
            all_matched = False
            continue
        print(f"{order_id}: status={r['status']} stage={r['stage']}")
        if r["status"] not in ("MATCHED", "MATCHED_WITH_VARIANCE"):
            all_matched = False

    if all_matched:
        print("\n>> PASS: all 3 Gateway-B-sourced orders resolved cleanly through")
        print(">> the same Pass 1/Pass 2 matching logic used for Razorpay-sourced")
        print(">> settlements -- zero changes to reconcile.py, only a column-mapping")
        print(">> config in generic_gateway_adapter.py.")
    else:
        print("\n>> Some Gateway B orders did not resolve cleanly -- see stage logs above.")


if __name__ == "__main__":
    inject_gateway_b_orders()
    results = reconcile(settlement_source="with_gateway_b")
    check_gateway_b_resolution(results)
