"""
Settlement Q&A -- answers plain-language questions about the last
reconciliation run without opening a dashboard or the review queue table.

Razorpay's own Agent Studio ships a "Settlement Insights" agent that sends
a daily settlement summary over WhatsApp, precisely so a merchant doesn't
have to check a dashboard. This is not that agent -- no WhatsApp, no
messaging infra, no daily schedule -- but it answers the same shape of
question, grounded the same way this whole codebase insists on: every
answer is read directly from the persisted `exceptions` table (db.py), the
same data the review queue shows. Nothing here invents a number. A
question this module doesn't recognize gets an honest "don't know," not a
guess.

Recognized question shapes (deterministic keyword/pattern matching, no
LLM -- there is no ambiguity here that needs judgment, only retrieval):
  - "what happened to order_1032" / "why is order_1032 unresolved"
  - "how many exceptions" / "how many are open"
  - "how many DUPLICATE exceptions" / "how many are on hold"
  - "what's my resolution rate" / "how much is resolved"
  - "how can it be resolved" / "how do I fix order_1032" / "what can I do
    by that time" / "will it affect my cash flow" -- canned, per-category
    guidance text (what to do, and whether it affects your books/cash
    while it's open), not a generated answer. Resolves the order/category
    from the question itself, or falls back to whatever order/category
    the last turn was about (see `context` below).
  - "any similar orders to order_1032" / "has this happened before" --
    other rows sharing the same category, plus other rows whose narration
    is a close textual match via the same difflib.get_close_matches
    Pass 3 (reconcile.py) already uses to shortlist fuzzy candidates. Not
    a new matching algorithm invented for this module -- the same
    function, at a stricter cutoff, since this compares one real
    narration directly against other real narrations across the whole
    batch, not against a short constructed candidate string inside an
    already-narrowed shortlist the way Pass 3 does. The looser Pass 3
    cutoff would return noisy false positives here.
  - "what happened to setl_a1b2c3d4e5f6a7" -- the same lookup as an
    order_id question, keyed on settlement_id instead, since a settlement
    can carry rows across multiple orders (see DUPLICATE detection).
  - "list DUPLICATE orders" / "which orders are UNEXPLAINED" / "show me
    the ON_HOLD_BY_RAZORPAY ones" -- the same category match "how many
    DUPLICATE exceptions" already does, but returning the actual
    order_ids instead of just a count. Capped at 15 shown plus a "N more"
    tail so one huge category can't flood the chat panel.
  - "how many have been confirmed" / "how many rejected" / "how many
    need clarification" -- counts by db.py's own resolution_status field
    and, for "needs clarification", by whether a note was attached via
    add_note() without resolving the row (see db.py's add_note
    docstring) -- not a new resolution state invented here.
  - "how much money is in DUPLICATE" / "cash value of UNEXPLAINED" -- a
    category-scoped sum of net_amount, computed fresh here since nothing
    else in the codebase slices cash by category alone.
  - "how much cash is at risk" / "what's my cash position" -- delegates
    to db.compute_cash_clarity(), the same function the Overview page's
    cash-position panel already uses, not reimplemented here. This
    project's own metrics bug (see PITCH_NOTES, "Metrics") was caused by
    three independent
    reimplementations of "what counts as resolved" silently disagreeing;
    a fourth copy here would risk the exact same drift.
  - "how does this batch look" / "give me an overview of this batch" --
    one combined summary (resolved %, open count, cash clarity, top
    categories), composed from the same real numbers the more specific
    handlers below already compute -- not a new calculation.
  - "what's the status breakdown" / "how many are matched" -- counts by
    the raw pipeline status field across every row, distinct from the
    category breakdown below.
  - "how many settlements are in this batch" / "total settlement value"
    / "how big is this batch" -- whole-batch counts and the full
    settlement value across every persisted row, not scoped to a
    category or to open exceptions the way every count/value handler
    above is.
  - "what's the biggest exception" / "smallest amount" -- the single
    highest- or lowest-value row, optionally scoped to a named category.
  - "why isn't this just an LLM" / "what model do you use" / "what's the
    architecture" / "what's your accuracy" / "tell me about Slash" --
    fixed, human-written answers about THIS SYSTEM itself (not the
    reconciliation data), sourced from PITCH_NOTES.md. Same canned-text
    principle as CATEGORY_GUIDANCE -- never model-generated.

Fallback for everything else (qa_intent_gate.py / qa_intent_router.py): if
none of the above keyword shapes match, a gated local model (the same
Ollama pattern Pass 4's llm_matcher.py/validation_gate.py use) gets one
attempt to classify the question into one of the shapes above and
reformulate it into the exact phrasing that shape's handler recognizes --
then that canonical phrasing is answered by the same deterministic
handlers above, unchanged. The model never generates an answer or a fact
itself, only picks which door to knock on. Live-tested against the actual
local model this project runs (qwen2.5:0.5b): its confidence score never
varied across dozens of real questions (always 1.0, right or wrong) and it
misclassified an unambiguous case, so qa_intent_gate.py currently holds
every result -- the routing logic is built, tested, and wired end to end,
but not blindly trusted, the same discipline validation_gate.py already
applies to Pass 4's arbiter (see qa_intent_gate.py's docstring for the
evidence). It activates automatically once a specific tier is shown,
empirically, not to share that failure mode.
"""
import difflib
import re
import sys
from collections import Counter

import db
import qa_intent_gate

