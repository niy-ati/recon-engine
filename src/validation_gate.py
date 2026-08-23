"""
Confidence gate. reconcile.py imports resolve_with_gate from here only --
never call_llm_arbiter from llm_matcher.py directly -- so no code path in
this codebase reaches an ungated arbiter result.

This is the platform-level validation layer, independent of agent logic,
that Razorpay's own Agent Studio guardrails require: "every agent action
passes through a platform-level validation layer before execution"
(razorpay.com/blog/razorpay-agent-studio-principles-guardrails-and-merchant-control).

A result auto-applies only if both hold:
  1. confidence >= CONFIDENCE_AUTO_ACCEPT
  2. the tier that produced it is in AUTO_APPLY_TRUSTED_TIERS

Everything else returns with auto_applied=False, routed to human review.
"""
from llm_matcher import call_llm_arbiter

CONFIDENCE_AUTO_ACCEPT = 0.90

# Empty: Ollama showed a positional-bias failure (defaults to whichever
# candidate is listed first while still reporting confidence >=0.90) and
# is excluded as a result. Add a tier here once it's shown reliable.
AUTO_APPLY_TRUSTED_TIERS = set()


def resolve_with_gate(ledger_narration: str, shortlist: list[str]):
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
