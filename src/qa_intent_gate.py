"""
Confidence+tier gate for qa_intent_router.py. settlement_qa.py imports
route_gated from here only -- mirrors validation_gate.py's own separation
for Pass 4's arbiter (reconcile.py never imports llm_matcher directly).

A classification is trusted enough to act on only if both hold:
  1. confidence >= CONFIDENCE_THRESHOLD
  2. the tier that produced it is in TRUSTED_TIERS

Everything else falls through to settlement_qa.py's existing honest
"I don't have a way to answer that," same as an unrecognized question
today -- never a guess.

TRUSTED_TIERS is empty for the same reason validation_gate.py's
AUTO_APPLY_TRUSTED_TIERS is empty, evidenced independently here: live
tests against the real local model this project actually runs
(qwen2.5:0.5b), against the exact entity-free question set
qa_intent_router.py actually offers, found:
  - confidence was 1.0 on every single response, right AND wrong, so it
    carries no calibration signal for this model on this task at all
    (worse than the positional-bias case validation_gate.py already
    documents for Pass 4's arbiter, where confidence at least varied);
  - it only got 4 of 8 real test questions right, with "resolution_rate"
    acting as a visible wrong-answer attractor 3 of those 4 times
    (picked for an unrelated "how's the weather" question, a vague
    "what's up with that order from yesterday," and a real but
    unrecognized "why do some settlements show up twice");
  - adding few-shot examples to the prompt raised accuracy to 5 of 8 but
    flipped a previously-correct answer ("how many are open") to wrong in
    the same run -- prompt tweaks visibly move which questions fail, not
    just how many, which is not something worth chasing further here.
This gate exists and is wired end to end so the feature activates the
moment a specific tier is shown, empirically, not to share this failure
mode -- add it to TRUSTED_TIERS then, the same way this codebase would
add one to validation_gate.AUTO_APPLY_TRUSTED_TIERS.
"""
import qa_intent_router
from validation_gate import CONFIDENCE_AUTO_ACCEPT

CONFIDENCE_THRESHOLD = CONFIDENCE_AUTO_ACCEPT  # same bar Pass 4 uses, for consistency

TRUSTED_TIERS: set[str] = set()


def route_gated(question: str) -> str | None:
    """Returns a canonical, already-recognized question to re-answer
    through settlement_qa.py's deterministic path, or None if nothing
    should be trusted -- Ollama unreachable, low confidence, an
    "unknown" classification, or (today, always) an untrusted tier.
    Never returns a guess.

    Skips the Ollama call entirely when TRUSTED_TIERS is empty, rather
    than making the real network round-trip and discarding its result --
    a real latency bug, found live: with nothing on the trusted list, no
    result from any tier could ever pass the check below regardless of
    what the model says, so the call was pure wasted time (2-5s warm, up
    to ~80s cold per llm_matcher.py's own docs) on every question that
    missed the deterministic keyword match above it. That became
    painfully visible once questions started arriving as natural speech
    instead of typed text -- spoken phrasing rarely matches a keyword
    shape, so this fallback fired on nearly every voice turn. Behavior is
    identical either way; this only removes the pointless wait."""
    if not TRUSTED_TIERS:
        return None
    routed = qa_intent_router.route(question)
    if routed is None:
        return None
    if routed.confidence < CONFIDENCE_THRESHOLD:
        return None
    tier_name = routed.tier.split(":")[0]
    if tier_name not in TRUSTED_TIERS:
        return None
    return qa_intent_router.to_canonical_question(routed)


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "what's going on with the duplicates"
    result = route_gated(q)
    if result is None:
        print(">> GATE HELD: not trusted -- settlement_qa.py falls through to its honest fallback.")
    else:
        print(f">> canonical question: {result!r}")
