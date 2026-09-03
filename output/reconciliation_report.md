# Settlement Reconciliation Report

**Settlement data source:** synthetic (generated, see generate_data.py)

**Total rows processed:** 514

**Throughput:** 514 rows in 7.77s (66.1 rows/sec) -- includes LLM arbiter call(s); Pass 1/2/2.5/2.75 alone are sub-second, the arbiter call dominates this number when present

**Clean deterministic match:** 78.4%
**Matched with explained variance:** 7.4%
**Unambiguous exact reference (deterministic, no LLM call):** 4.7%
**Resolved via learned pattern (human-confirmed before):** 0.0%
**AI-assisted, auto-applied (confidence >= 0.90 gate):** 0.0%
**Fuzzy-matched, flagged for human review:** 2.7%
**Unresolved exceptions:** 6.8%

**Overall resolved: 90.5%** (industry baseline for manual VLOOKUP reconciliation: ~51%)

## Exceptions by category

| Category | Count | Meaning |
|---|---|---|
| PARTIAL_PAYMENT | 31 | Refund netted into settlement -- explained, not an error |
| ON_HOLD_BY_RAZORPAY | 10 | Razorpay's own API reports on_hold=true for this settlement -- known, held for a reason, not a lost transaction or a normal delay |
| FUZZY_MATCH_NEEDS_REVIEW | 14 | Narration-based candidate match below auto-accept confidence -- human must confirm |
| UNEXPLAINED | 8 | No counterpart found anywhere -- needs manual investigation |
| UTR_LEVEL_MISMATCH | 7 | Money arrived, but under a different UTR than the settlement report claims -- Razorpay's UTR is two-tier (batch vs. line), not a missing payout |
| DUPLICATE | 11 | Same settlement reported twice, one real bank credit |
| AFA_MANDATE_HOLD | 6 | Subscription charge blocked by RBI e-mandate AFA threshold (>Rs.15,000) -- needs compliant step-up re-auth, not a blind retry |

## Cash-position clarity (not a forecast -- this run's own numbers)

Rs.291,313.90 in settlement amounts touched some exception or variance path this run (duplicate settlement exports excluded -- that money already cleared under its sibling row) -- cash position a downstream tool like Cashflow Forecaster would otherwise see as ambiguous. This engine resolved Rs.83,681.66 (28.7%) of it deterministically or via a gated match, now trustworthy cash-position input. Rs.30,254.68 has a candidate match held for a human to confirm, not counted as done. Rs.177,377.56 remains genuinely open and is disclosed as such, not folded into the resolved figure.