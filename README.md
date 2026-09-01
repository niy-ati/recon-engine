# Settlement Reconciliation Engine

A reconciliation system for Razorpay merchants that matches settlement, bank, and ledger records; resolves what it can prove deterministically; and only reaches for a narrowly scoped, confidence-gated AI layer when deterministic logic can't resolve a row. Everything else goes to a human, with a full audit trail explaining why it didn't resolve on its own.

**Governing principle:** every action traces back to a verifiable rule, a verifiable data field, or a human decision. Nothing is inferred or shown as resolved unless the data proves it.

**Live demo:** [reconcile-engine-demo.vercel.app](https://reconcile-engine-demo.vercel.app) — the real review dashboard and Settlement Q&A, running against a persisted batch.
**Video, screenshots:** [Google Drive folder](https://drive.google.com/drive/folders/1OBS8dvLnuLHjImn6XZF13Ev96iextn2g?usp=sharing)

## At a glance

- **90.5% resolved with zero human input**, vs. ~51% for manual spreadsheet reconciliation — measured on a real 514-row batch, holding 87–91% across five other untuned batches.
- **7-pass deterministic matcher** — UTR, order ID, learned patterns, exact digit references, fuzzy narrowing — before a model is ever consulted.
- **9 named exception categories**, each with a stated reason and what a merchant should actually do about it — never a generic failure flag.
- **One AI step, tightly gated**: a model picks between candidates a deterministic pass already shortlisted, at 90%+ confidence, and only auto-applies from a trust list that's empty until a tier proves itself. Nothing has ever auto-applied.
- **Full audit trail** — every automatic and human decision is a real, replayable SQLite record.
- **Live dashboard** — Overview, Queue, Records, Sources, with real charts computed from the batch, not screenshots.
- **Settlement Q&A** — ask plain-language questions about a batch by chat, voice, or an uploaded statement/photo. Retrieval only, never generated.
- **Hands-free Voice Agent** — listens, answers out loud, and can be interrupted mid-answer, entirely in-browser.
- **Real Razorpay connection** — authenticated and fired against the live Settlement Recon API, not just built and left unexercised.
- **Zero paid API keys anywhere.** The only model used (Ollama) runs locally.
- **Tax line matcher** — checks every settlement's GST against the real statutory rate, independent of matching. Found live: 2 rows the reconciliation itself calls clean are still charging the wrong GST.

## Table of Contents

- [Architecture](#architecture)
- [Reconciliation Passes](#reconciliation-passes)
- [Exception Categories](#exception-categories)
- [Metrics](#metrics)
- [Performance](#performance)
- [AI Usage and Validation](#ai-usage-and-validation)
- [Failure Recovery](#failure-recovery)
- [Live Razorpay Integration](#live-razorpay-integration)
- [Compared to Razorpay's Own Reconciliation Agent](#compared-to-razorpays-own-reconciliation-agent)
- [Review Application](#review-application)
- [Settlement Q&A](#settlement-qa)
- [Tax Line Matcher](#tax-line-matcher)
- [Scope](#scope)
- [Setup](#setup)
- [Testing](#testing)
- [License](#license)

## Architecture

![Architecture diagram showing data sources flowing through ingestion, seven matching passes, a validation gate, persistence, and the review application](assets/architecture.svg)

The validation gate lives in its own module, separate from both the matching engine and the model-calling logic — **the reconciliation engine has no import path to raw model output**, enforced by an automated test. Every proposed match, deterministic or AI-proposed, passes through the same gate before it counts as resolved.

## Reconciliation Passes

Cheapest and most certain first. A row never reaches a more expensive pass once an earlier one has resolved it.

| Pass | Matches on | Needs a model |
|---|---|---|
| 1: Settlement → Bank | UTR, amount, value date | No |
| 2: Settlement → Ledger | `order_id` | No |
| 2.5: Learned Pattern | A narration a human already confirmed, exact string | No |
| 2.6: Learned Template | A different order's narration from the same recurring template | No |
| 2.75: Exact Digit Reference | An unambiguous order number inside free text | No |
| 3: Fuzzy Candidate Narrowing | Sequence similarity — builds a shortlist only | No |
| 4: Confidence-Gated Arbiter | A model picks one candidate off that shortlist | **Yes — the only pass that does** |

**Three things worth knowing about how these behave:**

- **Pass 1 catches a real, underdocumented Razorpay quirk**: a settlement's batch-level UTR and its per-order [recon-line UTR](https://razorpay.com/docs/api/settlements/fetch-recon/) can genuinely diverge for the same transfer. When that happens, Pass 1 checks every other unclaimed bank row for an exact amount-and-date match under a different UTR — and only resolves it when exactly one such row exists. Two or more candidates stays unresolved rather than getting guessed at.
- **Pass 2.5 can't be poisoned by a lazy confirmation.** A narration only gets memorized for future auto-resolution if it contains a numeric reference unique to that order. A generic "payment received, thank you" still resolves the row in front of the reviewer — it just never enters the pattern store.
- **Pass 4 can't introduce a candidate of its own.** It's restricted to the 1–3 orders Pass 3 already shortlisted. It selects; it doesn't originate.

## Exception Categories

Every unmatched row gets a specific, named category and a stated reason — not a generic failure flag.

| Category | Trigger | What it means |
|---|---|---|
| `ROUNDING` | Bank credit off by less than one rupee | Sub-rupee GST-on-MDR drift. **No action needed.** |
| `TAX_DEDUCTION` | Ledger GST differs from the settlement report | **Check against Razorpay's monthly tax invoice.** |
| `PARTIAL_PAYMENT` | Ledger shows a refund netted into the settlement | Settled amount is gross minus refund. **Not a mismatch.** |
| `UTR_LEVEL_MISMATCH` | Bank credit matches amount/date under a different, unclaimed UTR | **The money arrived** — only the reference diverged. |
| `DUPLICATE` | A settlement ID appears twice, one bank credit exists | **The real money already cleared** under the sibling entry. |
| `ON_HOLD_BY_RAZORPAY` | Recon line reports `on_hold: true` | Razorpay is **deliberately holding** the payout. |
| `AFA_MANDATE_HOLD` | Subscription renewal above the RBI [₹15,000 e-mandate threshold](https://www.business-standard.com/amp/article/finance/new-e-mandate-guidelines-rbi-enhances-limit-for-e-mandates-on-credit-debit-cards-to-rs-15-000-122060800417_1.html) | Needs a **compliant step-up re-auth**, not a blind retry. |
| `FUZZY_MATCH_NEEDS_REVIEW` | Arbiter proposed a candidate, didn't clear the trust gate | A ranked candidate exists — **one click** confirms or rejects. |
| `UNEXPLAINED` | No counterpart anywhere after every pass runs | Genuinely unexplained. **Expected to stay above zero** — a perfect match rate is a red flag, not a win. |

## Metrics

- **90.5% resolved, zero human input** — 514-row batch. Re-run against five other untuned seeds: **87.1%–90.9%**, every one clear of the ~51% manual baseline by 35+ points. `python extras/seed_sweep.py` reproduces this live.
- **89.8 rows/sec throughput** — 514 rows in 5.72s, including the one LLM call the batch actually needs.
- A row the arbiter *proposed* but nobody confirmed is never counted as resolved. Duplicate rows are excluded from cash figures entirely, since that money already cleared under its sibling row.

![Row resolution state and cash position clarity, both shown as stacked bars: resolved in green, pending human confirmation in orange, genuinely open in red](assets/metrics.svg)

## Performance

- **5.72s end to end for the full 514-row batch** (89.8 rows/sec), including the one LLM call the batch actually needs — Pass 1 through 2.75 alone clear in well under a second.
- **Persistence reads every learned pattern and match index once per batch**, not once per row, via a bulk read plus two dictionary indexes. A 3,000-row stress batch runs in 0.9s and holds at 3.3s even at 6,000 rows — 12x the demo batch size — with byte-identical output at every scale.
- **The Ollama client connects over the literal loopback address, not the hostname** — on Windows, resolving `localhost` tries IPv6 first and only falls back to IPv4 after a real timeout, adding measurable latency to every single call.

![Before and after bar comparison for a 3,000 row batch, 6.6 seconds down to 0.9 seconds, and a 500 row batch, 0.51 seconds down to 0.06 seconds](assets/performance.svg)

## AI Usage and Validation

- **Used in exactly one place**: Pass 4, choosing between candidates a deterministic pass already shortlisted. It never invents a category, never decides whether a row is resolved, and never sees a candidate it didn't receive from the deterministic layer.
- **One model tier, local and free** — no paid API, no dependency on any account balance.
- **Adversarially tested, and it failed the test**: given a narration with no genuine identifying signal and two equally plausible candidates, it picked whichever came first and reported >90% confidence anyway — a reproducible positional-bias failure.
- **Result: the trust list is empty by design.** No row in any real batch has ever been auto-applied. Confidence is still recorded and still routes to human review — it's just never treated as sufficient on its own.
- **A second, independent test looked for a case where trusting it would've been right, and didn't find one.** [`src/ai_judgment_demo.py`](src/ai_judgment_demo.py): a narration naming two orders in one sentence, only one actually paid. Three framings, real model: two picked the wrong order outright, the third gave the right answer for an incoherent reason.
- **The governance is a real file, not a policy doc**: [`agent_manifest.json`](agent_manifest.json) states exactly what the agent reads/writes, every action it can and can't take, and what a human can revoke — machine-readable, not prose.

## Failure Recovery

- Ollama unreachable → falls through to a labeled deterministic stand-in, verified by substituting the network call with one that actually fails.
- Live Razorpay API call retries with exponential backoff on a network error, verified against a transport that fails twice before succeeding.
- An adversarial UTR-collision trap (two settlements, identical amount, different UTR) runs on every standard batch and resolves both correctly — the matcher can't cross-wire two customers' payments. See [`src/failure_injection_demo.py`](src/failure_injection_demo.py).
- Confirm and reject writes on the review server are guarded against a race on its `ThreadingHTTPServer`: a conditional update means only the first decision on a row is ever applied, regression-tested against the unguarded behavior.
- The validation sweep's timeout sits above Ollama's own documented cold-load ceiling (~80s), so a slow first call across five seeds doesn't read as a hang. See [`extras/seed_sweep.py`](extras/seed_sweep.py).

## Live Razorpay Integration

The connection to Razorpay's [Settlement Recon API](https://razorpay.com/docs/api/settlements/fetch-recon/) **is authenticated and has actually been fired against real test-mode credentials** — not just built and left unexercised. Test mode correctly returns zero settlements (no real money moves in test mode), and the system reports that as the expected result, not an error. The call retries with exponential backoff on a network failure, verified against a transport substituted to fail twice before succeeding.

## Compared to Razorpay's Own Reconciliation Agent

Razorpay's own [Intelligent Reconciliation Agent](https://razorpay.com/blog/razorpay-agentic-platform/) is real and shipped, in their own words: "upload a screenshot of your bank statement. The agent extracts UTR numbers and amounts instantly, cross-referencing them against Razorpay records to flag discrepancies." Two sources, ending at "flag a discrepancy."

**What this adds:** a third source (the merchant's own ledger), a named exception taxonomy, a confidence-gated AI layer, and a loop where a human correction generalizes to the next similarly worded row. [`src/three_source_advantage_demo.py`](src/three_source_advantage_demo.py) proves the third source matters against the real batch: **77 of 514 rows (15.0%) are resolved or explained only because the ledger got read** — including two rows with a perfectly clean UTR/amount/date match that a two-source tool would call done, which this system still flags because the merchant's own ledger has no record of the order at all.

The same gap holds even for Razorpay's own multi-gateway product. [Optimizer's Single View Recon](https://razorpay.com/blog/single-view-recon/) consolidates settlements across payment gateways into one dashboard — but it's a consolidated view, not a matching engine: no AI matching, no exception taxonomy, no merchant ledger as a source, and it only works on payments already routed through Optimizer.

## Review Application

Four pages, one stdlib `http.server`, no framework — **Overview**, **Queue**, **Records**, **Sources** — sharing one shell and one live SQLite database with the pipeline.

- **Overview** — a donut chart of the real status breakdown; a three-bucket bar showing how rarely the AI pass is actually needed; a category grid linking straight to filtered rows; a cash-value-by-category chart (a category with few rows can still hold the most money); and the cash-position clarity panel from [Metrics](#metrics).
- **Queue** — every row still needing a decision, full `replay_log` per row, Confirm / Reject / Needs-clarification actions writing straight to SQLite.
- **Records** — every persisted row, filterable and sortable entirely client-side.
- **Sources** — the three raw input files with row counts, to trace a result back to its export; plus the **tax line audit** below them (see [Tax Line Matcher](#tax-line-matcher)).

## Settlement Q&A

Plain-language questions about the batch, answered by [`src/settlement_qa.py`](src/settlement_qa.py) — **retrieval from the real persisted data, not a model's guess.** A question outside what it recognizes gets an honest "I don't have a way to answer that," never a guess. It can't take an action either — confirming or rejecting still goes through the review queue's own buttons. One exception, deliberately gated: see "A narrated version" below.

**What you can ask:**

- **A specific order or settlement** — "what happened to order_1032," "what happened to setl_a1b2c3."
- **A category** — count or list: "how many DUPLICATE exceptions," "list DUPLICATE orders."
- **Status and resolution** — "what's my resolution rate," "how many have been confirmed/rejected," "how many rows need clarification."
- **The whole batch, not just one order** — "give me an overview of this batch," "what's the status breakdown," "how many settlements are in this batch," "what's the total settlement value," "what's the biggest exception."
- **Cash position** — "how much money is in UNEXPLAINED," "what's my cash position" (reuses the exact function the Overview page's own cash panel uses — never a second, independent calculation).
- **Follow-ups and related cases** — "how can it be resolved," "what can I do meanwhile," "will it affect my cash flow," "any similar orders to order_1032" — all resolve against whichever order or category the conversation was already about.
- **The system itself** — "why isn't this just an LLM," "what model do you use," "what's the architecture," "what's your accuracy." Fixed, human-written answers, not generated ones.
- **Plain fintech vocabulary** — "what is a UTR," "what does chargeback mean," "what is AFA," "what is an audit trail." Fixed definitions, not this batch's own numbers, so there's nothing here to hallucinate.
- **Ordinary conversation** — a greeting, thanks, goodbye, "how are you," "what can you do," or a bare "ok"/"sure" all get a real, warm, human-written reply instead of the honest-but-cold fallback below — checked before anything else, so the first thing said to the voice agent is never a refusal.
- **A narrated version, in plain English** — "narrate this batch," "give me a written summary," "tell me a story about this batch." The one place a model *writes* an answer instead of retrieving one: Ollama receives only the same already-computed, verified numbers the deterministic overview uses, never raw rows, and every number in its response is extracted and checked against those exact facts before ever being shown. One invented number and the whole response is discarded for the plain deterministic version instead — tested live, not just claimed: the real model has genuinely invented a number in testing, and it was caught and silently rejected. This is the same validation-gate discipline Pass 4 uses, applied to language generation instead of candidate-selection.
- **Whether an open row is a one-off or systemic** — "is there a recurring pattern in the exceptions," "any systemic issues here." Groups every currently-open row by a generalized narration shape (every digit-bearing token collapsed to a placeholder) — a category count says *how many* rows are `FUZZY_MATCH_NEEDS_REVIEW`; this answers a different question a count can't: whether those rows are independent problems or one upstream cause. Found live on the real batch: all 14 open `FUZZY_MATCH_NEEDS_REVIEW` rows share the exact same shape — one narration template consistently misreading a digit as a letter, not 14 unrelated typos.
- **Whether tax was charged correctly** — "check tax rates," "any tax errors," "is the GST correct." See [Tax Line Matcher](#tax-line-matcher) below.

**How you can ask it:**

- **Chat** — type it, or upload a PDF or photo of a statement; it finds the order/settlement ID and answers about it. Every chat answer can be read back aloud.
- **Voice Agent** — a separate, persistent, hands-free widget on every page. It listens, answers out loud, and starts listening again automatically — no mic button to click each turn. **It can be interrupted mid-answer**: while it's speaking, it listens in the background and stops the instant it hears words that aren't just its own voice echoing back through the speakers, rather than relying on raw volume, which can't tell an echo from a real interruption.
- Every question above works identically by voice, however a person actually phrases it — apostrophe or not, "whats" or "what's."

**Everything here runs local — no cloud API, no key, no cost**, for the chat, the voice, or the document upload. A gated model fallback exists underneath for phrasing the keyword matching doesn't recognize — same confidence-gate discipline as the reconciliation engine's own Pass 4, held untrusted for the same reason: tested against the real local model, its confidence score came back 1.0 regardless of whether it was right, so it carries no usable signal yet.

## Tax Line Matcher

A named use case in its own right, separate from reconciliation — matching settlement, bank, and ledger amounts against each other says nothing about whether the tax on those amounts is *correct*. [`src/tax_audit.py`](src/tax_audit.py) checks every settlement's GST-on-MDR against the real, current statutory rate: **18%** of the MDR fee, not of the transaction's gross value ([source](https://razorpay.com/blog/enterprise-payment-gateway-pricing-india/)).

Found live on this project's own real 503-row batch: **2 settlements (`order_1210`, `order_1151`) are currently plain `MATCHED` rows with no exception at all** — clean by every check reconciliation runs, and clean by a dashboard that only compares settlement to ledger — yet both were charged Rs.1 more GST than the law requires. Their settlement and ledger figures agree with each other; they just both agree on the wrong number, which is exactly the case a pure amount-matching check can never catch.

Shown on the **Sources** page, and answerable directly — "check tax rates," "any tax errors," "is the GST correct" — through the same Settlement Q&A path as everything else.

## Scope

### In Scope

Reconciliation of settlement, bank, and ledger data for a single direct-to-consumer merchant on Razorpay, using either the live Settlement Recon API or an equivalent CSV export. Nine named exception categories. A complete, replayable audit trail for every automatic or human decision.

### Out of Scope

- **A dedicated TCS or TDS tax-line matcher.** Razorpay's real recon API exposes [no such field](https://razorpay.com/docs/api/settlements/fetch-recon/), and [Section 194-O liability is conditional on which party makes the final payment](https://razorpay.com/learn/section-194o-tds-for-e-commerce-businesses/) — a legal determination, not something readable off a settlement line. Independently corroborated by [Terra Insight](https://www.terra-insight.com/insights/razorpay-settlement-reconciliation/).
- **A second cash-forecasting feature.** Razorpay already ships a production Cashflow Forecaster. This system instead quantifies, in real rupee figures, how much cash-position ambiguity it removes before that data reaches one.
- **Bank and ledger integrations are synthetic.** No live bank or accounting-software API is connected. The settlement side has a real, fired connection — see [Live Razorpay Integration](#live-razorpay-integration).
- **The learned pattern store generalizes the digit reference, not arbitrary wording.** A genuinely different phrasing of the same event still needs a fresh confirm.
- **The review application is single-user, unauthenticated, local-only** — the right call for this scope, not a claim it's production-ready.

## Setup

### Requirements

Python 3.10+. The core — `reconcile.py`, `db.py`, `review_server.py`, `settlement_qa.py` — is zero-third-party-dependency, verified by parsing every import with Python's own `ast` module. Two optional layers need real packages (`requirements.txt`): the chat's document upload needs `pypdf` (required) and `pytesseract`/`Pillow` (optional OCR — degrades to an honest "not available" without the separate Tesseract engine); the separate Postgres-backed Vercel demo needs `psycopg`. Running `run_all.py` locally never touches either.

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

CI runs this same command against Python 3.10 and 3.12 on every push.

### 5. Install Ollama for the arbiter pass

Skip this to leave Pass 4 on its labeled, low-confidence stand-in response, which still routes correctly to human review.

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

Runs as a background service after install on macOS/Windows. On Linux, start it manually: `ollama serve`.

### 6. Connect the live Razorpay API

Skip this to stay on the synthetic dataset.

1. Create a Razorpay account at [razorpay.com](https://razorpay.com) — test mode needs no KYC.
2. Switch to Test Mode in the Dashboard's top navigation.
3. Go to `Account & Settings` → `API Keys`, or [dashboard.razorpay.com/#/app/keys](https://dashboard.razorpay.com/#/app/keys).
4. Generate a test-mode key pair — the secret is shown once.
5. Copy `.env.example` to `.env` and set `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET`. `.env` is gitignored.

```bash
python run_all.py --live
```

Test-mode credentials correctly return zero settlements — every row reports as an exception in this mode, which is expected, not a failure.

### 7. Install Tesseract for image uploads in chat

Skip this to leave document upload able to read PDFs, just not photos or screenshots — it says so honestly rather than failing silently.

```bash
# macOS
brew install tesseract

# Linux
sudo apt-get install tesseract-ocr

# Windows: download the installer from https://github.com/UB-Mannheim/tesseract/wiki
```

Everything stays local — no image, extracted text, or file ever leaves the machine.

## Testing

Covers the matching engine, persistence layer, validation gate, model-calling logic, live API retry-and-backoff behavior, and the review application's rendering logic. Network calls are substituted at the transport layer only — the decision logic under test always runs for real, against the substituted response.

## License

Released under the MIT License. See `LICENSE`.
