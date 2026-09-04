# Settlement Reconciliation Report

**Settlement data source:** synthetic (generated, see generate_data.py)

**Total rows processed:** 525

**Throughput:** 525 rows in 4.69s (112.0 rows/sec) -- includes LLM arbiter call(s); Pass 1/2/2.5/2.75 alone are sub-second, the arbiter call dominates this number when present

**Clean deterministic match:** 71.8%
**Matched with explained variance:** 13.1%
**Unambiguous exact reference (deterministic, no LLM call):** 2.7%
**Resolved via learned pattern (human-confirmed before):** 0.0%
**AI-assisted, auto-applied (confidence >= 0.90 gate):** 0.0%
**Fuzzy-matched, flagged for human review:** 1.9%
**Unresolved exceptions:** 10.5%

**Overall resolved: 87.6%** (industry baseline for manual VLOOKUP reconciliation: ~51%)

## Exceptions by category

| Category | Count | Meaning |
|---|---|---|
| TAX_DEDUCTION | 10 | GST-on-MDR variance -- check against monthly tax invoice |
| FUZZY_MATCH_NEEDS_REVIEW | 10 | Narration-based candidate match below auto-accept confidence -- human must confirm |
| ON_HOLD_BY_RAZORPAY | 10 | Razorpay's own API reports on_hold=true for this settlement -- known, held for a reason, not a lost transaction or a normal delay |
| UTR_LEVEL_MISMATCH | 11 | Money arrived, but under a different UTR than the settlement report claims -- Razorpay's UTR is two-tier (batch vs. line), not a missing payout |
| UNEXPLAINED | 22 | No counterpart found anywhere -- needs manual investigation |
| ROUNDING | 14 | Sub-rupee rounding drift -- explained, not an error |
| PARTIAL_PAYMENT | 34 | Refund netted into settlement -- explained, not an error |
| DISPUTED | 5 | Settlement recon line carries an active dispute_id -- money arrived but is provisionally at risk of a chargeback clawback |
| DUPLICATE | 14 | Same settlement reported twice, one real bank credit |
| AFA_MANDATE_HOLD | 4 | Subscription charge blocked by RBI e-mandate AFA threshold (>Rs.15,000) -- needs compliant step-up re-auth, not a blind retry |

## Cash-position clarity (not a forecast -- this run's own numbers)

Rs.419,687.92 in settlement amounts touched some exception or variance path this run (duplicate settlement exports excluded -- that money already cleared under its sibling row) -- cash position a downstream tool like Cashflow Forecaster would otherwise see as ambiguous. This engine resolved Rs.198,297.58 (47.2%) of it deterministically or via a gated match, now trustworthy cash-position input. Rs.40,999.00 has a candidate match held for a human to confirm, not counted as done. Rs.180,391.34 remains genuinely open and is disclosed as such, not folded into the resolved figure.