ORDER_ID_PATTERN = re.compile(
    r"\border[\s_,:#-]*(?:number|no\.?)?[\s_,:#-]*(\d+)\b", re.IGNORECASE
)  # not just "order_1032"/"order 1032" -- a real bug, found live: voice
   # input transcribes a spoken order number with punctuation or filler
   # words a typed question never would ("order #1032", "order number
   # 1032", "order, 1032"), and the original pattern required the digits
   # immediately after "order" with only a single optional space or
   # underscore -- anything else silently failed to extract at all.
SETTLEMENT_ID_PATTERN = re.compile(r"\b(setl_[a-z0-9]+)\b", re.IGNORECASE)

_APOSTROPHES = re.compile(r"['’‘]")


def _normalize(question: str) -> str:
    """Lowercased with every apostrophe (straight or curly) stripped, not
    just lowercased -- a real gap, found live: every trigger phrase below
    is written as "what's"/"isn't"/"doesn't", so voice transcription and
    plain casual typing that drops the apostrophe ("whats the
    architecture", "why isnt this just an llm") silently missed every one
    of them. All keyword matching below runs against this normalized
    form, and every trigger phrase is written apostrophe-free to match --
    matching against the raw apostrophe'd text elsewhere would silently
    stop matching the now-apostrophe-free question."""
    return _APOSTROPHES.sub("", question.lower())

KNOWN_CATEGORIES = [
    "UNEXPLAINED", "DUPLICATE", "PARTIAL_PAYMENT", "TAX_DEDUCTION", "ROUNDING",
    "FUZZY_MATCH_NEEDS_REVIEW", "AFA_MANDATE_HOLD", "ON_HOLD_BY_RAZORPAY",
]

# MATCHED_LOW_CONFIDENCE deliberately excluded: it's an arbiter's proposed
# candidate sitting in the human review queue, not a resolved row --
# db.py's own needs_action rule already treats it exactly like EXCEPTION.
# This mirrors the same fix already made in reconcile.py's summarize() and
# review_server.py's compute_cash_clarity() / render_donut() -- catching
# the same bug here too, since this module computes "resolved" a fourth
# time, independently, and would otherwise disagree with what the review
# site itself shows for the exact same batch.
RESOLVED_STATUSES = {
    "MATCHED", "MATCHED_WITH_VARIANCE", "MATCHED_EXACT_REFERENCE",
    "MATCHED_LEARNED_PATTERN", "MATCHED_AI_ASSISTED",
}

# Fixed, human-written guidance per category -- not generated, not looked
# up from an LLM. Each line is an honest statement of what the category
# means and what a merchant can actually do about it, so "how can it be
# resolved" gets a real answer instead of the flat fallback.
CATEGORY_GUIDANCE = {
    "DUPLICATE": (
        "This is a duplicate settlement entry for an order already matched "
        "elsewhere. No fund action needed -- it's excluded from cash totals "
        "already; the row exists so the duplicate export line isn't silently "
        "dropped from the audit trail. No waiting involved and no effect on "
        "your books either way."
    ),
    "UNEXPLAINED": (
        "No UTR, order ID, or narration reference tied this row to anything "
        "in the ledger or bank statement. Check the raw bank narration by "
        "hand, or wait for the next settlement cycle in case the missing "
        "reference shows up in a later transfer. While it's open: don't "
        "count this amount as reconciled in your books or cash forecast -- "
        "treat it as unexplained, not as received, until it's traced."
    ),
    "PARTIAL_PAYMENT": (
        "The settled amount is less than the order amount. Confirm with the "
        "payment gateway whether the balance was split across settlements, "
        "then reconcile the remainder against the next cycle. While it's "
        "open: only book the partial amount actually settled -- the "
        "remainder isn't in your account yet, so don't forecast it as "
        "received cash until the balance clears."
    ),
    "TAX_DEDUCTION": (
        "The variance matches a TDS/GST-style deduction. Verify the "
        "deducted amount against the applicable tax rate, then confirm it "
        "as an explained variance rather than a genuine shortfall. No "
        "waiting needed -- it's a real, permanent deduction, not money "
        "still in transit."
    ),
    "ROUNDING": (
        "The variance is a sub-rupee rounding difference. Safe to confirm "
        "as-is -- no money is actually missing, and it has no effect on "
        "your cash position."
    ),
    "FUZZY_MATCH_NEEDS_REVIEW": (
        "The AI arbiter proposed a candidate match from narration "
        "similarity, but it was deliberately not auto-applied. Open the "
        "review queue, check the highlighted narration evidence in the "
        "audit trail, and click Confirm if it looks right -- or Reject if "
        "it doesn't. The settlement amount is already in your account "
        "either way; this only affects whether it shows as matched or as "
        "an open exception in your books."
    ),
    "AFA_MANDATE_HOLD": (
        "This settlement is held by Razorpay's own AFA/e-mandate step-up "
        "flow, triggered by the RBI's Rs 15,000 threshold on recurring "
        "subscription charges. There's no action on your end -- it "
        "releases once the mandate step-up completes or the transaction is "
        "declined. While it's held: the money is not yet in your account, "
        "so exclude it from your cash flow forecast until it actually "
        "settles or is confirmed declined."
    ),
    "ON_HOLD_BY_RAZORPAY": (
        "Razorpay itself is holding this settlement (compliance or risk "
        "review, typically). Check the hold reason on the Razorpay "
        "dashboard directly -- this system only sees what's in the "
        "settlement export, not Razorpay's internal hold reasoning. While "
        "it's held: the funds are not yet in your bank account, so exclude "
        "this amount from your available cash and cash flow forecast until "
        "the hold clears -- it's a status your books should reflect "
        "honestly, not lost money."
    ),
}

