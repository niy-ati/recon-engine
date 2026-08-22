"""
Single entry point for the batch pipeline: runs every step in order,
persists results to SQLite, and prints the full report.

    python run_all.py             # synthetic settlement data (default)
    python run_all.py --live      # Razorpay settlement API, test-mode keys
    python run_all.py --keep-data # skip data generation, use data/ as-is
                                   # (use after src/load_real_data.py so your
                                   # loaded files aren't overwritten)

The review server (`python src/review_server.py`) is a separate, long-running
command, not launched here.
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
STEPS = [
    "generate_data.py",
    "reconcile.py",
    "failure_injection_demo.py",
]


def run_step(script, extra_args=None):
    args = [sys.executable, script] + (extra_args or [])
    print(f"\n{'=' * 60}\n>>> {' '.join(args[1:])}\n{'=' * 60}", flush=True)
    result = subprocess.run(args, cwd=SRC)
    if result.returncode != 0:
        print(f"\n!! {script} exited with code {result.returncode} -- stopping here.")
        sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true",
                         help="Use the real Razorpay settlement API (test-mode keys) instead of synthetic data")
    parser.add_argument("--keep-data", action="store_true",
                         help="Skip generate_data.py and failure_injection_demo.py -- use data/ as-is "
                              "(needed after src/load_real_data.py, otherwise your loaded files get "
                              "overwritten or have synthetic trap rows appended to them)")
    args = parser.parse_args()

    steps = [] if args.keep_data else list(STEPS)
    for step in steps:
        run_step(step)
    if args.keep_data:
        print("\n--keep-data: skipping generate_data.py and failure_injection_demo.py, "
              "using data/ exactly as it is right now.")
    run_step("report.py", extra_args=["--live"] if args.live else [])

    report_path = ROOT / "output" / "reconciliation_report.md"

    print(f"\n{'=' * 60}\nFULL REPORT ({report_path.relative_to(ROOT)})\n{'=' * 60}")
    print(report_path.read_text())

    print(
        "\nNext: python src/review_server.py -- opens the live review queue "
        "at http://localhost:8000/, backed by the SQLite database this run "
        "just wrote to."
    )


if __name__ == "__main__":
    main()
