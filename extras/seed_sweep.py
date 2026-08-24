"""
Reproduces the held-out multi-seed validation quoted in README.md and
src/generate_data.py's own docstring, instead of asking anyone to just
trust five numbers typed into a comment.

Runs the full pipeline (generate_data.py -> reconcile.py ->
failure_injection_demo.py -> report.py, the same steps run_all.py runs)
once per seed, against a genuinely different 514-row batch each time,
and reports the "Overall resolved" figure the pipeline itself computed --
never recomputed or estimated here.

Restores data/ and output/ to their committed state when done, or on any
error, via `git checkout --`. Refuses to run at all if either already has
uncommitted changes, so it can never discard real work.

    python extras/seed_sweep.py                  # the five seeds quoted in README.md
    python extras/seed_sweep.py 1 2 3             # any seeds you want to check yourself
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
REPORT = ROOT / "output" / "reconciliation_report.md"
TRACKED_PATHS = ["data", "output"]
DEFAULT_SEEDS = [42, 7, 21, 99, 555]

RESOLVED_RE = re.compile(r"\*\*Overall resolved: ([\d.]+)%\*\*")


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=SRC, capture_output=True, text=True, **kwargs)


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def refuse_if_dirty() -> None:
    status = git("status", "--porcelain", *TRACKED_PATHS).stdout.strip()
    if status:
        print("Refusing to run: data/, output/, or src/generate_data.py already "
              "have uncommitted changes. Commit, stash, or discard them first --\n")
        print(status)
        sys.exit(1)


def restore_tracked() -> None:
    git("checkout", "--", *TRACKED_PATHS)


def measure_one_seed(seed: int) -> float:
    env = {"RECON_SEED": str(seed)}
    import os
    full_env = {**os.environ, **env}
    for step in ["generate_data.py", "reconcile.py", "failure_injection_demo.py"]:
        result = subprocess.run([sys.executable, step], cwd=SRC, env=full_env,
                                 capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"{step} failed under seed={seed}:\n{result.stderr}")
    result = subprocess.run([sys.executable, "report.py"], cwd=SRC, env=full_env,
                             capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"report.py failed under seed={seed}:\n{result.stderr}")

    text = REPORT.read_text()
    match = RESOLVED_RE.search(text)
    if not match:
        raise RuntimeError(f"Could not find 'Overall resolved' in {REPORT} for seed={seed}")
    return float(match.group(1))


def main() -> None:
    seeds = [int(s) for s in sys.argv[1:]] or DEFAULT_SEEDS

    refuse_if_dirty()
    results: dict[int, float] = {}
    try:
        for seed in seeds:
            print(f"seed={seed} ... ", end="", flush=True)
            pct = measure_one_seed(seed)
            results[seed] = pct
            print(f"{pct}% resolved")
    finally:
        restore_tracked()
        print("\ndata/ and output/ restored to their committed state.")

    if results:
        values = list(results.values())
        print(f"\nRange across {len(values)} seed(s): {min(values)}%-{max(values)}%")


if __name__ == "__main__":
    main()