# Fixed, human-written answers about THIS SYSTEM's own architecture, model
# choice, metrics, and research -- not the reconciliation data itself. Same
# pattern as CATEGORY_GUIDANCE: canned text a person wrote and can verify,
# never model-generated, so a question like "why isn't this just an LLM"
# gets a real, accurate answer instead of the honest fallback -- without
# opening the door to free-form generation, which would risk exactly the
# kind of confidently-wrong answer this project's own research (see the
# Slash-thread finding below) argues against. Every number and claim here
# matches what's documented in PITCH_NOTES.md, not invented for voice
# output.
PROJECT_KNOWLEDGE = [
    (
        ("why not llm", "why not use an llm", "why not just use ai", "why not ai",
         "why isnt this an llm", "why isnt this just an llm", "why not gpt",
         "why deterministic", "why not generative", "why not use a model for everything"),
        "Matching is deterministic, not model-generated -- bank UTR, then "
        "order ID, then a learned pattern. The one place a model touches "
        "anything is a narrow tie-break, gated at 90 percent confidence and "
        "never auto-applied, because testing found it can be confidently "
        "wrong."
    ),
    (
        ("what model", "which model", "whats ollama", "what is ollama",
         "tell me about ollama", "do you use gpt", "what ai do you use"),
        "A local Ollama model, qwen2.5:0.5b, handles one narrow job: "
        "picking which order a fuzzy match most likely belongs to, after "
        "every deterministic pass fails. Its result is never auto-applied "
        "-- no paid API key is used anywhere."
    ),
    (
        ("whats the architecture", "what is the architecture", "how does this work",
         "how does this system work", "explain the architecture", "walk me through the architecture",
         "tech stack", "whats the tech stack"),
        "Settlements are matched in passes, cheapest and most certain "
        "first: bank UTR, then order ID, then learned patterns, then "
        "fuzzy narrowing, and only as a last resort a gated model "
        "tie-break. Every outcome is written to a persistent audit trail, "
        "built on pure Python standard library."
    ),
    (
        ("whats your accuracy", "what is your accuracy", "resolution rate baseline",
         "what metrics", "what are your metrics", "how accurate is this",
         "whats the baseline", "compared to manual", "how much better than manual"),
        "Manual reconciliation typically clears about 51 percent of rows. "
        "This engine resolves 90.5 percent with zero human input on a "
        "real 514-row batch, holding between 87 and 91 percent across "
        "five different re-tested batches."
    ),
    (
        ("tell me about slash", "whats slash", "what is slash", "the slash agent",
         "your research", "what did your research find", "differentiator",
         "what makes this different", "whats the differentiator"),
        "Research traced a real internal Razorpay thread about an AI "
        "agent called Slash, where engineers raised the need for a "
        "pre-execution enforcement layer deciding what's allowed to run "
        "before it runs. This system's validation gate is exactly that -- "
        "nothing a model proposes auto-applies unless a proven "
        "trustworthy tier is explicitly allow-listed, and today that list "
        "is empty by design."
    ),
    (
        ("why not improve an existing agent", "why not a bigger ai system",
         "why build this instead", "why this and not"),
        "Razorpay's own Bookkeeping Agent posts entries based on "
        "predefined rules, which by definition can't resolve an exception "
        "no rule matches. This system is built as the residual layer "
        "underneath that gap, not a replacement for it."
    ),
]


def _project_knowledge(question: str) -> str | None:
    """Fixed, human-written answers about the SYSTEM itself -- its own
    architecture, model choice, metrics, and research -- not the
    reconciliation data. Checked in _answer() before _resolution_guidance:
    a bare "why" in a question like "why isn't this just an LLM" would
    otherwise match _is_resolution_question's generic why_signal and get
    misrouted to "tell me which order or category you mean" instead of a
    real answer."""
    ql = _normalize(question)
    for phrases, answer in PROJECT_KNOWLEDGE:
        if any(p in ql for p in phrases):
            return answer
    return None


def _is_resolution_question(question: str) -> bool:
    """Matches four related shapes, all answered from the same canned
    CATEGORY_GUIDANCE text: "how can it/order_2/a DUPLICATE be resolved",
    "what can I do [about it / by that time / while I wait]", "will this
    affect my cash flow / system / books", and a category-level "why is/are
    it/they X" with no specific order named -- CATEGORY_GUIDANCE already
    opens with what the category means, which is the honest answer to a
    "why" question too, not a new fact invented for it. "resolv" also
    matches "unresolved" on its own, so it's paired with a question-word
    check rather than used alone.

    "why" is deliberately scoped to questions with no order_id: when an
    order IS named ("why is order_2 unresolved"), _find_order's own
    per-row `reason` field already answers that specific "why" more
    precisely than the generic per-category text here would -- the
    top-level dispatch in _answer() must keep routing those to
    _find_order, not here. Regression-tested directly: a "why" question
    naming a real order used to (correctly) hit _find_order before this
    signal existed; it must still, not get diverted to generic guidance."""
    ql = _normalize(question)
    resolve_signal = ("resolv" in ql or "fix" in ql) and any(
        w in ql for w in ("how", "what should", "whats the", "what is the")
    )
    action_signal = any(p in ql for p in (
        "what can i do", "what should i do", "what do i do",
        "in the meantime", "while i wait", "while waiting", "by that time", "meanwhile",
    ))
    impact_signal = any(p in ql for p in (
        "affect my", "impact my", "affect the", "impact the", "hit my books", "hit my cash",
    ))
    why_signal = "why" in ql and not ORDER_ID_PATTERN.search(question)
    return resolve_signal or action_signal or impact_signal or why_signal


def _extract_order_id(question: str) -> str | None:
    match = ORDER_ID_PATTERN.search(question)
    return f"order_{match.group(1)}" if match else None


