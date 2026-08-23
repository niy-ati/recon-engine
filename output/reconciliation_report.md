# Settlement Reconciliation Report

**Settlement data source:** synthetic (generated, see generate_data.py)

**Total rows processed:** 88

**Throughput:** 88 rows in 5.50s (16.0 rows/sec) -- includes LLM arbiter call(s); Pass 1/2/2.5/2.75 alone are sub-second, the arbiter call dominates this number when present

**Clean deterministic match:** 77.3%
**Matched with explained variance:** 2.3%
**Unambiguous exact reference (deterministic, no LLM call):** 1.1%
**Resolved via learned pattern (human-confirmed before):** 0.0%
**AI-assisted, auto-applied (confidence >= 0.90 gate):** 0.0%
**Fuzzy-matched, flagged for human review:** 2.3%
**Unresolved exceptions:** 17.0%

**Overall resolved: 83.0%** (industry baseline for manual VLOOKUP reconciliation: ~51%)

## Exceptions by category

| Category | Count | Meaning |
|---|---|---|
| DUPLICATE | 6 | Same settlement reported twice, one real bank credit |
| PARTIAL_PAYMENT | 2 | Refund netted into settlement -- explained, not an error |
| FUZZY_MATCH_NEEDS_REVIEW | 2 | Narration-based candidate match below auto-accept confidence -- human must confirm |
| ON_HOLD_BY_RAZORPAY | 3 | Razorpay's own API reports on_hold=true for this settlement -- known, held for a reason, not a lost transaction or a normal delay |
| UNEXPLAINED | 2 | No counterpart found anywhere -- needs manual investigation |
| AFA_MANDATE_HOLD | 4 | Subscription charge blocked by RBI e-mandate AFA threshold (>Rs.15,000) -- needs compliant step-up re-auth, not a blind retry |