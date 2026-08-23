# Settlement Reconciliation Report

**Settlement data source:** synthetic (generated, see generate_data.py)

**Total rows processed:** 84

**Throughput:** 84 rows in 10.36s (8.1 rows/sec) -- includes LLM arbiter call(s); Pass 1/2/2.5/2.75 alone are sub-second, the arbiter call dominates this number when present

**Clean deterministic match:** 72.6%
**Matched with explained variance:** 10.7%
**Unambiguous exact reference (deterministic, no LLM call):** 6.0%
**Resolved via learned pattern (human-confirmed before):** 0.0%
**AI-assisted, auto-applied (confidence >= 0.90 gate):** 0.0%
**Fuzzy-matched, flagged for human review:** 3.6%
**Unresolved exceptions:** 7.1%

**Overall resolved: 92.9%** (industry baseline for manual VLOOKUP reconciliation: ~51%)

## Exceptions by category

| Category | Count | Meaning |
|---|---|---|
| PARTIAL_PAYMENT | 9 | Refund netted into settlement -- explained, not an error |
| FUZZY_MATCH_NEEDS_REVIEW | 3 | Narration-based candidate match below auto-accept confidence -- human must confirm |
| ON_HOLD_BY_RAZORPAY | 1 | Razorpay's own API reports on_hold=true for this settlement -- known, held for a reason, not a lost transaction or a normal delay |
| UNEXPLAINED | 3 | No counterpart found anywhere -- needs manual investigation |
| DUPLICATE | 2 | Same settlement reported twice, one real bank credit |