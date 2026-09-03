"""
A persisted, re-runnable evaluation harness for Pass 4's arbiter --
formalizing the "found live" narrative claims already scattered across
this codebase (validation_gate.py's positional-bias finding,
ai_judgment_demo.py's context-judgment case, notes/PITCH_NOTES.md's
"4/4 OCR-typo cases correctly picked") into structured, repeatable test
cases with the raw per-case result saved to disk -- not just a claim
frozen in a comment from whenever it was last run by hand.

Modeled directly on Razorpay's own published evaluation philosophy
(razorpay.com/blog/the-winner-doesnt-take-it-all): reject public
benchmarks -- they're contaminated (leak into training data) and don't
resemble this system's actual shortlist-narrowing shape anyway -- build a
small domain-specific harness instead, store raw results so a later
re-score never needs to spend another model call, and re-run whenever the
local model changes rather than trust one frozen finding forever. Their
own framework also stores "raw votes" for exactly this reason; CASES
below is this project's equivalent, checked into the repo as data, not
buried in prose.

Deliberately NOT copying every element of that post's methodology: it
recommends clustered confidence intervals across repeated samples, which
adds no real signal here, since llm_matcher.py already calls Ollama at
temperature=0 -- the same case returns the identical output every time a
model is warm, so repeat-sampling would measure infrastructure flakiness,
not genuine model variance. For a narrow local model doing one job
(picking from a 2-3 item shortlist), case coverage and honest calibration
checking (does confidence actually track correctness?) are the real
signal, not a confidence interval over noise that isn't there.

Run with `python src/arbiter_eval.py`. Needs a live, warm Ollama to be a
genuine measurement of the model -- with Ollama unreachable, every case
silently falls back to llm_matcher._stand_in_arbiter (always picks
shortlist[0]), which this harness detects and flags rather than reporting
a misleading accuracy number for a fallback nobody is being asked to
trust in the first place.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from llm_matcher import call_llm_arbiter, OLLAMA_MODEL

EVAL_DIR = Path(__file__).resolve().parent.parent / "eval"

# A case with no real correct answer (see "positional_bias_no_signal"
# below) uses this sentinel instead of a real candidate_id -- scored
# separately, as a calibration check, never averaged into accuracy.
AMBIGUOUS = "__ambiguous__"


@dataclass
class EvalCase:
    id: str
    narration: str
    shortlist: list[str]
    expected: str  # a real candidate_id, hand-verified against the narration, or AMBIGUOUS
    note: str      # why this case exists and what it's actually testing


# Fixed, deterministic order -- "deterministic item selection ensuring
# fair comparison" is one of the two properties Razorpay's own framework
# names as load-bearing (the other, clustered confidence intervals, is
# the one this harness deliberately skips -- see module docstring). Every
# case here is real, not invented for this file: each is either lifted
# directly from an existing adversarial script in this codebase or
# documents a plain case Pass 4 is already known to get right, so this
# measures a genuine mix, not a stacked deck in either direction.
CASES: list[EvalCase] = [
    EvalCase(
        id="ocr_typo_digit_as_letter",
        narration="pymt rcvd 3 ord#l171 thx",  # 'l' (lowercase L) for '1' -- a real
        # OCR/typo shape this batch's own FUZZY_MATCH_NEEDS_REVIEW rows share (see
        # settlement_qa.py's _recurring_patterns docstring).
        shortlist=["order_1171", "order_1032"],
        expected="order_1171",
        note="Single clean shortlist, one digit-as-letter typo -- the easy case Pass 4 "
             "is already known to get right (notes/PITCH_NOTES.md: '4/4 OCR-typo cases "
             "correctly picked').",
    ),
    EvalCase(
        id="positional_bias_no_signal",
        narration="payment received, thank you",  # no identifying signal for either candidate
        shortlist=["order_2001", "order_2002"],
        expected=AMBIGUOUS,
        note="Nothing in this narration ties it to either candidate -- there is no "
             "correct pick to score against. The honest behavior is a low-confidence "
             "result the gate holds regardless of which candidate comes back. What "
             "actually happened live, the finding validation_gate.py's "
             "AUTO_APPLY_TRUSTED_TIERS comment is built on: the model picked "
             "shortlist[0] anyway and reported >=90% confidence. This case checks for "
             "exactly that failure mode -- high confidence with genuinely no signal --  "
             "not for a 'correct' candidate that doesn't exist.",
    ),
    EvalCase(
        id="context_judgment_two_orders_one_narration",
        narration=(
            "order 8001 payment cancelled and refunded in full, "
            "order 8002 payment received and confirmed"
        ),
        shortlist=["order_8001", "order_8002"],
        expected="order_8002",
        note="Same case as ai_judgment_demo.py, formalized as data here -- reading "
             "which of two named orders the narration actually confirms as paid, not "
             "just extracting a number. ai_judgment_demo.py's own docstring: two "
             "differently-phrased attempts at this during development picked wrong "
             "outright, and the one that picked right still gave an incoherent reason.",
    ),
]


def run_eval(cases: list[EvalCase] = CASES) -> dict:
    """Runs every case once against the real arbiter -- never through
    validation_gate.resolve_with_gate(), since the gate's own pass/fail
    behavior is already covered by test_validation_gate.py; this measures
    the model's raw judgment underneath it. Every raw per-case result is
    both returned and written to disk by main() -- "raw vote storage
    allowing re-scoring without re-running," so a future change to how
    this is analyzed doesn't need a fresh model call for cases already run."""
    per_case = []
    for case in cases:
        result = call_llm_arbiter(case.narration, case.shortlist)
        correct = None if case.expected == AMBIGUOUS else result.candidate_id == case.expected
        per_case.append({
            "id": case.id, "narration": case.narration, "shortlist": case.shortlist,
            "expected": case.expected, "picked": result.candidate_id,
            # rounded -- a local model's raw float (e.g. 0.9999999999999999)
            # is real but reads as noise, not signal, in a report meant for
            # a person to read
            "confidence": round(result.confidence, 3), "tier": result.tier,
            "reason": result.reason, "correct": correct,
        })

    scored = [c for c in per_case if c["correct"] is not None]
    correct_confidences = [c["confidence"] for c in scored if c["correct"]]
    wrong_confidences = [c["confidence"] for c in scored if not c["correct"]]

    ambiguous_cases = [c for c in per_case if c["correct"] is None]
    # The specific failure this whole harness exists to catch: high
    # confidence reported where no real signal exists at all.
    overconfident_on_no_signal = [c for c in ambiguous_cases if c["confidence"] >= 0.90]

    stand_in_count = sum(1 for c in per_case if c["tier"] == "stand-in")

    return {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": OLLAMA_MODEL,
        "cases": per_case,
        "scored_case_count": len(scored),
        "accuracy_pct": round(100 * sum(1 for c in scored if c["correct"]) / len(scored), 1) if scored else None,
        "mean_confidence_when_correct": round(sum(correct_confidences) / len(correct_confidences), 3) if correct_confidences else None,
        "mean_confidence_when_wrong": round(sum(wrong_confidences) / len(wrong_confidences), 3) if wrong_confidences else None,
        "ambiguous_case_count": len(ambiguous_cases),
        "overconfident_on_no_signal_count": len(overconfident_on_no_signal),
        "stand_in_fallback_count": stand_in_count,  # >0 means Ollama wasn't reachable for at least one case
    }