def _extract_settlement_id(question: str) -> str | None:
    match = SETTLEMENT_ID_PATTERN.search(question)
    return match.group(1) if match else None


def _extract_category(question: str) -> str | None:
    """Word-boundary matched, not a bare substring test -- a real bug,
    found live: "ROUNDING" is a substring of the ordinary English word
    "surrounding", so "are there other exceptions surrounding this one"
    used to get miscategorized as a ROUNDING question and hijack whatever
    handler ran next (a general "how many exceptions" count silently
    became a ROUNDING-only count, for example).

    Matched against the question with its own spacing intact (not a
    version with every space turned to underscore -- that would turn the
    whole question into one run of word characters with no boundaries
    left at all, which would silently stop matching a category typed with
    its real underscores, like "which orders are ON_HOLD_BY_RAZORPAY").
    Each category's own internal "_" is matched against a literal "_" OR
    whitespace, so both "ON_HOLD_BY_RAZORPAY" and "ON HOLD BY RAZORPAY"
    are recognized by the same pattern. An optional trailing "S"/"ES" is
    allowed too -- a real gap, found live: a merchant naturally says "what
    about duplicates" or "how many partial payments", not the bare
    singular enum form, and the word-boundary fix above (correctly) no
    longer lets a plural slide through as a loose substring match."""
    qu = question.upper()
    for category in KNOWN_CATEGORIES:
        pattern = re.escape(category).replace("_", r"[_\s]")
        if re.search(rf"\b{pattern}(?:ES|S)?\b", qu):
            return category
    if re.search(r"\bon hold\b", _normalize(question)):
        return "ON_HOLD_BY_RAZORPAY"
    return None


def _plural(count: int, word: str) -> str:
    """"1 row" / "5 rows" -- not the "row(s)" placeholder every count used
    to read out as literally, which is fine written down but awkward and
    unnatural spoken aloud over voice."""
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _find_order(order_id: str) -> str:
    """Includes each row's net_amount -- a real bug, found live: a money
    question naming a specific order ("how much money is stuck in
    order_4") is intercepted here by _answer()'s top-level order_id
    branch before _cash_value (which only handles category-scoped or
    overall totals, not a single order) ever runs, so the actual number
    the merchant asked about used to never appear anywhere in the
    answer."""
    rows = [r for r in db.get_all_exceptions() if r["order_id"] == order_id]
    if not rows:
        return f"No record of {order_id} in the last reconciliation run."

    lines = [f"{order_id}: {_plural(len(rows), 'row')} found."]
    for r in rows:
        amount = f" net=Rs.{r['net_amount']:,.2f}" if r["net_amount"] is not None else ""
        lines.append(
            f"  status={r['status']}{amount}"
            + (f" category={r['category']}" if r['category'] else "")
            + (f" -- {r['reason']}" if r['reason'] else "")
        )
        if r["resolution_status"] != "OPEN":
            lines.append(f"  human decision: {r['resolution_status']}"
                          + (f" ({r['resolution_note']})" if r["resolution_note"] else ""))
    return "\n".join(lines)


def _find_settlement(settlement_id: str) -> str:
    """Same shape as _find_order, keyed on settlement_id instead. A
    settlement_id can carry more than one row -- a DUPLICATE settlement
    and its clean-matched sibling share one order_id but are still two
    distinct settlement rows (see reconcile.py's DUPLICATE detection) --
    so this lists every row under that settlement, not just the first."""
    rows = [r for r in db.get_all_exceptions() if r["settlement_id"] == settlement_id]
    if not rows:
        return f"No record of {settlement_id} in the last reconciliation run."

    lines = [f"{settlement_id}: {_plural(len(rows), 'row')} found."]
    for r in rows:
        amount = f" net=Rs.{r['net_amount']:,.2f}" if r["net_amount"] is not None else ""
        lines.append(
            f"  order_id={r['order_id']} status={r['status']}{amount}"
            + (f" category={r['category']}" if r['category'] else "")
            + (f" -- {r['reason']}" if r['reason'] else "")
        )
        if r["resolution_status"] != "OPEN":
            lines.append(f"  human decision: {r['resolution_status']}"
                          + (f" ({r['resolution_note']})" if r["resolution_note"] else ""))
    return "\n".join(lines)


def _category_count(question: str) -> str | None:
    """Answers both "how many DUPLICATE exceptions" (a count) and "list
    DUPLICATE orders" / "which orders are UNEXPLAINED" (the actual
    order_ids) -- same category match, the question's own phrasing picks
    which shape comes back. Capped at 15 shown so one large category
    can't flood the chat panel; the count in the sentence is still the
    real total, not the shown-count.

    Requires an actual count/list trigger word, not just a category name
    appearing anywhere in the question -- a real production bug, caught
    live: "why u think tehy are duplicate" mentions "duplicate" but isn't
    asking for a count, and used to get answered with a bare
    "11 row(s) categorized as DUPLICATE" instead of either a real answer
    or an honest "don't know". A category name alone is not a count
    request."""
    category = _extract_category(question)
    if category is None:
        return None
    ql = _normalize(question)
    rows = db.get_all_exceptions()
    matching = [r for r in rows if r["category"] == category]

    wants_list = any(kw in ql for kw in ("list", "which order", "which one", "show me", "what are the", "what about", "tell me about"))
    if wants_list:
        if not matching:
            return f"No rows are categorized as {category}."
        order_ids = [r["order_id"] for r in matching if r["order_id"]]
        shown = order_ids[:15]
        more = f", and {len(order_ids) - 15} more" if len(order_ids) > 15 else ""
        return f"{_plural(len(matching), 'row')} categorized as {category}: {', '.join(shown)}{more}."

    # wants_count gates BOTH branches below -- the ON_HOLD_BY_RAZORPAY one
    # used to fire on "on hold" alone with no count check at all, a real
    # bug found live: since _extract_category's own fallback sets this
    # category from the literal phrase "on hold", any sentence containing
    # it -- "my settlement is on hold, is that bad", even "I've been on
    # hold with support for an hour" -- got a bare, often nonsensical
    # count instead of being treated like every other category here. The
    # extra "on hold" query phrasings below keep the legitimate status
    # check ("what's on hold right now") working -- unlike a real category
    # name, "on hold" is itself a phrase that only means something as a
    # query when paired with a question word, not just present anywhere.
    wants_count = any(kw in ql for kw in (
        "how many", "count of", "number of", "total number",
        "whats on hold", "what is on hold", "which are on hold",
        "any on hold", "anything on hold",
    ))
    if not wants_count:
        return None

    if category == "ON_HOLD_BY_RAZORPAY":
        return f"{_plural(len(matching), 'settlement')} on hold (ON_HOLD_BY_RAZORPAY)."
    return f"{_plural(len(matching), 'row')} categorized as {category}."


