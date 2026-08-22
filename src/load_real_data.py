"""
Loads a real bank statement and/or internal ledger CSV in place of the
synthetic ones generate_data.py produces. Validates columns against what
reconcile.py expects before touching anything, and backs up whatever was
in data/ first.

    python load_real_data.py --bank path/to/your_bank_statement.csv
    python load_real_data.py --ledger path/to/your_tally_export.csv
    python load_real_data.py --bank ... --ledger ...

Either argument works alone -- loading just one side while keeping the
other synthetic is a supported scenario.

Note: this only makes sense paired with a settlement source that actually
relates to the loaded data. The synthetic settlement_report.csv has its
own fabricated UTRs and order IDs, so pairing it with a real bank
statement will show a near-zero match rate correctly, since there's
nothing to match.
"""
import argparse
import csv
import shutil
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

REQUIRED_COLUMNS = {
    "bank_statement.csv": ["utr", "credited_amount", "value_date", "narration"],
    "internal_ledger.csv": ["invoice_id", "order_ref", "customer", "amount", "narration", "gst_line"],
}


def validate_and_count(path, expected_columns):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    actual_columns = list(rows[0].keys()) if rows else []
    missing = [c for c in expected_columns if c not in actual_columns]
    if missing:
        raise ValueError(
            f"{path} is missing required column(s): {missing}.\n"
            f"  Found:    {actual_columns}\n"
            f"  Expected: {expected_columns}\n"
            f"Rename your columns to match, or run generate_data.py once to "
            f"see a reference file with the exact expected shape."
        )
    return len(rows)


def load(source_path, target_filename):
    target_path = DATA_DIR / target_filename
    row_count = validate_and_count(source_path, REQUIRED_COLUMNS[target_filename])

    if target_path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = DATA_DIR / f"{target_filename}.backup-{stamp}"
        shutil.copy(target_path, backup_path)
        print(f"Backed up existing {target_filename} -> {backup_path.name}")

    shutil.copy(source_path, target_path)
    print(f"Loaded {row_count} real rows from {source_path} -> data/{target_filename}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bank", type=Path, help="Path to your real bank statement CSV")
    parser.add_argument("--ledger", type=Path, help="Path to your real internal ledger CSV")
    args = parser.parse_args()

    if not args.bank and not args.ledger:
        parser.error("Provide at least one of --bank or --ledger")

    try:
        if args.bank:
            load(args.bank, "bank_statement.csv")
        if args.ledger:
            load(args.ledger, "internal_ledger.csv")
    except (ValueError, FileNotFoundError) as e:
        print(f"\nCould not load your file:\n\n{e}")
        raise SystemExit(1)

    print(
        "\nNow run: python run_all.py --keep-data  (plain run_all.py would "
        "regenerate synthetic data and overwrite what you just loaded -- "
        "--keep-data skips that step and the failure-injection step, using "
        "data/ exactly as it is now). Add --live too if you also want the "
        "settlement side to hit the real Razorpay API instead of synthetic."
    )


if __name__ == "__main__":
    main()
