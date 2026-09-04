"""
Reproduces the held-out multi-seed validation quoted in README.md and
src/generate_data.py's own docstring, instead of asking anyone to just
trust five numbers typed into a comment.

Runs the full pipeline (generate_data.py -> reconcile.py ->
failure_injection_demo.py -> report.py, the same steps run_all.py runs)
once per seed, against a genuinely different ~525-row batch each time,
and reports the "Overall resolved" figure the pipeline itself computed --
never recomputed or estimated here.

Restores data/ and output/ to their committed state when done, or on any
error, via `git checkout --`. Refuses to run at all if either already has
uncommitted changes, so it can never discard real work.

Each step gets a bounded STEP_TIMEOUT_SECONDS instead of running forever.
This exists because of a real, investigated incident: a full seed=2026 run
once appeared to hang mid-sweep, past this script's own outer time budget.
Re-run individually and chained, seed=2026 completed cleanly every single
time afterward (30s, matching every other seed) -- so it wasn't a
deterministic bug in that seed's data. The likelier explanation is in
llm_matcher.py's own comment: a cold Ollama model load measured at up to
~80s on this hardware, against a client timeout of 100s -- run seed=2026
sixth in a row, after several back-to-back pipelines, and a cold reload
landing there is entirely plausible, no bug required. STEP_TIMEOUT_SECONDS
is set above that 100s ceiling so a legitimate cold start is never
misreported as a failure; it exists to bound a genuine stall, not to
second-guess a slow-but-correct cold load.

    python extras/seed_sweep.py                  # the five seeds quoted in README.md
    python extras/seed_sweep.py 1 2 3             # any seeds you want to check yourself
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
REPORT = ROOT / "output" / "reconciliation_report.md"
TRACKED_PATHS = ["data", "output"]
DEFAULT_SEEDS = [42, 7, 21, 99, 555]
STEP_TIMEOUT_SECONDS = 130  # above llm_matcher.py's own 100s Ollama client timeout

RESOLVED_RE = re.compile(r"\*\*Overall resolved: ([\d.]+)%\*\*")


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def refuse_if_dirty() -> None:
    status = git("status", "--porcelain", *TRACKED_PATHS).stdout.strip()
    if status:
        print("Refusing to run: data/ or output/ already have uncommitted "
              "changes. Commit, stash, or discard them first --\n")
        print(status)
        sys.exit(1)


def restore_tracked() -> None:
    git("checkout", "--", *TRACKED_PATHS)


def measure_one_seed(seed: int) -> float:
    full_env = {**os.environ, "RECON_SEED": str(seed)}
    for step in ["generate_data.py", "reconcile.py", "failure_injection_demo.py", "report.py"]:
        # --fresh-batch on report.py only: each seed draws every ID from a
        # different point in its own random stream, so five seeds run back
        # to back would otherwise persist as five disjoint batches that
        # never overwrite each other -- see db.reset_batch()'s own
        # docstring for the real accumulation bug this prevents (this
        # script, run without this flag, was one of its real causes).
        args = [sys.executable, step] + (["--fresh-batch"] if step == "report.py" else [])
        try:
            result = subprocess.run(args, cwd=SRC, env=full_env,
                                     capture_output=True, text=True, timeout=STEP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"{step} exceeded {STEP_TIMEOUT_SECONDS}s under seed={seed} -- most likely "
                "a stalled Ollama call, not a deterministic problem with this seed's data "
                "(see the module docstring). Re-run this seed alone to check."
            )
        if result.returncode != 0:
            raise RuntimeError(f"{step} failed under seed={seed}:\n{result.stderr}")

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
            try:
                pct = measure_one_seed(seed)
            except RuntimeError as e:
                print(f"FAILED -- {e}")
                continue
            results[seed] = pct
            print(f"{pct}% resolved")
    finally:
        restore_tracked()
        print("\ndata/ and output/ restored to their committed state.")

    if results:
        values = list(results.values())
        print(f"\nRange across {len(values)}/{len(seeds)} seed(s) that completed: "
              f"{min(values)}%-{max(values)}%")
    if len(results) < len(seeds):
        sys.exit(1)


if __name__ == "__main__":
    main()
