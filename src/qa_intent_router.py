"""
Raw model-calling logic for settlement_qa.py's free-text fallback. No
gating decision lives here -- qa_intent_gate.py owns that, mirroring how
llm_matcher.py/validation_gate.py split Pass 4's arbiter, and
settlement_qa.py imports the gate module only, never this one directly.

Classifies a question into one of a small, closed set of shapes
settlement_qa.py's deterministic handlers already answer, then reformulates
it into the exact canonical phrasing those handlers already recognize --
this module never generates an answer or a financial fact, only picks
which door to knock on.

Only entity-free question shapes are offered here. Verified directly
against settlement_qa.py's own dispatch order (see _answer(), and the
regression test in test_settlement_qa.py that pins this): any question
naming an order_id, settlement_id, or category is always fully answered by
_find_order / _find_settlement / _category_count / _similar_orders /
_resolution_guidance before _answer() ever reaches this fallback --
_category_count in particular answers unconditionally whenever a category
is present at all, and the two order/settlement lookups are equally
unconditional. So this fallback is only ever invoked with none of those
three present, and offering entity-dependent intents here would be dead
code that can never actually be selected -- removed rather than kept
"for later," per this project's own no-speculative-code standard.

Reuses llm_matcher.py's exact model-calling pattern: same Ollama endpoint,
same model, same JSON-schema-constrained output, same temperature 0. No
stand-in fallback (unlike llm_matcher.py's guess-candidate-zero): guessing
an intent out of several unrelated shapes has no safe default the way
guessing shortlist[0] does for a narrowed candidate list. If Ollama isn't
reachable, route() returns None and the caller falls through to the
existing honest "I don't have a way to answer that," unchanged.

Live-tested against the actual local model this project runs
(qwen2.5:0.5b), across real questions restricted to exactly this
entity-free intent set:
  - confidence was 1.0 on nearly every response, right AND wrong -- it
    carries close to no calibration signal for this model on this task
    (worse than the positional-bias case validation_gate.py already
    documents for Pass 4's arbiter, where confidence at least varied);
  - "resolution_rate" acted as a visible attractor default whenever the
    model was actually unsure, appearing for an unrelated "how's the
    weather" question;
  - adding few-shot examples to the prompt didn't reliably fix this --
    confidence started varying slightly, but a previously-correct
    classification ("how many are open") flipped wrong in the same test
    run, a net wash rather than an improvement.
See qa_intent_gate.py for what this means for whether any of it is
trusted today.
"""
import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from llm_matcher import OLLAMA_MODEL, OLLAMA_URL

# Every intent settlement_qa.py can answer with zero entity in the
# question -- see the module docstring for why entity-bearing shapes
# aren't offered here at all.
INTENTS = {
    "open_count": "how many rows are currently open, needing a decision",
    "resolution_rate": "the overall percentage or fraction resolved",
    "category_breakdown": "a breakdown of counts across every category",
    "confirmed_count": "how many rows have been confirmed by a human reviewer",
    "rejected_count": "how many rows have been rejected by a human reviewer",
    "needs_clarification_count": "how many open rows have a clarification note attached",
    "cash_value_overall": "overall cash position, or how much money is at risk",
    "unknown": "none of the above, or the question isn't about this reconciliation batch at all",
}

# The exact phrasing each intent maps to -- chosen to match
# settlement_qa.py's own trigger-phrase lists verbatim. Reformulating into
# one of these means the answer is produced by the same tested
# deterministic path as every question that matched a keyword directly --
# this router never answers anything itself.
CANONICAL_QUESTIONS = {
    "open_count": "how many are open",
    "resolution_rate": "what's my resolution rate",
    "category_breakdown": "exceptions by category",
    "confirmed_count": "how many have been confirmed",
    "rejected_count": "how many rejected",
    "needs_clarification_count": "how many rows need clarification",
    "cash_value_overall": "what's my cash position",
}


@dataclass
class RoutedIntent:
    intent: str
    confidence: float
    tier: str


RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "confidence": {"type": "number"},
    },
    "required": ["intent", "confidence"],
}


def _build_prompt(question: str) -> str:
    intent_lines = "\n".join(f'- "{name}": {desc}' for name, desc in INTENTS.items())
    return (
        "A merchant is asking a question in a chat widget about a payment "
        "settlement reconciliation batch. Classify the question into "
        f"exactly one of these intents:\n{intent_lines}\n\n"
        f"Question: {question!r}\n\n"
        "Respond with ONLY a JSON object: "
        '{"intent": "...", "confidence": 0.0}'
    )


def _parse_response(text: str) -> tuple[str, float] | None:
    try:
        parsed = json.loads(text)
        intent = parsed["intent"]
        confidence = float(parsed["confidence"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if intent not in INTENTS:
        return None
    return intent, confidence


def _call_ollama(question: str) -> tuple[str, float] | None:
    """Returns None if Ollama isn't reachable or the response didn't parse
    -- both are "couldn't classify," treated identically by the caller,
    never papered over with a guess."""
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": _build_prompt(question)}],
        "format": RESPONSE_SCHEMA,
        "stream": False,
        "options": {"temperature": 0},
    }).encode("utf-8")

    req = urllib.request.Request(
        OLLAMA_URL, data=body, method="POST",
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=100) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ConnectionRefusedError, TimeoutError):
        return None

    text = payload.get("message", {}).get("content", "")
    return _parse_response(text)


def route(question: str) -> RoutedIntent | None:
    """Classifies `question` into a RoutedIntent, or None if Ollama isn't
    reachable or its response didn't parse -- the caller (qa_intent_gate.py)
    treats that exactly like a low-confidence result: fall through to the
    existing honest "don't know," never a guess."""
    result = _call_ollama(question)
    if result is None:
        return None
    intent, confidence = result
    return RoutedIntent(intent=intent, confidence=confidence, tier=f"ollama:{OLLAMA_MODEL}")


def to_canonical_question(routed: RoutedIntent) -> str | None:
    """Turns a classified intent back into the exact phrasing
    settlement_qa.py's deterministic keyword matching already recognizes,
    so the real answer still comes from that tested path, never from this
    module. Returns None for "unknown" so the caller falls through to the
    honest fallback instead."""
    return CANONICAL_QUESTIONS.get(routed.intent)


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "why do some settlements show up twice"
    r = route(q)
    if r is None:
        print("Ollama unreachable or response unparseable -- would fall through to honest fallback.")
    else:
        print(f"intent={r.intent} confidence={r.confidence} tier={r.tier}")
        print(f"canonical -> {to_canonical_question(r)!r}")
        print("(raw, ungated -- see qa_intent_gate.py for whether this would actually be trusted)")
