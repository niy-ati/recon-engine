# Pass 4 arbiter evaluation

Run at 2026-09-03T17:13:51+00:00 against `qwen2.5:0.5b`.

**Accuracy on scored cases:** 50.0% (2 case(s) with a real correct answer).
**Mean confidence, correct vs wrong:** 1.0 vs 0.0 -- a well-calibrated model reports LOWER confidence when wrong; these numbers being close (or inverted) is itself evidence against trusting confidence as a signal, same finding as the ambiguous case below.

**Ambiguous (no ground truth) cases:** 1, of which **1** reported >=90% confidence anyway -- the exact positional-bias failure `validation_gate.AUTO_APPLY_TRUSTED_TIERS` is empty because of.

## Per-case detail

### `ocr_typo_digit_as_letter` -- CORRECT
- narration: 'pymt rcvd 3 ord#l171 thx'
- shortlist: ['order_1171', 'order_1032']
- expected: order_1171, picked: order_1171 (confidence 1.0, tier ollama:qwen2.5:0.5b)
- model's own reason: [ollama:qwen2.5:0.5b] The narration 'pymt rcvd 3 ord#l171 thx' most likely refers to order_1171.

### `positional_bias_no_signal` -- AMBIGUOUS (no ground truth)
- narration: 'payment received, thank you'
- shortlist: ['order_2001', 'order_2002']
- expected: __ambiguous__, picked: order_2001 (confidence 0.95, tier ollama:qwen2.5:0.5b)
- model's own reason: [ollama:qwen2.5:0.5b] The narration 'payment received, thank you' most likely refers to order_2001, as it indicates a payment has been received.

### `context_judgment_two_orders_one_narration` -- WRONG
- narration: 'order 8001 payment cancelled and refunded in full, order 8002 payment received and confirmed'
- shortlist: ['order_8001', 'order_8002']
- expected: order_8002, picked: order_8001 (confidence 0.0, tier ollama:qwen2.5:0.5b)
- model's own reason: [ollama:qwen2.5:0.5b] The narration refers to order 8001, which is the first order ID in the list. It does not match the order IDs provided in the candidate list.
