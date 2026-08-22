"""
The only place an LLM touches this pipeline.

reconcile.py's deterministic passes narrow each unmatched ledger row to a
shortlist of 1-3 plausible order_ids using difflib similarity and numeric
order-id extraction. This module's only job is to pick the best candidate
from that shortlist and explain why in one sentence -- never to invent a
match outside it, never to move money, never to change a status without a
human seeing it first.

Fallback order:
  1. Ollama, local (http://localhost:11434) -- free, open-weight, no
     credentials required, no data leaving the machine.
  2. Deterministic stand-in -- keeps the confidence-gate contract
     inspectable even if Ollama isn't running.

Hard boundaries, unchanged by which tier answers:
  - Confidence below CONFIDENCE_AUTO_ACCEPT is never auto-applied.
  - The model only sees the narrowed shortlist, never the full ledger or
    settlement tables.
  - Output schema (candidate_id, confidence, reason) is validated before
    use. A malformed response, or a candidate_id outside the shortlist it
    was given, is routed to human review, not retried or trusted.
  - AUTO_APPLY_TRUSTED_TIERS controls which tiers may auto-apply at all,
    independent of confidence. Testing qwen2.5:0.5b via Ollama on a
    narration with no real distinguishing signal showed it defaults to
    whichever candidate is listed first while still reporting confidence
    >=0.90 -- a positional-bias failure, not a hypothetical one. Ollama is
    excluded from that set as a result, and nothing else is in it yet, so
    no tier auto-applies today. A tier can be added once it's shown to be
    reliably calibrated.
"""
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

CONFIDENCE_AUTO_ACCEPT = 0.90

# Tiers allowed to auto-apply regardless of reported confidence. A
# confidence from an untrusted tier is still reported and compared to the
# threshold, but auto_applied is forced False. Empty because no tier has
# been shown reliable enough yet.
AUTO_APPLY_TRUSTED_TIERS = set()

OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:0.5b")

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "candidate_id": {"type": "string"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["candidate_id", "confidence", "reason"],
}


@dataclass
class ArbiterResult:
    candidate_id: str | None
    confidence: float
    reason: str
    auto_applied: bool
    tier: str = "unknown"  # "ollama:<model>" | "stand-in"


def _build_prompt(ledger_narration: str, shortlist: list[str]) -> str:
    return (
        f"Given this ledger narration: {ledger_narration!r}\n"
        f"and these candidate order IDs: {shortlist!r}\n"
        f"which one does the narration most likely refer to? Pick exactly "
        f"one from the candidate list -- never a value outside it.\n"
        f'Respond with ONLY a JSON object: '
        f'{{"candidate_id": "...", "confidence": 0.0, "reason": "..."}}'
    )


def _parse_arbiter_json(text: str, shortlist: list[str], source: str) -> ArbiterResult:
    try:
        parsed = json.loads(text)
        candidate_id = parsed["candidate_id"]
        confidence = float(parsed["confidence"])
        reason = str(parsed["reason"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return ArbiterResult(None, 0.0, f"[{source}] Response was not valid JSON in the expected schema: {text!r}"[:300], False, tier=source)

    if candidate_id not in shortlist:
        return ArbiterResult(None, 0.0, f"[{source}] Picked {candidate_id!r}, not in its own shortlist {shortlist} -- rejected, not trusted.", False, tier=source)

    return ArbiterResult(candidate_id, confidence, f"[{source}] {reason}", False, tier=source)


def _stand_in_arbiter(ledger_narration: str, shortlist: list[str]) -> ArbiterResult:
    best = shortlist[0]
    return ArbiterResult(
        best, 0.72,
        f"[stand-in, no model reachable] Narration loosely resembles order {best}; not a strong enough signal.",
        False, tier="stand-in",
    )


def _call_ollama(ledger_narration: str, shortlist: list[str]) -> ArbiterResult | None:
    """Returns None if Ollama isn't reachable, distinct from a low-confidence
    result (which means Ollama answered but wasn't sure)."""
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": _build_prompt(ledger_narration, shortlist)}],
        "format": RESPONSE_SCHEMA,
        "stream": False,
        "options": {"temperature": 0},  # deterministic output for a repeatable judgment
    }).encode("utf-8")

    req = urllib.request.Request(
        OLLAMA_URL, data=body, method="POST",
        headers={"content-type": "application/json"},
    )
    try:
        # 100s covers a cold model load (~80s on this hardware); a warm
        # call takes ~2-5s. A short timeout would misdiagnose a slow but
        # working cold start as "Ollama not running."
        with urllib.request.urlopen(req, timeout=100) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ConnectionRefusedError, TimeoutError):
        return None

    text = payload.get("message", {}).get("content", "")
    return _parse_arbiter_json(text, shortlist, source=f"ollama:{OLLAMA_MODEL}")


def call_llm_arbiter(ledger_narration: str, shortlist: list[str]) -> ArbiterResult:
    """Picks the best candidate from `shortlist` for `ledger_narration`.
    Tries Ollama, then the deterministic stand-in."""
    if not shortlist:
        return ArbiterResult(None, 0.0, "No candidates in shortlist.", False)

    result = _call_ollama(ledger_narration, shortlist)
    if result is not None:
        return result

    return _stand_in_arbiter(ledger_narration, shortlist)


def resolve_with_gate(ledger_narration: str, shortlist: list[str]) -> ArbiterResult:
    result = call_llm_arbiter(ledger_narration, shortlist)
    tier_name = result.tier.split(":")[0]
    tier_trusted = tier_name in AUTO_APPLY_TRUSTED_TIERS
    if result.candidate_id is not None and result.confidence >= CONFIDENCE_AUTO_ACCEPT and tier_trusted:
        result.auto_applied = True
    else:
        result.auto_applied = False
        if result.candidate_id is not None and result.confidence >= CONFIDENCE_AUTO_ACCEPT and not tier_trusted:
            result.reason += " [held despite high confidence -- this tier is not trusted for auto-apply, see AUTO_APPLY_TRUSTED_TIERS]"
    return result


if __name__ == "__main__":
    r = resolve_with_gate(
        ledger_narration="pymt rcvd Customer26 ord#1036 thx",
        shortlist=["order_1036", "order_1063"],
    )
    print(f"candidate={r.candidate_id} confidence={r.confidence} auto_applied={r.auto_applied}")
    print(f"reason: {r.reason}")
    if not r.auto_applied:
        print(">> GATE HELD: routed to human review queue, NOT silently applied.")