def _resolution_status_count(question: str) -> str | None:
    """Counts by db.py's own resolution_status field -- CONFIRMED and
    REJECTED are the only two terminal values resolve_exception() ever
    writes (see its status_map). "Needs clarification" isn't a third
    resolution_status value -- add_note() deliberately leaves a row OPEN
    and just attaches resolution_note (see its docstring: "Row stays
    OPEN, stays in the queue"), so that's counted as OPEN-with-a-note
    here, not invented as a status that doesn't exist in the schema."""
    ql = _normalize(question)
    rows = db.get_all_exceptions()

    # "how many" plus a bare "confirm"/"reject" substring, not an exact
    # phrase match -- a real gap, found live: natural voice phrasing like
    # "how many have I confirmed so far" doesn't contain any of the fixed
    # multi-word phrases this used to require verbatim.
    wants_count = "how many" in ql

    if wants_count and "confirm" in ql:
        count = sum(1 for r in rows if r["resolution_status"] == "CONFIRMED")
        verb = "has" if count == 1 else "have"
        return f"{_plural(count, 'row')} {verb} been confirmed by a human reviewer."

    if wants_count and "reject" in ql:
        count = sum(1 for r in rows if r["resolution_status"] == "REJECTED")
        verb = "has" if count == 1 else "have"
        return f"{_plural(count, 'row')} {verb} been rejected by a human reviewer."

    if any(kw in ql for kw in ("need clarification", "needs clarification", "have a note", "with a note", "clarification note")):
        count = sum(1 for r in rows if r["resolution_status"] == "OPEN" and r.get("resolution_note"))
        verb = "is" if count == 1 else "are"
        return f"{_plural(count, 'row')} {verb} still open with a clarification note attached."

    return None


def _cash_value(question: str) -> str | None:
    """Rupee-value questions, not row counts. A category-scoped sum is
    computed fresh here since nothing else in the codebase slices cash by
    category alone. The overall at-risk/resolved/pending/still-open split
    is NOT reimplemented here -- db.compute_cash_clarity() is the exact
    function the Overview page's cash-position panel already uses. See
    the module docstring for why a fourth independent copy of this logic
    is worth avoiding."""
    ql = _normalize(question)
    money_signal = any(kw in ql for kw in (
        "how much money", "how much cash", "total value", "cash value",
        "rupee value", "at risk", "cash position",
    ))
    if not money_signal:
        return None

    rows = db.get_all_exceptions()
    category = _extract_category(question)
    if category:
        total = sum(r["net_amount"] for r in rows if r["category"] == category and r["net_amount"] is not None)
        return f"Rs.{total:,.2f} across rows categorized as {category}."

    c = db.compute_cash_clarity(rows)
    return (
        f"Rs.{c['at_risk']:,.2f} total touched some exception or variance path this run. "
        f"Rs.{c['resolved']:,.2f} ({c['resolved_pct']}%) is resolved and trustworthy, "
        f"Rs.{c['pending_review']:,.2f} ({c['pending_review_pct']}%) is pending human review, "
        f"and Rs.{c['still_open']:,.2f} ({c['still_open_pct']}%) is still open."
    )


def _open_count(question: str) -> str | None:
    """"how many" plus any open-shaped topic word, not a fixed set of
    exact multi-word phrases -- a real gap, found live: "how many orders
    need my attention" has "how many" and "need" both present, but not as
    the single substring "how many need" this used to require verbatim.
    Safe to broaden: a category-specific "how many X exceptions" is
    always intercepted by _category_count earlier in the dispatch order,
    so this never has to tell the two apart itself."""
    ql = _normalize(question)
    open_signal = any(kw in ql for kw in (
        "open", "pending", "need", "exceptions", "attention", "unresolved", "outstanding",
    ))
    if "how many" in ql and open_signal:
        open_rows = db.get_open_exceptions()
        return f"{_plural(len(open_rows), 'row')} currently open, needing a decision."
    return None


def _resolution_rate(question: str) -> str | None:
    ql = _normalize(question)
    if any(kw in ql for kw in ("resolution rate", "how much resolved", "how much is resolved", "overall resolved", "percent resolved")):
        rows = db.get_all_exceptions()
        if not rows:
            return "No batch persisted yet -- run the pipeline first."
        resolved = sum(1 for r in rows if r["status"] in RESOLVED_STATUSES)
        pct = round(100 * resolved / len(rows), 1)
        return f"{pct}% resolved ({resolved} of {len(rows)} rows)."
    return None


