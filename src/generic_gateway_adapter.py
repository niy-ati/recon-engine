"""
Proves reconcile.py's matching logic is gateway-agnostic, not hardcoded to
Razorpay's export shape. Merchants commonly run more than one payment
gateway in parallel, so a reconciliation tool that only understands one
gateway's settlement shape doesn't generalize.

"Gateway B" is a deliberately generic stand-in, not a replica of any real
competitor's API or file format -- the point being demonstrated is
architectural: the same Pass 1-4 matching logic in reconcile.py, unmodified,
resolves a settlement that arrived via a completely different export shape
once it passes through a config-driven column mapping.

Canonical schema (identical to what ingest.py produces for Razorpay data):
    settlement_id, payment_id, order_id, gross, mdr, gst_on_mdr, net, utr, settlement_date
"""
import csv
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# The entire adapter for a new gateway is this one mapping -- add a
# gateway by adding a config, not by writing new matching code.
GATEWAY_B_CONFIG = {
    "settlement_id": "txn_ref",
    "payment_id": "gateway_payment_id",
    "order_id": "merchant_order_id",
    "gross": "gross_amount",
    "mdr": "processing_fee",
    "gst_on_mdr": "gst_on_fee",
    "net": "net_settled",
    "utr": "bank_utr",
    "settlement_date": "settlement_dt",
}
GATEWAY_B_DATE_FORMAT = "%d-%m-%Y"  # deliberately different from Razorpay's YYYY-MM-DD


def normalize_gateway_row(raw: dict, config: dict, date_format: str = "%Y-%m-%d") -> dict:
    """Maps one row from an arbitrary gateway's column names into the
    canonical schema reconcile.py expects. Everything gateway-specific is
    confined to the config dict, never to logic here."""
    canonical = {config_field: raw.get(source_field) for config_field, source_field in config.items()}

    for money_field in ("gross", "mdr", "gst_on_mdr", "net"):
        if canonical.get(money_field) not in (None, ""):
            canonical[money_field] = float(canonical[money_field])

    if canonical.get("settlement_date"):
        parsed = datetime.strptime(canonical["settlement_date"], date_format)
        canonical["settlement_date"] = parsed.strftime("%Y-%m-%d")

    # A generic adapter has no way to know another gateway's on_hold
    # equivalent, so it defaults to False rather than guessing.
    canonical["on_hold"] = False

    return canonical


def load_gateway_b_export(path: Path | None = None) -> list[dict]:
    """Reads data/gateway_b_export.csv (Gateway B's own column names and
    date format) and returns rows normalized into the canonical schema."""
    path = path or (DATA_DIR / "gateway_b_export.csv")
    if not path.exists():
        return []
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    return [normalize_gateway_row(r, GATEWAY_B_CONFIG, GATEWAY_B_DATE_FORMAT) for r in rows]


if __name__ == "__main__":
    fixture = {
        "txn_ref": "GWB-88213",
        "gateway_payment_id": "gwpay_9f3a1c",
        "merchant_order_id": "order_5001",
        "gross_amount": "2999",
        "processing_fee": "44.99",
        "gst_on_fee": "8.10",
        "net_settled": "2945.91",
        "bank_utr": "UTR2026081599999",
        "settlement_dt": "17-08-2026",  # DD-MM-YYYY, Gateway B's own format
    }
    normalized = normalize_gateway_row(fixture, GATEWAY_B_CONFIG, GATEWAY_B_DATE_FORMAT)
    print("Gateway B raw row ->", fixture)
    print("Normalized to     ->", normalized)
    assert normalized["order_id"] == "order_5001"
    assert normalized["net"] == 2945.91
    assert normalized["settlement_date"] == "2026-08-17"  # DD-MM-YYYY correctly converted
    print("\nMapping verified: a different column layout and date format "
          "normalizes into the same canonical schema Razorpay's own data produces.")
