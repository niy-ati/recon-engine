# Settlement Reconciliation Engine

A multi source settlement reconciliation system that matches settlement, bank, and ledger records, **resolves what it can prove deterministically**, applies a **narrowly scoped and confidence gated AI layer** only where deterministic logic cannot resolve a row, and routes everything else to a human with a **complete audit trail** explaining why automatic resolution was not possible.

The governing principle: **every action the system takes must trace back to a verifiable rule, a verifiable data field, or a human decision.** Nothing is inferred, assumed, or presented as resolved unless it can be proven from the data.

## Table of Contents

- [Architecture](#architecture)
- [Reconciliation Logic](#reconciliation-logic)
- [Exception Categories](#exception-categories)
- [Metrics](#metrics)
- [Performance](#performance)
- [AI Usage and Validation](#ai-usage-and-validation)
- [Live Razorpay Integration](#live-razorpay-integration)
- [Compared to Razorpay's Own Reconciliation Agent](#compared-to-razorpays-own-reconciliation-agent)
- [Scope](#scope)
- [Setup](#setup)
- [Testing](#testing)
- [License](#license)

## Architecture

![Architecture diagram showing data sources flowing through ingestion, seven matching passes, a validation gate, persistence, and the review application](assets/architecture.svg)

The validation gate is a module **architecturally separate** from both the matching engine and the model calling logic. **The reconciliation engine has no import path to the raw model output**, checked by an automated test, not left as a convention someone could break silently. Every proposed match, deterministic or AI proposed, passes through the same gate before it can be marked resolved. This mirrors a governance principle Razorpay has [stated publicly for Agent Studio](https://razorpay.com/blog/razorpay-agent-studio-principles-guardrails-and-merchant-control): **every agent action passes through a platform level validation layer before execution.**

## Reconciliation Logic

Seven passes run in strict order, cheapest and most certain first. A row is never sent to a more expensive pass once an earlier pass has resolved it with certainty.

| Pass | What it matches on | Resolves without a model call |
|---|---|---|
| 1: Settlement to Bank | UTR, amount, value date | Yes |
| 2: Settlement to Ledger | `order_id` | Yes |
| 2.5: Learned Pattern | A narration a human already confirmed, exact string | Yes |
| 2.6: Learned Template | A different order's narration from the same recurring template | Yes |
| 2.75: Exact Digit Reference | An unambiguous order number inside free text | Yes |
| 3: Fuzzy Candidate Narrowing | Sequence similarity, builds a shortlist only | Yes, produces no decision |
| 4: Confidence Gated Arbiter | A model picks one candidate off that shortlist | **No, the only pass that consults a model** |

**Pass 1 resolves a real, underdocumented quirk in Razorpay's own settlement data.** A `settlement` is a batch carrying its own UTR. Per order detail comes from a separate [settlement recon line](https://razorpay.com/docs/api/settlements/fetch-recon/) carrying a **second, per line UTR**, `settlement_utr`, confirmed directly against [Razorpay's Route and Linked Account documentation](https://razorpay.com/docs/payments/route/linked-account/). **These two references can genuinely diverge for the same real transfer**, exactly the kind of divergence a merchant reconciling by bank statement alone would misclassify as a missing payout. When a settlement's own UTR has no matching bank row, Pass 1 checks every other unclaimed bank row for an exact amount and date match under a different UTR, and resolves it **only when exactly one such row exists.** Two or more candidates is a genuine coincidence, **left unresolved rather than guessed.**

**Pass 2.5 cannot be poisoned by a careless confirmation.** A narration is only memorized for future automatic resolution if it **contains a numeric reference unique to that order among every other order the system has observed.** A generic confirmation such as "payment received, thank you" still resolves the row in front of the reviewer, but **is never written into the pattern store.**

**Pass 2.6 is the one place the learned pattern store generalizes beyond an exact string repeat.** Confirming a match also stores that same narration with its order's own digit reference replaced by a placeholder. A **differently numbered narration from the same recurring template**, the same customer or the same payment gateway generating the same surrounding text every time, resolves against that template without a fresh confirm, but **only if the captured reference uniquely identifies exactly one order still needing a match**, the same discipline as every other deterministic pass here. This closes a real, previously named gap without generalizing wording it hasn't actually seen.

**Pass 4 cannot introduce a candidate.** The model is restricted to the one to three orders Pass 3 already shortlisted. **It selects, it does not originate.**

## Exception Categories

Every unmatched row gets a specific, named category and a stated reason, not a generic failure flag.

| Category | Trigger | What it means |
|---|---|---|
| `ROUNDING` | Bank credit under the correct UTR, off by less than one rupee | Sub rupee GST on MDR drift. **No action needed.** |
| `TAX_DEDUCTION` | Bank credit off by one to two rupees, or ledger GST differs from the settlement report by more than one rupee | **Check against Razorpay's monthly tax invoice** before filing input tax credit. |
| `PARTIAL_PAYMENT` | Ledger narration shows a refund netted into the settlement | Settled amount is gross minus refund. **Not a mismatch.** |
| `UTR_LEVEL_MISMATCH` | Bank credit matches amount and date exactly under a different, unclaimed UTR | **The money arrived.** Only the reference label diverged. See Pass 1 above. |
| `DUPLICATE` | A settlement identifier appears twice for one transfer, one bank credit exists | A duplicate export row. **The real money already cleared under the sibling entry.** |
| `ON_HOLD_BY_RAZORPAY` | The recon line reports `on_hold: true` | Razorpay is **deliberately holding** this payout for a stated reason. |
| `AFA_MANDATE_HOLD` | Narration shows a subscription renewal above the RBI [e-mandate AFA threshold](https://www.business-standard.com/amp/article/finance/new-e-mandate-guidelines-rbi-enhances-limit-for-e-mandates-on-credit-debit-cards-to-rs-15-000-122060800417_1.html) of fifteen thousand rupees | Needs a compliant step up re-authentication, **not a blind retry.** |
| `FUZZY_MATCH_NEEDS_REVIEW` | Arbiter proposed a candidate, did not clear the trust gate | **A ranked candidate exists.** One click confirms or rejects it. |
| `UNEXPLAINED` | No counterpart anywhere after every pass runs | Genuinely unexplained. **A reconciliation system reporting a perfect match rate is a signal to distrust, not a signal of quality**, so this category is expected to stay above zero. |

## Metrics

Every metric below is computed from **one single source of truth** and read from the same place everywhere it is displayed. That was not always true here; see [Scope](#scope) for a real metrics bug that was found and corrected before this figure could be presented.

Measured on the current **514 row synthetic batch, ten times the floor** typically used to validate a system of this kind. **A row an arbiter proposed but nobody has confirmed is real work still owed to a human, and is never counted inside the resolved figure.** Duplicate rows are **excluded from the cash figures entirely**, since that money already cleared under its sibling row and counting it again would double count cash that was never actually at risk.

![Row resolution state and cash position clarity, both shown as stacked bars: resolved in green, pending human confirmation in orange, genuinely open in red](assets/metrics.svg)

## Performance

A 3,000 row synthetic stress test, profiled with `cProfile` rather than reasoned about, found the real bottleneck was not where intuition pointed. **The single largest cost, larger than the entire matching engine combined, was a persistence layer function reopening a fresh SQLite connection and re-running the full schema migration script once per unmatched ledger row**, instead of once per batch. Fixed with a single bulk read of every learned pattern at the start of the pass. Two additional linear scans, the settlement to ledger match and the result lookups feeding three separate passes, were each rebuilt as a dictionary index computed once per batch, mirroring an indexing pattern the first matching pass already used.

![Before and after bar comparison for a 3,000 row batch, 6.6 seconds down to 0.9 seconds, and a 500 row batch, 0.51 seconds down to 0.06 seconds](assets/performance.svg)

**Zero behavior change.** **All 88 tests passed unchanged before and after**, and the real 514 row batch produced **byte identical categorization and cash figures** before and after the fix. At 6,000 rows, double the original stress test ceiling, the fix held at 3.3 seconds, **confirming the correction scales** rather than just working at one measured size. This was a pure efficiency correction, not a rewrite of matching logic, **verified rather than assumed safe.**

## AI Usage and Validation

**A model is consulted in exactly one place: Pass 4**, choosing between one and three candidates a deterministic process already shortlisted. It **never invents a category**, never decides whether a row is resolved, and never sees a candidate list it did not receive from the deterministic layer.

**Only one model tier exists, and it is local and free.** **No paid API is used anywhere in this pipeline**, so a run never depends on any account balance. This tier was **adversarially tested** with a narration carrying no genuine identifying signal, presented against two equally plausible candidates. **It selected whichever candidate was listed first and reported confidence above 0.90 regardless**, a reproducible positional bias failure, not a hypothetical one. Its confidence value is still recorded and still routes the row to human review; **it is never treated as sufficient on its own to apply a match automatically.**

**A direct answer stated here rather than waiting to be asked: no row in any real batch has ever been auto applied.** **The trust allowlist required for automatic application is empty by design**, a direct consequence of the finding above. The gate mechanism itself is separately unit tested with a simulated trusted tier to confirm the logic is correct; **the absence of a real auto applied row is expected, not a coverage gap.**

## Live Razorpay Integration

This system's connection to Razorpay's [Settlement Recon API](https://razorpay.com/docs/api/settlements/fetch-recon/) was **authenticated and fired against real test mode credentials, not merely built and left unexercised.** Razorpay's test mode generates no settlements, since no real money moves in test mode; **a correctly authenticated call against test mode credentials returns zero items by Razorpay's own documented design**, and this system reports that outcome as the expected result **rather than presenting an empty response as unverified success.** The call itself **retries with exponential backoff on a network error**, verified by substituting the transport with a fake that fails twice before succeeding and confirming the real retry loop recovers, not just that a retry function exists.

## Compared to Razorpay's Own Reconciliation Agent

Razorpay already ships an [Intelligent Reconciliation Agent](https://razorpay.com/blog/razorpay-agentic-platform/) as part of its Agentic Dashboard. In their own words: **"upload a screenshot of your bank statement. The agent extracts UTR numbers and amounts instantly, cross-referencing them against Razorpay records to flag discrepancies."** That is a real, shipped, fast tool for the common case, and this README names it directly rather than pretending it doesn't exist.

What their own description covers: **two sources**, a bank statement and Razorpay's settlement record, matched on **UTR and amount**, ending in **"flag a discrepancy."** What is not published anywhere found in researching this: a third source (the merchant's own ledger), a **named exception taxonomy**, a **confidence gated AI layer**, or a **loop where a human correction generalizes** to the next similarly worded row. This system does all four, and [`src/three_source_advantage_demo.py`](src/three_source_advantage_demo.py) proves it against the real batch rather than asserting it: **77 of 514 rows (15.0%)** are resolved or explained only because the ledger was read, not the bank statement and settlement report alone. Two of those rows are the sharpest example — **a perfectly clean UTR, amount, and date match**, exactly what a two source tool would call done and stop looking at, that this system still flags because the merchant's own ledger has no record of the order at all.

## Scope

### In Scope

Reconciliation of settlement, bank, and ledger data for a single direct to consumer merchant on Razorpay, using either the live Settlement Recon API or an equivalent CSV export. **Nine named exception categories.** **A complete, replayable audit trail for every automatic or human decision.**

### Out of Scope

- **A dedicated TCS or TDS tax line matcher.** Razorpay's real recon API exposes [no such field](https://razorpay.com/docs/api/settlements/fetch-recon/) among its **26 documented fields**, and [Section 194O liability is conditional on which party makes the final payment](https://razorpay.com/learn/section-194o-tds-for-e-commerce-businesses/), **a legal determination, not a threshold this engine can read off a settlement line.** Independently corroborated by [Terra Insight](https://www.terra-insight.com/insights/razorpay-settlement-reconciliation/).
- **A second cash forecasting feature.** Razorpay already ships a [production Cashflow Forecaster](https://razorpay.com/newsroom/razorpay-launches-the-worlds-first-ai-native-agent-studio-for-payments-at-ftx26-powered-by-anthropics-claude/). This system instead **quantifies, in real rupee figures**, exactly how much cash position ambiguity it removes before that data would reach a forecaster. See [Metrics](#metrics).
- **A metrics bug, found and corrected, not hidden.** Three places in this codebase independently computed a resolved percentage, and **all three counted an unconfirmed arbiter candidate as resolved.** Found by tracing the figure against the persistence layer's own definition of which rows require human action. **The correction moved the headline figure from 93.2% to the current, defensible 90.5%**, and is pinned down by five regression tests.
- **Bank and ledger integrations are synthetic.** No live bank or accounting software API is connected. The settlement side has a **real, fired connection**, described under [Live Razorpay Integration](#live-razorpay-integration).
- **The learned pattern store generalizes the digit reference, not arbitrary wording.** Pass 2.6 lets a differently numbered narration from the same recurring template benefit from a prior confirmation without a fresh one. What it does **not** do: recognize a genuinely different phrasing of the same underlying event, since the surrounding text must still match exactly. **Requires at least one prior human confirm to exist at all** — on a freshly generated batch with no confirmed history, this pass has nothing to match against yet.
- **The review application is single user, unauthenticated, local only.** A correct choice for this scope, **not a production deployment claim.**

## Setup

### Requirements

**Python 3.10 or later**, for PEP 604 union syntax. **Zero third party Python dependencies**, verified by parsing every import in the codebase with Python's own `ast` module rather than checked by memory.

### 1. Clone and verify

```bash
git clone https://github.com/niy-ati/recon-engine.git
cd recon-engine
python --version
```

### 2. Run the synthetic pipeline

No credentials required.

```bash
python run_all.py
```

Generates a synthetic settlement, bank, and ledger dataset, runs every pass, persists results to SQLite, and prints the full report.

### 3. Start the review application

```bash
python src/review_server.py
```

Opens `http://localhost:8000`, backed directly by the database the previous step wrote.

### 4. Run the tests

```bash
python -m unittest discover -s tests -p "test_*.py"
```

CI runs this same command against Python 3.10 and 3.12 on every push, in `.github/workflows/test.yml`.

### 5. Install Ollama for the arbiter pass

Skip this step to leave Pass 4 on its labeled, low confidence stand in response, which still routes correctly to human review.

```bash
# macOS
brew install ollama

# Linux
curl -fsSL https://ollama.com/install.sh | sh

# Windows: download the installer from https://ollama.com/download
```

```bash
ollama pull qwen2.5:0.5b
```

Ollama runs as a background service after installation on macOS and Windows. On Linux, start it manually if needed:

```bash
ollama serve
```

### 6. Connect the live Razorpay API

Skip this step to stay on the synthetic dataset.

1. Create a Razorpay account at [razorpay.com](https://razorpay.com). **Test mode requires no KYC.**
2. In the Dashboard, switch to Test Mode using the toggle in the top navigation bar.
3. Go to `Account & Settings`, then `API Keys`, or directly to [dashboard.razorpay.com/#/app/keys](https://dashboard.razorpay.com/#/app/keys).
4. Generate a test mode key pair. **The secret is shown once; copy both values immediately.**
5. Copy `.env.example` to `.env` in the repository root and set `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` to the values from step four. **`.env` is already excluded in `.gitignore`.**

```bash
python run_all.py --live
```

**Test mode credentials correctly return zero settlements.** Every bank and ledger row will report as an exception in this mode, **which is expected, not a failure of the integration.**

## Testing

The suite covers the matching engine, persistence layer, validation gate, model calling logic, live API retry and backoff behavior, and the review application's rendering logic. **Network calls are substituted at the transport layer only; the decision logic under test always runs for real against the substituted response.**

## License

Released under the MIT License. See `LICENSE`.
