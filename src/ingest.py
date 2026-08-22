"""
Ingestion layer: normalizes real Razorpay API field names and synthetic CSV
field names into one internal schema before anything touches reconcile.py's
matching logic.

Internal canonical settlement-row schema (matches generate_data.py's
synthetic output):
    settlement_id, payment_id, order_id, gross, mdr, gst_on_mdr, net, utr,
    settlement_date, on_hold

Real Razorpay API shape (GET /v1/settlements/recon/combined?year=&month=[&day=]):
each recon line carries entity_id, type, debit, credit, amount, fee, tax,
settled (bool), settled_at (unix timestamp -- the line's own settlement
date), created_at (unix timestamp -- when the payment/refund was created,
not when it settled), settlement_id, settlement_utr, order_id, method,
dispute_id, and on_hold (bool).

Mapping used below:
    gross -> amount, mdr -> fee, gst_on_mdr -> tax, net -> credit,
    utr -> settlement_utr, settlement_date -> settled_at (converted from
    unix timestamp to "YYYY-MM-DD")
"""
import csv
import json
import urllib.error
import urllib.request
from base64 import b64encode
from datetime import datetime, timezone
from pathlib import Path

from config import get_razorpay_credentials
import generic_gateway_adapter

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
API_BASE = "https://api.razorpay.com/v1"


def normalize_recon_line(raw):
    """One line from the settlement recon endpoint -> canonical settlement row."""
    settlement_date = None
    settled_at = raw.get("settled_at")
    if settled_at is not None:
        settlement_date = datetime.fromtimestamp(settled_at, tz=timezone.utc).strftime("%Y-%m-%d")

    return {
        "settlement_id": raw.get("settlement_id"),
        "payment_id": raw.get("entity_id"),
        "order_id": raw.get("order_id"),
        "gross": raw.get("amount"),
        "mdr": raw.get("fee"),
        "gst_on_mdr": raw.get("tax"),
        "net": raw.get("credit"),
        "utr": raw.get("settlement_utr"),
        "settlement_date": settlement_date,
        "on_hold": bool(raw.get("on_hold", False)),
    }


def _live_get(path, key_id, key_secret, params=None):
    """Authenticated GET against the Razorpay API using HTTP Basic Auth
    (key_id as username, key_secret as password). Never logs the
    Authorization header; raises with Razorpay's own error body on failure."""
    url = f"{API_BASE}/{path}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())

    auth = b64encode(f"{key_id}:{key_secret}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Razorpay API returned HTTP {e.code} for {path}: {body}") from None


def fetch_live_recon(year, month, day=None):
    """Calls GET /v1/settlements/recon/combined and returns the raw response."""
    key_id, key_secret = get_razorpay_credentials()
    params = {"year": year, "month": month}
    if day is not None:
        params["day"] = day
    return _live_get("settlements/recon/combined", key_id, key_secret, params)


def load_settlements(source="synthetic", year=None, month=None, day=None):
    """source='synthetic' (default): reads the CSV, already in canonical shape.

    source='live': authenticated call to Razorpay's API. Test-mode keys
    authenticate the same as live-mode keys, but test-mode transactions
    never generate settlements (settlement is real money moving to a bank
    account, which never happens in test mode) -- an authenticated call
    returning zero items is the expected, correct result under test-mode
    keys, not a failure.

    source='with_gateway_b': combines the synthetic (Razorpay-shaped)
    settlements with a second batch normalized from a different export
    shape (generic_gateway_adapter.py), proving reconcile.py's matching
    logic isn't hardcoded to one settlement source's column layout.
    """
    if source == "synthetic":
        with open(DATA_DIR / "settlement_report.csv", newline="") as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            r["gross"] = float(r["gross"])
            r["mdr"] = float(r["mdr"])
            r["gst_on_mdr"] = float(r["gst_on_mdr"])
            r["net"] = float(r["net"])
            # `or "False"`, not `.get(..., "False")`: a row with fewer
            # columns than the header gives on_hold=None (an existing key
            # with no value), which .get()'s default doesn't cover.
            r["on_hold"] = (r.get("on_hold") or "False").strip().lower() == "true"
        return rows
    elif source == "live":
        now = datetime.now(timezone.utc)
        raw = fetch_live_recon(year or now.year, month or now.month, day)
        items = raw.get("items", [])
        return [normalize_recon_line(item) for item in items]
    elif source == "with_gateway_b":
        razorpay_rows = load_settlements(source="synthetic")
        gateway_b_rows = generic_gateway_adapter.load_gateway_b_export()
        return razorpay_rows + gateway_b_rows
    else:
        raise ValueError(f"unknown source: {source!r}")


if __name__ == "__main__":
    fixture_recon_line = {
        "entity_id": "pay_Nk8x2FhLp9QeRt",
        "type": "payment",
        "debit": 0,
        "credit": 4881.02,
        "amount": 4999,
        "fee": 88.98,
        "tax": 16.02,
        "settled": True,
        "on_hold": False,
        "created_at": 1786656000,
        "settled_at": 1786742400,
        "settlement_id": "setl_Mq3nP7vXaB1c2D",
        "settlement_utr": "UTR2026081512345",
        "order_id": "order_1015",
        "method": "upi",
    }
    fixture_on_hold_line = {**fixture_recon_line, "on_hold": True, "settled": False,
                             "settled_at": None, "order_id": "order_1020"}

    normalized = normalize_recon_line(fixture_recon_line)
    print("Recon line     ->", fixture_recon_line)
    print("Normalized to  ->", normalized)
    assert normalized["order_id"] == "order_1015"
    assert normalized["net"] == 4881.02
    assert normalized["utr"] == "UTR2026081512345"
    assert normalized["settlement_date"] == "2026-08-14"
    assert normalized["on_hold"] is False

    normalized_hold = normalize_recon_line(fixture_on_hold_line)
    print("\nOn-hold line   ->", fixture_on_hold_line)
    print("Normalized to  ->", normalized_hold)
    assert normalized_hold["on_hold"] is True

    print("\nMapping verified: settlement_date comes from the recon line's own "
          "settled_at, no separate batch-level join needed. on_hold carries "
          "through correctly in both states.")
