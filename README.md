# Settlement Reconciliation Engine

A settlement reconciliation system for Razorpay merchants. It matches settlement, bank, and ledger records; resolves what it can prove deterministically; and only reaches for a narrowly scoped, confidence-gated AI layer when deterministic logic can't resolve a row. Everything else goes to a human, with a full audit trail explaining why it didn't resolve on its own.

The governing principle: **every action here traces back to a verifiable rule, a verifiable data field, or a human decision.** Nothing is inferred, assumed, or shown as resolved unless the data actually proves it.

**Pitch deck, demo video, and screenshots:** [Google Drive folder](https://drive.google.com/drive/folders/1OBS8dvLnuLHjImn6XZF13Ev96iextn2g?usp=sharing)

## Table of Contents

- [AI Judgment and Failure Recovery](#ai-judgment-and-failure-recovery)
- [Architecture](#architecture)
- [Reconciliation Logic](#reconciliation-logic)
- [Exception Categories](#exception-categories)
- [Metrics](#metrics)
- [Performance](#performance)
- [AI Usage and Validation](#ai-usage-and-validation)
- [Live Razorpay Integration](#live-razorpay-integration)
- [Compared to Razorpay's Own Reconciliation Agent](#compared-to-razorpays-own-reconciliation-agent)
- [Settlement Q&A](#settlement-qa)
- [Scope](#scope)
- [Setup](#setup)
- [Testing](#testing)
- [License](#license)

## AI Judgment and Failure Recovery

**AI judgment.**
- A model is consulted in exactly one place in the whole pipeline: Pass 4, and only after six deterministic passes have already tried and failed. It never invents a category and never decides whether a row is resolved — it selects from a shortlist it didn't build. See [Reconciliation Logic](#reconciliation-logic) and [AI Usage and Validation](#ai-usage-and-validation).
- Adversarially tested with a narration carrying no genuine identifying signal: it picked whichever candidate was listed first and overstated its own confidence. The trust allowlist has been empty ever since, as a direct consequence of that result. See [AI Usage and Validation](#ai-usage-and-validation).
- A second, independent test was built specifically to find a case where trusting the model would have been correct. Tested three separate ways against the real model — none of the three produced a reliable correct answer. Shipped the version that fails, not the one that happened to pass. See [`src/ai_judgment_demo.py`](src/ai_judgment_demo.py).

**Failure recovery.**
- Ollama unreachable falls through to Anthropic, then to a deterministic stand-in. Verified by substituting the network call with one that actually fails, not by assuming the fallback works. See [AI Usage and Validation](#ai-usage-and-validation).
- The live Razorpay API call retries with exponential backoff on a network error. Verified by making the transport fail twice on purpose and confirming the third attempt recovers. See [Live Razorpay Integration](#live-razorpay-integration).
- An adversarial UTR-collision trap (two settlements, identical amount, different UTR) runs as part of every standard batch and resolves both correctly, so the matcher can't silently cross-wire two customers' payments. See [`src/failure_injection_demo.py`](src/failure_injection_demo.py).
- `review_server.py`'s confirm endpoint had a genuine concurrency race: it runs on a real `ThreadingHTTPServer`, and a stale double-confirm could have overwritten a human's actual decision. **Found, root-caused, and fixed** with a conditional write plus a regression test that fails against the old code and passes against the new. See [`src/db.py`](src/db.py)'s `resolve_exception`.
- A held-out validation sweep across five fresh seeds once appeared to hang past its own time budget. Root cause: `llm_matcher.py`'s own docstring documents an Ollama cold-load of up to ~80 seconds, and the affected seed happened to run sixth in a row. Bounded with a timeout set above that real ceiling. See [`extras/seed_sweep.py`](extras/seed_sweep.py).

## Architecture

![Architecture diagram showing data sources flowing through ingestion, seven matching passes, a validation gate, persistence, and the review application](assets/architecture.svg)

The validation gate lives in its own module, separate from both the matching engine and the model-calling logic. **The reconciliation engine has no import path to raw model output** — checked by an automated test, not left as a convention someone could quietly break. Every proposed match, deterministic or AI-proposed, passes through the same gate before it counts as resolved, which mirrors a principle Razorpay has [stated publicly for Agent Studio](https://razorpay.com/blog/razorpay-agent-studio-principles-guardrails-and-merchant-control): every agent action passes through a platform-level validation layer before execution.

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
| 4: Confidence Gated Arbiter | A model picks one candidate off that shortlist | No, the only pass that consults a model |

Pass 1 resolves a real, underdocumented quirk in Razorpay's own settlement data. A settlement is a batch carrying its own UTR, but the per-order detail comes from a separate [settlement recon line](https://razorpay.com/docs/api/settlements/fetch-recon/) with its own, second UTR — `settlement_utr` — confirmed against [Razorpay's Route and Linked Account documentation](https://razorpay.com/docs/payments/route/linked-account/). **Those two references can genuinely diverge for the same real transfer**, exactly the kind of thing a merchant reconciling by bank statement alone would misread as a missing payout. When a settlement's own UTR has no matching bank row, Pass 1 checks every other unclaimed bank row for an exact amount-and-date match under a different UTR, and resolves it only when exactly one such row exists. Two or more candidates is a real coincidence, and it stays unresolved rather than getting guessed at.

Pass 2.5 can't be poisoned by a careless confirmation. **A narration only gets memorized for future automatic resolution if it contains a numeric reference unique to that order among every other order the system has observed.** A generic confirmation like "payment received, thank you" still resolves the row in front of the reviewer — it just never gets written into the pattern store.

Pass 2.6 is the one place the learned-pattern store generalizes past an exact string repeat. Confirming a match also stores that same narration with its order's own digit reference swapped for a placeholder. A differently numbered narration from the same recurring template — same customer, same payment gateway generating the same surrounding text every time — resolves against that template without a fresh confirm, but **only if the captured reference uniquely identifies exactly one order still needing a match**, the same discipline as every other deterministic pass here. This closes a real, previously named gap without generalizing wording it hasn't actually seen.

**Pass 4 can't introduce a candidate of its own.** The model is restricted to the one to three orders Pass 3 already shortlisted. It selects, it doesn't originate.

## Exception Categories

Every unmatched row gets a specific, named category and a stated reason, not a generic failure flag.

| Category | Trigger | What it means |
|---|---|---|
| `ROUNDING` | Bank credit under the correct UTR, off by less than one rupee | Sub-rupee GST-on-MDR drift. **No action needed.** |
| `TAX_DEDUCTION` | Bank credit off by one to two rupees, or ledger GST differs from the settlement report by more than one rupee | **Check against Razorpay's monthly tax invoice** before filing input tax credit. |
| `PARTIAL_PAYMENT` | Ledger narration shows a refund netted into the settlement | Settled amount is gross minus refund. **Not a mismatch.** |
| `UTR_LEVEL_MISMATCH` | Bank credit matches amount and date exactly under a different, unclaimed UTR | **The money arrived** — only the reference label diverged. See Pass 1 above. |
| `DUPLICATE` | A settlement identifier appears twice for one transfer, one bank credit exists | A duplicate export row. **The real money already cleared** under the sibling entry. |
| `ON_HOLD_BY_RAZORPAY` | The recon line reports `on_hold: true` | Razorpay is **deliberately holding** this payout for a stated reason. |
| `AFA_MANDATE_HOLD` | Narration shows a subscription renewal above the RBI [e-mandate AFA threshold](https://www.business-standard.com/amp/article/finance/new-e-mandate-guidelines-rbi-enhances-limit-for-e-mandates-on-credit-debit-cards-to-rs-15-000-122060800417_1.html) of fifteen thousand rupees | Needs a **compliant step-up re-authentication**, not a blind retry. |
| `FUZZY_MATCH_NEEDS_REVIEW` | Arbiter proposed a candidate, did not clear the trust gate | A ranked candidate exists. **One click** confirms or rejects it. |
| `UNEXPLAINED` | No counterpart anywhere after every pass runs | Genuinely unexplained. **A reconciliation system reporting a perfect match rate is a signal to distrust, not a signal of quality**, so this category is expected to stay above zero. |

## Metrics

Every metric below is computed from one single source of truth and read from the same place everywhere it's displayed. That wasn't always true here — see [Scope](#scope) for a real metrics bug that got found and corrected before this figure could be presented.

Measured on the current 514-row synthetic batch, ten times the floor typically used to validate a system like this. A row the arbiter proposed but nobody has confirmed is real work still owed to a human, so it's never counted inside the resolved figure. Duplicate rows are excluded from the cash figures entirely, since that money already cleared under its sibling row and counting it again would double-count cash that was never actually at risk.

![Row resolution state and cash position clarity, both shown as stacked bars: resolved in green, pending human confirmation in orange, genuinely open in red](assets/metrics.svg)

**Throughput: 89.8 rows/sec** (514 rows in 5.72s, including the LLM arbiter call for the one row that needs it — Pass 1 through 2.75 alone process the batch in well under a second).

**90.5% isn't a cherry-picked run.** The batch generator's random seed is pinned to 42 for a reproducible default, but the pipeline has also been re-run against five other seeds it was never tuned against, generating five different 514-row batches from the same failure-mode mix: 88.0%, 88.5%, 87.1%, and 90.9% resolved, alongside the default run's 90.5%. **An 87.1%-90.9% range**, every single one clear of the ~51% manual baseline by more than 35 points. `python extras/seed_sweep.py` reproduces this directly — it re-runs the full pipeline once per seed, prints the same figures live, then restores `data/` and `output/` to their committed state when it's done.

## Performance

A 3,000-row synthetic stress test, profiled with `cProfile` instead of reasoned about, found the real bottleneck wasn't where intuition pointed. **The single largest cost — bigger than the entire matching engine combined — was a persistence-layer function reopening a fresh SQLite connection and re-running the full schema migration script once per unmatched ledger row**, instead of once per batch. Fixed with a single bulk read of every learned pattern at the start of the pass. Two more linear scans, the settlement-to-ledger match and the result lookups feeding three separate passes, each got rebuilt as a dictionary index computed once per batch, mirroring the indexing pattern the first matching pass already used.

![Before and after bar comparison for a 3,000 row batch, 6.6 seconds down to 0.9 seconds, and a 500 row batch, 0.51 seconds down to 0.06 seconds](assets/performance.svg)

**Zero behavior change**: all tests passed unchanged before and after, and the real 514-row batch produced byte-identical categorization and cash figures. At 6,000 rows, double the original stress-test ceiling, the fix held at 3.3 seconds, so the correction scales rather than just working at one measured size. A pure efficiency correction, not a rewrite of matching logic, and checked rather than assumed safe.

A second bottleneck, found the same way, this time on the real batch, not a synthetic stress test. `cProfile` on the actual `python run_all.py` command, the one in this README's own Setup section, showed **32.7 of 41.9 total seconds — 78% of the entire run — spent inside `socket.connect()`** for the Ollama arbiter calls, not in model inference. The cause: `llm_matcher.py` addressed Ollama by the hostname `localhost` rather than the literal address `127.0.0.1`. On Windows, resolving `localhost` tries IPv6 first, times out, then falls back to IPv4 — measured at ~2.05 seconds added to every single connection, against 0.0004 seconds for the literal IPv4 address, **roughly 5,000x on the connection step alone.** Fixed by changing one constant. **Measured result on the real 514-row batch: 41.88s down to 5.72s, about 7.3x**, with the identical 90.5% resolved figure before and after. The full test suite dropped from about 65 seconds to under 11 as a direct consequence, since several tests exercise this same code path.

## AI Usage and Validation

**A model is consulted in exactly one place: Pass 4**, choosing between one and three candidates a deterministic process already shortlisted. It never invents a category, never decides whether a row is resolved, and never sees a candidate list it didn't receive from the deterministic layer.

Only one model tier exists, and it's local and free — no paid API is used anywhere in this pipeline, so a run never depends on any account balance. That tier was adversarially tested with a narration carrying no genuine identifying signal, presented against two equally plausible candidates: **it picked whichever candidate was listed first and reported confidence above 0.90 regardless**, a reproducible positional-bias failure, not a hypothetical one. Its confidence value is still recorded and still routes the row to human review; it's never treated as sufficient on its own to apply a match automatically.

To answer this directly, before anyone has to ask: **no row in any real batch has ever been auto-applied.** The trust allowlist required for automatic application is empty by design, a direct consequence of the finding above. The gate mechanism itself is separately unit tested with a simulated trusted tier to confirm the logic is correct — the absence of a real auto-applied row is expected, not a coverage gap.

A second, independent test went looking for the opposite result — a case where trusting the model would have been correct — and didn't find one. [`src/ai_judgment_demo.py`](src/ai_judgment_demo.py) presents a ledger narration naming two real orders in one sentence, only one of which it actually says was paid ("order 8001 payment cancelled and refunded in full, order 8002 payment received and confirmed"). That's not mechanical digit extraction, and it isn't the same adversarial case restated. Tested three separately framed variants against the real local model during development: **two picked the wrong order outright, and the one that picked correctly still gave an incoherent reason**, conflating the two clauses it was supposed to be telling apart. `python src/ai_judgment_demo.py` reproduces the losing run every time — a second, independently arrived-at reason the trust allowlist stays empty, not a repeat of the first.

[`agent_manifest.json`](agent_manifest.json) states all of this as a structured, machine-readable contract instead of just prose: exactly what data the agent reads and writes, every action it can and can't take, the same gate rule above, and what a human retains the power to revoke. It mirrors the shape of Razorpay's own [Agent Studio guardrails](https://razorpay.com/blog/razorpay-agent-studio-principles-guardrails-and-merchant-control) — merchant control, a review-first mode, an audit trail — applied here, not just referenced.

## Live Razorpay Integration

This system's connection to Razorpay's [Settlement Recon API](https://razorpay.com/docs/api/settlements/fetch-recon/) **has actually been authenticated and fired against real test-mode credentials**, not just built and left unexercised. Razorpay's test mode generates no settlements, since no real money moves in test mode, so a correctly authenticated call against test-mode credentials returns zero items by Razorpay's own documented design — and this system reports that as the expected result, not an empty response passed off as unverified success. The call itself retries with exponential backoff on a network error, verified by substituting the transport with a fake that fails twice before succeeding, confirming the retry loop actually recovers rather than just that a retry function exists.

## Compared to Razorpay's Own Reconciliation Agent

Razorpay already ships an [Intelligent Reconciliation Agent](https://razorpay.com/blog/razorpay-agentic-platform/) as part of its Agentic Dashboard. In their own words: "upload a screenshot of your bank statement. The agent extracts UTR numbers and amounts instantly, cross-referencing them against Razorpay records to flag discrepancies." That's a real, shipped, fast tool for the common case, and it's named here directly instead of pretending it doesn't exist.

What their own description covers: two sources, a bank statement and Razorpay's settlement record, matched on UTR and amount, ending at "flag a discrepancy." What's missing from anything published: a third source, the merchant's own ledger; a named exception taxonomy; a confidence-gated AI layer; and a loop where a human correction generalizes to the next similarly worded row. This system does all four, and [`src/three_source_advantage_demo.py`](src/three_source_advantage_demo.py) proves it against the real batch instead of just asserting it: **77 of 514 rows (15.0%) are resolved or explained only because the ledger got read**, not the bank statement and settlement report alone. Two of those rows are the sharpest example — **a perfectly clean UTR, amount, and date match**, exactly what a two-source tool would call done and stop looking at — and this system still flags them, because the merchant's own ledger has no record of the order at all.

## Settlement Q&A

Track 04's own "Example Directions" name four shapes explicitly: multi-source reconciliation, a settlement Q&A agent, a forward cash forecaster, and a tax-line matcher. This build covers the first two. [`src/settlement_qa.py`](src/settlement_qa.py) — its own dedicated test file at [`tests/test_settlement_qa.py`](tests/test_settlement_qa.py) — answers plain-language questions about the last reconciliation run, wired live into the review site's chat widget through `review_server.py`'s `_handle_ask`.

**This is deliberately not an LLM feature.** Every recognized question shape here is retrieval, not judgment: "what happened to order_1032," "how many DUPLICATE exceptions," "what's my resolution rate," "how can it be resolved," follow-ups like "what can I do meanwhile" or "will it affect my cash flow" that refer back to whichever order or category the conversation was already about, and "any similar orders to order_1032." That last one reuses the exact same `difflib.get_close_matches` function Pass 3 already uses to shortlist fuzzy candidates, at a stricter cutoff appropriate to comparing one narration against every other narration in the batch rather than a short constructed candidate string. Every answer is read directly from the same persisted `exceptions` table the review queue itself shows — **no number is ever generated or estimated** — and a question outside the recognized set gets an honest "I don't have a way to answer that," not a guess dressed up as an answer. What it still can't do, on purpose: take an action. It only answers questions — confirming or rejecting a row still has to happen through the review queue's own buttons, the same deliberate gate every other resolution in this system goes through. Same zero-hallucination design as the rest of this system, applied to the one surface a merchant is most likely to actually type a question into.

## Scope

### In Scope

Reconciliation of settlement, bank, and ledger data for a single direct-to-consumer merchant on Razorpay, using either the live Settlement Recon API or an equivalent CSV export. **Nine named exception categories.** **A complete, replayable audit trail** for every automatic or human decision.

### Out of Scope

- **A dedicated TCS or TDS tax-line matcher.** Razorpay's real recon API exposes [no such field](https://razorpay.com/docs/api/settlements/fetch-recon/) among its 26 documented fields, and [Section 194-O liability is conditional on which party makes the final payment](https://razorpay.com/learn/section-194o-tds-for-e-commerce-businesses/) — a legal determination, not something this engine can read off a settlement line. Independently corroborated by [Terra Insight](https://www.terra-insight.com/insights/razorpay-settlement-reconciliation/).
- **A second cash-forecasting feature.** Razorpay already ships a [production Cashflow Forecaster](https://razorpay.com/newsroom/razorpay-launches-the-worlds-first-ai-native-agent-studio-for-payments-at-ftx26-powered-by-anthropics-claude/). This system instead quantifies, in real rupee figures, exactly how much cash-position ambiguity it removes before that data would reach a forecaster. See [Metrics](#metrics).
- **A metrics bug, found and corrected.** Three places in this codebase independently computed a resolved percentage, and all three counted an unconfirmed arbiter candidate as resolved. Found by tracing the figure against the persistence layer's own definition of which rows require human action. The correction moved the headline figure from 93.2% to the current, defensible 90.5%, pinned down by five regression tests.
- **Bank and ledger integrations are synthetic.** No live bank or accounting-software API is connected. The settlement side has a real, fired connection — see [Live Razorpay Integration](#live-razorpay-integration).
- **The learned pattern store generalizes the digit reference, not arbitrary wording.** Pass 2.6 lets a differently numbered narration from the same recurring template benefit from a prior confirmation without a fresh one. It doesn't recognize a genuinely different phrasing of the same underlying event, since the surrounding text still has to match exactly. It also needs at least one prior human confirm to exist at all — on a freshly generated batch with no confirmed history, this pass has nothing to match against yet.
- **The review application is single-user, unauthenticated, local-only.** The right call for this scope, not a claim it's production-ready.

## Setup

### Requirements

Python 3.10 or later, for PEP 604 union syntax. Zero third-party Python dependencies, verified by parsing every import in the codebase with Python's own `ast` module rather than checked by memory.

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

Skip this step to leave Pass 4 on its labeled, low-confidence stand-in response, which still routes correctly to human review.

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

1. Create a Razorpay account at [razorpay.com](https://razorpay.com). Test mode requires no KYC.
2. In the Dashboard, switch to Test Mode using the toggle in the top navigation bar.
3. Go to `Account & Settings`, then `API Keys`, or directly to [dashboard.razorpay.com/#/app/keys](https://dashboard.razorpay.com/#/app/keys).
4. Generate a test mode key pair. The secret is shown once — copy both values immediately.
5. Copy `.env.example` to `.env` in the repository root and set `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` to the values from step four. `.env` is already gitignored; real credentials go there, not in `.env.example`.

```bash
python run_all.py --live
```

Test-mode credentials correctly return zero settlements. Every bank and ledger row will report as an exception in this mode — that's expected, not a failure of the integration.

## Testing

The suite covers the matching engine, persistence layer, validation gate, model-calling logic, live API retry-and-backoff behavior, and the review application's rendering logic. Network calls are substituted at the transport layer only; the decision logic under test always runs for real, against the substituted response.

## License

Released under the MIT License. See `LICENSE`.