def _category_breakdown(question: str) -> str | None:
    """Counts by `category` -- deliberately scoped to "...by category"
    phrasing rather than a bare "breakdown", which used to also swallow
    "status breakdown" questions that _status_breakdown below is meant to
    answer instead (a real dispatch collision, caught before shipping)."""
    ql = _normalize(question)
    if any(kw in ql for kw in ("category breakdown", "breakdown by category", "by category", "exceptions by category")):
        rows = db.get_all_exceptions()
        counts = Counter(r["category"] for r in rows if r["category"])
        if not counts:
            return "No categorized exceptions in the last run."
        return "\n".join(f"{cat}: {n}" for cat, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    return None


def _batch_summary(question: str) -> str | None:
    """One combined answer for a broad "how does this batch look"
    request -- composed entirely from the same real, already-tested
    numbers the more specific handlers elsewhere in this module compute
    (RESOLVED_STATUSES, db.compute_cash_clarity, a category Counter), not
    a new calculation invented for this. A genuine gap: a merchant's
    first question about a batch is often this broad, before they know
    which specific count/value/category to ask about by name."""
    # Bare keywords ("overview", "summary", "rundown", ...), not a fixed
    # phrase requiring an exact wrapper like "of THIS batch" -- a real gap,
    # found live: "give an overview of the WHOLE batch" and "overview of
    # the batch" (no "this") both missed the original phrase list
    # entirely. Safe to match these words alone since nothing else in this
    # module's vocabulary uses them for anything else.
    ql = _normalize(question)
    if not any(kw in ql for kw in (
        "overview", "summary", "summarize", "rundown", "recap",
        "how does this batch look", "how does the batch look",
        "how is this batch doing", "how is the batch doing",
        "hows this batch", "hows the batch",
        "tell me about this batch", "tell me about the batch",
    )):
        return None

    rows = db.get_all_exceptions()
    if not rows:
        return "No batch persisted yet -- run the pipeline first."

    resolved = sum(1 for r in rows if r["status"] in RESOLVED_STATUSES)
    pct = round(100 * resolved / len(rows), 1)
    open_rows = db.get_open_exceptions()
    clarity = db.compute_cash_clarity(rows)
    cats = Counter(r["category"] for r in rows if r["category"])
    top_cats = ", ".join(f"{cat} ({n})" for cat, n in sorted(cats.items(), key=lambda kv: -kv[1])[:3])

    lines = [
        f"{_plural(len(rows), 'row')} in this batch, {pct}% resolved ({resolved} of {len(rows)}).",
        f"{_plural(len(open_rows), 'row')} still need a decision.",
        f"Rs.{clarity['at_risk']:,.2f} touched some exception or variance path -- "
        f"Rs.{clarity['resolved']:,.2f} resolved, Rs.{clarity['pending_review']:,.2f} pending review, "
        f"Rs.{clarity['still_open']:,.2f} still open.",
    ]
    if top_cats:
        lines.append(f"Top categories: {top_cats}.")
    return "\n".join(lines)


def _status_breakdown(question: str) -> str | None:
    """Counts by the raw pipeline `status` field (MATCHED, EXCEPTION,
    MATCHED_LOW_CONFIDENCE, MATCHED_WITH_VARIANCE, ...) across every
    persisted row -- distinct from _category_breakdown, which counts by
    `category` and only covers the subset of rows that have one at all.
    A real gap: there was no way to ask "what's the status breakdown" or
    "how many are matched vs how many are exceptions" -- every existing
    handler either scoped to a named category or to needs_action rows."""
    ql = _normalize(question)
    if not any(kw in ql for kw in (
        "status breakdown", "breakdown by status", "breakdown of status",
        "by status", "matched vs", "how many matched", "how many are matched",
        "how many rows matched", "how many settled cleanly",
    )):
        return None
    rows = db.get_all_exceptions()
    if not rows:
        return "No batch persisted yet -- run the pipeline first."
    counts = Counter(r["status"] for r in rows)
    return "\n".join(f"{status}: {n}" for status, n in sorted(counts.items(), key=lambda kv: -kv[1]))


def _batch_totals(question: str) -> str | None:
    """Whole-batch counts and the full settlement value -- every
    persisted row, not just the exception/variance-scoped subset
    _cash_value and _category_count answer. A real gap, found live: "how
    many settlements are in this batch" and "what's the total settlement
    value" had no handler at all, since every existing count/value
    handler is scoped to open exceptions or a specific category."""
    ql = _normalize(question)
    total_value_signal = any(kw in ql for kw in (
        "total value", "total amount", "total settlement value", "total worth",
        "grand total", "batch worth", "how much did this batch settle",
        "total settled", "total net amount",
    ))
    count_signal = any(kw in ql for kw in (
        "how many settlements", "how many orders", "how many rows",
        "how many total", "total orders", "total settlements", "total rows",
        "size of this batch", "how big is this batch", "how many records",
    ))
    if not (total_value_signal or count_signal):
        return None

    rows = db.get_all_exceptions()
    if not rows:
        return "No batch persisted yet -- run the pipeline first."

    total_value = sum(r["net_amount"] for r in rows if r["net_amount"] is not None)
    distinct_orders = len({r["order_id"] for r in rows if r["order_id"]})
    distinct_settlements = len({r["settlement_id"] for r in rows if r["settlement_id"]})

    if total_value_signal and not count_signal:
        return f"Rs.{total_value:,.2f} total across {_plural(len(rows), 'row')} in this batch."
    if count_signal and not total_value_signal:
        return (f"{_plural(len(rows), 'row')} in this batch, covering "
                f"{_plural(distinct_orders, 'order')} and {_plural(distinct_settlements, 'settlement')}.")
    return (f"{_plural(len(rows), 'row')} in this batch ({_plural(distinct_orders, 'order')}, "
            f"{_plural(distinct_settlements, 'settlement')}) totaling Rs.{total_value:,.2f}.")


def _extreme_amount(question: str) -> str | None:
    """The single highest- or lowest-value row in the batch, optionally
    scoped to a named category -- a real gap: net_amount was already
    persisted per row, but nothing answered "what's the biggest
    exception" or "which settlement has the highest amount"."""
    ql = _normalize(question)
    wants_max = any(kw in ql for kw in (
        "biggest", "largest", "highest value", "highest amount", "top amount", "most money",
    ))
    wants_min = any(kw in ql for kw in (
        "smallest", "lowest value", "lowest amount", "least money",
    ))
    if not (wants_max or wants_min):
        return None

    category = _extract_category(question)
    rows = db.get_all_exceptions()
    if category:
        rows = [r for r in rows if r["category"] == category]
    rows = [r for r in rows if r["net_amount"] is not None]
    if not rows:
        scope = f" {category}" if category else ""
        return f"No{scope} rows with a net amount to compare."

    target = max(rows, key=lambda r: r["net_amount"]) if wants_max else min(rows, key=lambda r: r["net_amount"])
    label = "largest" if wants_max else "smallest"
    ident = target["order_id"] or target["settlement_id"] or f"row {target['id']}"
    cat_part = f", category={target['category']}" if target["category"] else ""
    return (f"The {label} amount in this batch is Rs.{target['net_amount']:,.2f} -- {ident} "
            f"(status={target['status']}{cat_part}).")


def _resolution_guidance(question: str, context: dict | None) -> str | None:
    if not _is_resolution_question(question):
        return None

    order_id = _extract_order_id(question)
    category = _extract_category(question)

    if order_id:
        rows = [r for r in db.get_all_exceptions() if r["order_id"] == order_id]
        if not rows:
            return f"No record of {order_id} in the last reconciliation run."
        # The categorized row is the interesting one, not whichever row
        # happened to be inserted first -- a DUPLICATE settlement and its
        # clean-matched sibling share one order_id (see reconcile.py's
        # DUPLICATE detection), and db.get_all_exceptions() has no
        # guaranteed ordering between them. Same fix _similar_orders
        # already has (see its own comment); a real bug found live:
        # "how can order_20 be resolved" for a known DUPLICATE order was
        # answered "tell me which order or category you mean" whenever
        # the plain MATCHED sibling (category=None) happened to sort first.
        category = next((r["category"] for r in rows if r["category"]), rows[0]["category"])
    elif category is None and context:
        order_id = context.get("last_order_id")
        category = context.get("last_category")
        if order_id and not category:
            rows = [r for r in db.get_all_exceptions() if r["order_id"] == order_id]
            if rows:
                category = next((r["category"] for r in rows if r["category"]), rows[0]["category"])

    if not category:
        return (
            "Tell me which order or category you mean -- e.g. "
            "\"how can order_1032 be resolved\" or \"how can a DUPLICATE "
            "be resolved\"."
        )

    guidance = CATEGORY_GUIDANCE.get(category)
    if not guidance:
        return f"{category} rows don't need manual resolution -- they're already matched cleanly."

    prefix = f"For {order_id} ({category}): " if order_id else f"For {category}: "
    return prefix + guidance


def _is_similarity_question(question: str) -> bool:
    ql = _normalize(question)
    return any(p in ql for p in (
        "similar", "like this order", "like order", "same pattern",
        "happened before", "seen this before", "other order", "any other order",
        "has this happened",
    ))


def _similar_orders(question: str, context: dict | None) -> str | None:
    """Read-only, no LLM: same category as the target order, plus other
    orders whose narration is a close textual match via difflib. See the
    module docstring for why the cutoff differs from Pass 3's."""
    if not _is_similarity_question(question):
        return None

    order_id = _extract_order_id(question)
    if not order_id and context:
        order_id = context.get("last_order_id")
    if not order_id:
        return ("Tell me which order you mean -- e.g. \"any similar orders "
                "to order_1032\".")

    rows = db.get_all_exceptions()
    same_order = [r for r in rows if r["order_id"] == order_id]
    if not same_order:
        return f"No record of {order_id} in the last reconciliation run."
    # A DUPLICATE settlement and its clean-matched sibling share one
    # order_id (see reconcile.py's DUPLICATE detection) -- the row with
    # an actual category is the interesting one to compare, not whichever
    # row happened to be inserted first.
    target = next((r for r in same_order if r["category"]), same_order[0])

    others = [r for r in rows if r["order_id"] != order_id]

    same_category = []
    if target["category"]:
        same_category = [r["order_id"] for r in others if r["category"] == target["category"]]

    narration_matches = []
    if target.get("narration"):
        candidates = {r["order_id"]: r["narration"] for r in others if r.get("narration")}
        close_text = difflib.get_close_matches(target["narration"], list(candidates.values()), n=5, cutoff=0.6)
        narration_matches = [oid for oid, narr in candidates.items() if narr in close_text]

    if not same_category and not narration_matches:
        return (f"{order_id} ({target['category'] or target['status']}) doesn't share a "
                f"category or a closely worded narration with any other order in this run.")

    lines = [f"{order_id} is categorized {target['category'] or target['status']}."]
    if same_category:
        shown = same_category[:5]
        more = f", and {len(same_category) - 5} more" if len(same_category) > 5 else ""
        verb = "shares" if len(same_category) == 1 else "share"
        lines.append(f"{_plural(len(same_category), 'other row')} {verb} that exact category: {', '.join(shown)}{more}.")
    if narration_matches:
        lines.append(f"Narration wording is closely similar to: {', '.join(narration_matches)}.")
    return "\n".join(lines)


FALLBACK_MESSAGE = (
    "I don't have a way to answer that from the reconciliation data. "
    "Try asking about a specific order (\"what happened to order_1032\") "
    "or settlement (\"what happened to setl_a1b2c3\"), a category count "
    "or list (\"how many DUPLICATE exceptions\", \"list UNEXPLAINED "
    "orders\"), open items (\"how many are open\"), confirmed/rejected "
    "counts, the resolution rate, cash value (\"how much is at risk\"), "
    "similar orders (\"any similar orders to order_1032\"), the batch as "
    "a whole (\"how many settlements are in this batch\", \"total "
    "settlement value\", \"status breakdown\"), the biggest or smallest "
    "amount (\"what's the largest exception\"), or \"how can it be "
    "resolved\" once an order or category has come up."
)


def _answer(question: str, context: dict | None, _allow_llm_fallback: bool = True) -> tuple[str, dict]:
    referent = dict(context) if context else {}

    order_id = _extract_order_id(question)
    settlement_id = _extract_settlement_id(question)
    category = _extract_category(question)

    def _updated_referent() -> dict:
        # Only actually commit the order/category this question named once
        # we know the question got a real answer from it -- not merely
        # because an order/category name appeared in the text. A real bug
        # found live: "what about duplicates" mentions a category but (with
        # no count/list trigger) falls through to the honest fallback; if
        # this update ran unconditionally up front regardless of outcome,
        # it still overwrote last_category and popped last_order_id, so the
        # NEXT question ("any similar cases to this one") lost the order
        # context it needed even though "what about duplicates" itself
        # never used it for anything.
        updated = dict(context) if context else {}
        if order_id:
            updated["last_order_id"] = order_id
            rows = [r for r in db.get_all_exceptions() if r["order_id"] == order_id]
            if rows:
                updated["last_category"] = rows[0]["category"]
        elif category:
            updated["last_category"] = category
            updated.pop("last_order_id", None)
        return updated

    if settlement_id and not order_id:
        return _find_settlement(settlement_id), _updated_referent()

    if order_id and not _is_resolution_question(question) and not _is_similarity_question(question):
        return _find_order(order_id), _updated_referent()

    # Checked before _similar_orders/_resolution_guidance: a project
    # question like "why isn't this just an LLM" would otherwise trip
    # _is_resolution_question's bare why_signal and get misrouted to "tell
    # me which order or category you mean" instead of a real answer.
    project_answer = _project_knowledge(question)
    if project_answer is not None:
        return project_answer, _updated_referent()

    similar = _similar_orders(question, context)
    if similar is not None:
        return similar, _updated_referent()

    guidance = _resolution_guidance(question, context)
    if guidance is not None:
        return guidance, _updated_referent()

    # _cash_value first: it's gated behind its own money_signal check (a
    # question has to say "how much money"/"cash value"/etc. to match at
    # all), so checking it before _category_count is safe -- but the
    # order matters, since "how much money is in DUPLICATE" would
    # otherwise get answered as a plain category count first, DUPLICATE
    # being a recognized category name either way.
    for handler in (_cash_value, _resolution_status_count, _category_count,
                     _open_count, _batch_summary, _status_breakdown, _batch_totals,
                     _extreme_amount, _resolution_rate, _category_breakdown):
        result = handler(question)
        if result is not None:
            return result, _updated_referent()

    # None of the keyword shapes matched. Given the extraction and
    # dispatch above, order_id/settlement_id/category are always None by
    # this point -- any question naming one of those is always fully
    # answered earlier (see qa_intent_router.py's module docstring for the
    # regression-tested proof). One retry: ask the gated local model to
    # classify the (necessarily entity-free) question and reformulate it
    # into the exact phrasing a shape above already recognizes, then
    # answer THAT through the same deterministic path -- never generate
    # the answer itself. qa_intent_gate.route_gated returns None for
    # anything not trusted -- today that's everything (see its docstring)
    # -- so this call always falls through cleanly whether or not a future
    # tier is trusted. _allow_llm_fallback=False on the recursive call caps
    # this at one retry -- a bad canonical phrasing can't loop back here a
    # second time.
    if _allow_llm_fallback:
        canonical = qa_intent_gate.route_gated(question)
        if canonical is not None:
            result, updated = _answer(canonical, referent, _allow_llm_fallback=False)
            if result != FALLBACK_MESSAGE:
                return result, updated

    return FALLBACK_MESSAGE, referent


def answer(question: str) -> str:
    """Entry point: returns a plain-text answer grounded in the persisted
    exceptions table, or an honest "don't know" if the question doesn't
    match a recognized shape. Stateless -- no memory of prior questions;
    see `answer_with_context` for the version the chat widget uses."""
    return _answer(question, None)[0]


def answer_with_context(question: str, context: dict | None = None) -> tuple[str, dict]:
    """Same recognized question shapes as `answer`, plus follow-ups like
    "how can it be resolved" that refer back to whichever order or
    category the previous turn was about. `context` is an opaque dict
    the caller round-trips turn to turn (e.g. {"last_order_id": ...,
    "last_category": ...}); returns (answer_text, updated_context)."""
    return _answer(question, context or {})


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(answer(" ".join(sys.argv[1:])))
    else:
        print("Settlement Q&A -- type a question, or Ctrl+C to exit.")
        ctx: dict = {}
        while True:
            try:
                q = input("> ")
            except (EOFError, KeyboardInterrupt):
                break
            if q.strip():
                text, ctx = answer_with_context(q, ctx)
                print(text)
