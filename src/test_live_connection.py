"""
Run once RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are set in the repo-root
.env file:

    python test_live_connection.py

Makes one authenticated call to Razorpay's API and reports the outcome --
never prints the key values, only the HTTP result and settlement shape.

Expected with test-mode keys: a 200 response with an empty items list.
Test mode never generates settlements, so this confirms auth and request
shape are correct. A 401 means the key pair is wrong or mismatched; a 400
means the request itself is malformed.
"""
import sys
from datetime import datetime, timezone

import ingest


def main() -> None:
    now = datetime.now(timezone.utc)
    print(f"Calling GET /v1/settlements/recon/combined?year={now.year}&month={now.month} ...")
    try:
        raw = ingest.fetch_live_recon(now.year, now.month)
    except RuntimeError as e:
        print(f"\nFAILED: {e}")
        sys.exit(1)

    items = raw.get("items", [])
    print(f"\nSUCCESS -- authenticated call completed.")
    print(f"Items returned: {len(items)}")

    if not items:
        print(
            "\nZero items is the expected result with test-mode keys -- "
            "test mode does not generate settlements. This confirms the "
            "connection and auth are correct."
        )
    else:
        print("\nGot real data. Normalizing the first item as a sanity check:")
        print(ingest.normalize_recon_line(items[0]))
        print(
            "\nIf you see real settlement data, you may be pointed at live-mode "
            "keys, not test-mode ones -- double check which .env you filled in."
        )


if __name__ == "__main__":
    main()
