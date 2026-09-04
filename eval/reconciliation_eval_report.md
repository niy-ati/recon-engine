# Reconciliation accuracy evaluation

Run at 2026-09-04T15:51:23+00:00, 519 ground-truth rows.

**Overall accuracy: 96.34%** across every labeled row in the batch, not a handful of hand-picked cases.

**Single-outcome rows: 98.13% (472/481)**
**Duplicate-pair symmetry: 73.68% (14/19)** -- exactly one of each pair correctly flagged DUPLICATE, the other MATCHED.

## Per-category precision / recall

| category | tp | fp | fn | precision | recall | f1 |
|---|---|---|---|---|---|---|
| AFA_MANDATE_HOLD | 4 | 0 | 0 | 1.0 | 1.0 | 1.0 |
| DISPUTED | 5 | 0 | 0 | 1.0 | 1.0 | 1.0 |
| MATCHED | 356 | 0 | 0 | 1.0 | 1.0 | 1.0 |
| MATCHED_EXACT_REFERENCE | 14 | 0 | 0 | 1.0 | 1.0 | 1.0 |
| ON_HOLD_BY_RAZORPAY | 10 | 0 | 0 | 1.0 | 1.0 | 1.0 |
| PARTIAL_PAYMENT | 34 | 0 | 0 | 1.0 | 1.0 | 1.0 |
| ROUNDING | 14 | 0 | 0 | 1.0 | 1.0 | 1.0 |
| TAX_DEDUCTION | 10 | 0 | 0 | 1.0 | 1.0 | 1.0 |
| UNEXPLAINED | 6 | 12 | 0 | 0.3333 | 1.0 | 0.5 |
| UTR_LEVEL_MISMATCH | 6 | 0 | 9 | 1.0 | 0.4 | 0.5714 |

## Misclassified rows

- `settlement:setl_720fd6fedeb32f`: expected UTR_LEVEL_MISMATCH, got UNEXPLAINED
- `settlement:setl_c113b8b357ab2f`: expected UTR_LEVEL_MISMATCH, got UNEXPLAINED
- `settlement:setl_c3a39b4f0de393`: expected UTR_LEVEL_MISMATCH, got UNEXPLAINED
- `settlement:setl_99f72b2dab0a6b`: expected UTR_LEVEL_MISMATCH, got UNEXPLAINED
- `settlement:setl_4b32347a4c1f62`: expected UTR_LEVEL_MISMATCH, got UNEXPLAINED
- `settlement:setl_d43bb0271c6646`: expected UTR_LEVEL_MISMATCH, got UNEXPLAINED
- `settlement:setl_a8f790976f2880`: expected UTR_LEVEL_MISMATCH, got UNEXPLAINED
- `settlement:setl_72d759ecd34c71`: expected UTR_LEVEL_MISMATCH, got UNEXPLAINED
- `settlement:setl_d53c156141d332`: expected UTR_LEVEL_MISMATCH, got UNEXPLAINED

## Misclassified duplicate pairs

- `dup_10` (['settlement:setl_9f095c6f1e3068', 'settlement:setl_9f095c6f1e3068_dup']): got ['MATCHED', 'UTR_LEVEL_MISMATCH'], expected one DUPLICATE + one MATCHED
- `dup_76` (['settlement:setl_c01400c8a86b18', 'settlement:setl_c01400c8a86b18_dup']): got ['MATCHED', 'UTR_LEVEL_MISMATCH'], expected one DUPLICATE + one MATCHED
- `dup_148` (['settlement:setl_421f81884fc924', 'settlement:setl_421f81884fc924_dup']): got ['MATCHED', 'UTR_LEVEL_MISMATCH'], expected one DUPLICATE + one MATCHED
- `dup_401` (['settlement:setl_2dcba43037777e', 'settlement:setl_2dcba43037777e_dup']): got ['MATCHED', 'UTR_LEVEL_MISMATCH'], expected one DUPLICATE + one MATCHED
- `dup_487` (['settlement:setl_fa610ae411df34', 'settlement:setl_fa610ae411df34_dup']): got ['MATCHED', 'UTR_LEVEL_MISMATCH'], expected one DUPLICATE + one MATCHED