def render_report(report: dict) -> str:
    lines = [
        "# Pass 4 arbiter evaluation",
        "",
        f"Run at {report['run_at']} against `{report['model']}`.",
        "",
    ]
    if report["stand_in_fallback_count"]:
        lines.append(
            f"**{report['stand_in_fallback_count']} of {len(report['cases'])} case(s) fell back to the "
            f"deterministic stand-in (Ollama unreachable) -- these are NOT a real model evaluation.** "
            f"Start Ollama and re-run for a genuine result."
        )
        lines.append("")

    if report["accuracy_pct"] is not None:
        lines.append(f"**Accuracy on scored cases:** {report['accuracy_pct']}% "
                     f"({report['scored_case_count']} case(s) with a real correct answer).")
        lines.append(
            f"**Mean confidence, correct vs wrong:** "
            f"{report['mean_confidence_when_correct']} vs {report['mean_confidence_when_wrong']} -- "
            f"a well-calibrated model reports LOWER confidence when wrong; these numbers being "
            f"close (or inverted) is itself evidence against trusting confidence as a signal, "
            f"same finding as the ambiguous case below."
        )
        lines.append("")

    lines.append(
        f"**Ambiguous (no ground truth) cases:** {report['ambiguous_case_count']}, of which "
        f"**{report['overconfident_on_no_signal_count']}** reported >=90% confidence anyway -- "
        f"the exact positional-bias failure `validation_gate.AUTO_APPLY_TRUSTED_TIERS` is empty "
        f"because of."
    )
    lines.append("")
    lines.append("## Per-case detail")
    lines.append("")
    for c in report["cases"]:
        verdict = "AMBIGUOUS (no ground truth)" if c["correct"] is None else ("CORRECT" if c["correct"] else "WRONG")
        lines.append(f"### `{c['id']}` -- {verdict}")
        lines.append(f"- narration: {c['narration']!r}")
        lines.append(f"- shortlist: {c['shortlist']}")
        lines.append(f"- expected: {c['expected']}, picked: {c['picked']} (confidence {c['confidence']}, tier {c['tier']})")
        lines.append(f"- model's own reason: {c['reason']}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    report = run_eval()
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    (EVAL_DIR / "arbiter_eval_results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown = render_report(report)
    (EVAL_DIR / "arbiter_eval_report.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"\nWritten to {EVAL_DIR}/arbiter_eval_results.json and {EVAL_DIR}/arbiter_eval_report.md")


if __name__ == "__main__":
    main()
