"""
A held-out-style precision/recall evaluation for reconcile() itself, the
same rigor arbiter_eval.py already applies to Pass 4 alone, run here
across the full synthetic batch instead of a handful of hand-picked cases.

Ground truth comes from data/ground_truth.csv, written by generate_data.py
at the same time it writes the batch -- one hand-verified expected outcome
per row, keyed by reconcile.py's own match_key scheme. This file never
re-derives reconcile.py's decision logic to check it; it only compares
reconcile()'s actual output against a label recorded independently, at
generation time, before reconcile() ever ran.

Two kinds of ground truth row:
  - a single expected label (or a pipe-joined set, for the OCR-typo case,
    whose outcome depends on a live arbiter call and is not one fixed
    value even when the pipeline is working correctly)
  - a "group" pair (the duplicate-settlement case): which of the two rows
    gets flagged DUPLICATE depends on shuffled file order, not anything
    about the row itself, so the pair is scored together -- exactly one
    MATCHED, one DUPLICATE -- rather than as two independent predictions.

Run with `python src/reconciliation_eval.py`. Needs data/ground_truth.csv
to exist (generate_data.py writes it every run) and reads the same
data/*.csv reconcile() already reads -- no live model call is required for
the deterministic majority of rows; a warm Ollama only affects the OCR-typo
case's exact outcome, already scored against an allowed set for that
reason.
"""
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from reconcile import reconcile

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
EVAL_DIR = Path(__file__).resolve().parent.parent / "eval"


def load_ground_truth(path: Path = DATA_DIR / "ground_truth.csv") -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def canonical_outcome(result: dict) -> str:
    """The one label a ground-truth row can be checked against: the
    category when reconcile() assigned one, else the status. Every branch
    in generate_data.py's ground truth was hand-verified against this same
    formula, not against reconcile.py's raw status/category split."""
    return result["category"] or result["status"]


def score(results: list[dict], ground_truth: list[dict]) -> dict:
    actual_by_key = {r["match_key"]: canonical_outcome(r) for r in results}

    singles = [g for g in ground_truth if not g["group"]]
    groups = defaultdict(list)
    for g in ground_truth:
        if g["group"]:
            groups[g["group"]].append(g)

    missing_keys = [g["match_key"] for g in ground_truth if g["match_key"] not in actual_by_key]

    single_results = []
    for g in singles:
        actual = actual_by_key.get(g["match_key"])
        allowed = set(g["expected"].split("|"))
        single_results.append({
            "match_key": g["match_key"], "expected": g["expected"],
            "actual": actual, "correct": actual in allowed,
        })

    group_results = []
    for group_id, members in groups.items():
        actuals = sorted(actual_by_key.get(m["match_key"]) for m in members)
        correct = actuals == sorted(["DUPLICATE", "MATCHED"])
        group_results.append({
            "group": group_id, "match_keys": [m["match_key"] for m in members],
            "actual": actuals, "correct": correct,
        })

    # Per-label precision/recall over the single-outcome rows only -- a
    # group's correctness is a pairwise invariant, not a single predicted
    # label, so it is reported separately rather than folded into these
    # per-label counts.
    labels = sorted({r["expected"] for r in single_results if "|" not in r["expected"]})
    per_label = {}
    for label in labels:
        tp = sum(1 for r in single_results if r["expected"] == label and r["actual"] == label)
        fp = sum(1 for r in single_results if r["expected"] != label and r["actual"] == label)
        fn = sum(1 for r in single_results if r["expected"] == label and r["actual"] != label)
        precision = round(tp / (tp + fp), 4) if (tp + fp) else None
        recall = round(tp / (tp + fn), 4) if (tp + fn) else None
        f1 = round(2 * precision * recall / (precision + recall), 4) if precision and recall and (precision + recall) else None
        per_label[label] = {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}

    single_correct = sum(1 for r in single_results if r["correct"])
    group_correct = sum(1 for r in group_results if r["correct"])
    # A pair counts as 2 rows for a fair row-level denominator against
    # the single-outcome rows -- both twins must land right for the pair
    # to be correct, so this never overstates accuracy relative to
    # scoring every row independently.
    total_rows = len(single_results) + 2 * len(group_results)
    total_correct = single_correct + 2 * group_correct

    return {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_ground_truth_rows": len(ground_truth),
        "overall_accuracy_pct": round(100 * total_correct / total_rows, 2) if total_rows else None,
        "missing_from_results": missing_keys,
        "single_rows": {
            "count": len(single_results),
            "correct": single_correct,
            "accuracy_pct": round(100 * single_correct / len(single_results), 2) if single_results else None,
            "results": single_results,
        },
        "duplicate_pairs": {
            "count": len(group_results),
            "correct": group_correct,
            "accuracy_pct": round(100 * group_correct / len(group_results), 2) if group_results else None,
            "results": group_results,
        },
        "per_label": per_label,
    }


def render_report(report: dict) -> str:
    lines = [
        "# Reconciliation accuracy evaluation",
        "",
        f"Run at {report['run_at']}, {report['total_ground_truth_rows']} ground-truth rows.",
        "",
        f"**Overall accuracy: {report['overall_accuracy_pct']}%** across every labeled row in the batch, "
        f"not a handful of hand-picked cases.",
        "",
    ]

    if report["missing_from_results"]:
        lines.append(
            f"**{len(report['missing_from_results'])} ground-truth match_key(s) never appeared in "
            f"reconcile()'s output** -- a real defect (data/reconcile.py drift), not a scoring gap: "
            f"{report['missing_from_results']}"
        )
        lines.append("")

    s = report["single_rows"]
    lines.append(f"**Single-outcome rows: {s['accuracy_pct']}% ({s['correct']}/{s['count']})**")
    d = report["duplicate_pairs"]
    lines.append(
        f"**Duplicate-pair symmetry: {d['accuracy_pct']}% ({d['correct']}/{d['count']})** -- "
        f"exactly one of each pair correctly flagged DUPLICATE, the other MATCHED."
    )
    lines.append("")

    lines.append("## Per-category precision / recall")
    lines.append("")
    lines.append("| category | tp | fp | fn | precision | recall | f1 |")
    lines.append("|---|---|---|---|---|---|---|")
    for label, m in report["per_label"].items():
        lines.append(f"| {label} | {m['tp']} | {m['fp']} | {m['fn']} | {m['precision']} | {m['recall']} | {m['f1']} |")
    lines.append("")

    wrong_singles = [r for r in s["results"] if not r["correct"]]
    if wrong_singles:
        lines.append("## Misclassified rows")
        lines.append("")
        for r in wrong_singles:
            lines.append(f"- `{r['match_key']}`: expected {r['expected']}, got {r['actual']}")
        lines.append("")

    wrong_groups = [r for r in d["results"] if not r["correct"]]
    if wrong_groups:
        lines.append("## Misclassified duplicate pairs")
        lines.append("")
        for r in wrong_groups:
            lines.append(f"- `{r['group']}` ({r['match_keys']}): got {r['actual']}, expected one DUPLICATE + one MATCHED")
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    results = reconcile()
    ground_truth = load_ground_truth()
    report = score(results, ground_truth)
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    (EVAL_DIR / "reconciliation_eval_results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown = render_report(report)
    (EVAL_DIR / "reconciliation_eval_report.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"\nWritten to {EVAL_DIR}/reconciliation_eval_results.json and {EVAL_DIR}/reconciliation_eval_report.md")


if __name__ == "__main__":
    main()
