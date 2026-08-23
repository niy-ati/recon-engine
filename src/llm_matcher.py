"""
Raw model-calling logic. No gating decision lives here -- validation_gate.py
owns that, and reconcile.py never imports this module directly.

Picks the best candidate from a pre-narrowed shortlist of order_ids and
explains why in one sentence. Never invents a match outside the shortlist.

Fallback order:
  1. Ollama, local (http://localhost:11434) -- free, open-weight, no
     credentials, no data leaving the machine.
  2. Deterministic stand-in -- used if Ollama isn't running.

Output schema (candidate_id, confidence, reason) is validated before use;
a malformed response or a candidate outside the shortlist is rejected.
"""
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

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
    auto_applied: bool  # always False coming out of this module -- see validation_gate.py
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
    Tries Ollama, then the deterministic stand-in. auto_applied is always
    False on the returned result -- deciding that is validation_gate.py's
    job, not this module's."""
    if not shortlist:
        return ArbiterResult(None, 0.0, "No candidates in shortlist.", False)

    result = _call_ollama(ledger_narration, shortlist)
    if result is not None:
        return result

    return _stand_in_arbiter(ledger_narration, shortlist)


if __name__ == "__main__":
    r = call_llm_arbiter(
        ledger_narration="pymt rcvd Customer26 ord#1036 thx",
        shortlist=["order_1036", "order_1063"],
    )
    print(f"candidate={r.candidate_id} confidence={r.confidence} tier={r.tier}")
    print(f"reason: {r.reason}")
    print(">> Raw arbiter output -- ungated. See validation_gate.py for the auto-apply decision.")
