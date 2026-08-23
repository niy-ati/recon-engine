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
  - "how can it be resolved" / "how do I fix order_1032" -- canned,
    per-category guidance text, not a generated answer. Resolves the
    order/category from the question itself, or falls back to whatever
    order/category the last turn was about (see `context` below).
"""
import re
import sys
from collections import Counter

import db

ORDER_ID_PATTERN = re.compile(r"\border[_\s]?(\d+)\b", re.IGNORECASE)

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
        "dropped from the audit trail."
    ),
    "UNEXPLAINED": (
        "No UTR, order ID, or narration reference tied this row to anything "
        "in the ledger or bank statement. Check the raw bank narration by "
        "hand, or wait for the next settlement cycle in case the missing "
        "reference shows up in a later transfer."
    ),
    "PARTIAL_PAYMENT": (
        "The settled amount is less than the order amount. Confirm with the "
        "payment gateway whether the balance was split across settlements, "
        "then reconcile the remainder against the next cycle."
    ),
    "TAX_DEDUCTION": (
        "The variance matches a TDS/GST-style deduction. Verify the "
        "deducted amount against the applicable tax rate, then confirm it "
        "as an explained variance rather than a genuine shortfall."
    ),
    "ROUNDING": (
        "The variance is a sub-rupee rounding difference. Safe to confirm "
        "as-is -- no money is actually missing."
    ),
    "FUZZY_MATCH_NEEDS_REVIEW": (
        "The AI arbiter proposed a candidate match from narration "
        "similarity, but it was deliberately not auto-applied. Open the "
        "review queue, check the highlighted narration evidence in the "
        "audit trail, and click Confirm if it looks right -- or Reject if "
        "it doesn't."
    ),
    "AFA_MANDATE_HOLD": (
        "This settlement is held by Razorpay's own AFA/e-mandate step-up "
        "flow, triggered by the RBI's Rs 15,000 threshold on recurring "
        "subscription charges. There's no action on your end -- it "
        "releases once the mandate step-up completes or the transaction is "
        "declined."
    ),
    "ON_HOLD_BY_RAZORPAY": (
        "Razorpay itself is holding this settlement (compliance or risk "
        "review, typically). Check the hold reason on the Razorpay "
        "dashboard directly -- this system only sees what's in the "
        "settlement export, not Razorpay's internal hold reasoning."
    ),
}

def _is_resolution_question(question: str) -> bool:
    """Matches "how can it/order_2/a DUPLICATE be resolved", "how do I fix
    this", "what should I do", "what's the fix" -- any phrasing that pairs
    a question word with resolve/fix, not just the exact fixed phrases
    ("resolv" also matches "unresolved" on its own, so it's paired with a
    question-word check rather than used alone)."""
    ql = question.lower()
    if "resolv" not in ql and "fix" not in ql:
        return False
    return any(w in ql for w in ("how", "what should", "what's the", "what is the"))


def _extract_order_id(question: str) -> str | None:
    match = ORDER_ID_PATTERN.search(question)
    return f"order_{match.group(1)}" if match else None


def _extract_category(question: str) -> str | None:
    q = question.upper().replace(" ", "_")
    for category in KNOWN_CATEGORIES:
        if category in q or category.replace("_", " ") in question.upper():
            return category
    if "on hold" in question.lower():
        return "ON_HOLD_BY_RAZORPAY"
    return None


def _find_order(order_id: str) -> str:
    rows = [r for r in db.get_all_exceptions() if r["order_id"] == order_id]
    if not rows:
        return f"No record of {order_id} in the last reconciliation run."

    lines = [f"{order_id}: {len(rows)} row(s) found."]
    for r in rows:
        lines.append(
            f"  status={r['status']}"
            + (f" category={r['category']}" if r['category'] else "")
            + (f" -- {r['reason']}" if r['reason'] else "")
        )
        if r["resolution_status"] != "OPEN":
            lines.append(f"  human decision: {r['resolution_status']}"
                          + (f" ({r['resolution_note']})" if r["resolution_note"] else ""))
    return "\n".join(lines)


def _category_count(question: str) -> str | None:
    category = _extract_category(question)
    if category is None:
        return None
    rows = db.get_all_exceptions()
    count = sum(1 for r in rows if r["category"] == category)
    if category == "ON_HOLD_BY_RAZORPAY" and "on hold" in question.lower():
        return f"{count} settlement(s) on hold (ON_HOLD_BY_RAZORPAY)."
    return f"{count} row(s) categorized as {category}."


def _open_count(question: str) -> str | None:
    ql = question.lower()
    if any(kw in ql for kw in ("how many open", "how many pending", "how many need", "how many exceptions", "how many are open")):
        open_rows = db.get_open_exceptions()
        return f"{len(open_rows)} row(s) currently open, needing a decision."
    return None


def _resolution_rate(question: str) -> str | None:
    ql = question.lower()
    if any(kw in ql for kw in ("resolution rate", "how much resolved", "how much is resolved", "overall resolved", "percent resolved")):
        rows = db.get_all_exceptions()
        if not rows:
            return "No batch persisted yet -- run the pipeline first."
        resolved = sum(1 for r in rows if r["status"] in RESOLVED_STATUSES)
        pct = round(100 * resolved / len(rows), 1)
        return f"{pct}% resolved ({resolved} of {len(rows)} rows)."
    return None


def _category_breakdown(question: str) -> str | None:
    ql = question.lower()
    if any(kw in ql for kw in ("breakdown", "by category", "exceptions by")):
        rows = db.get_all_exceptions()
        counts = Counter(r["category"] for r in rows if r["category"])
        if not counts:
            return "No categorized exceptions in the last run."
        return "\n".join(f"{cat}: {n}" for cat, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    return None


def _resolution_guidance(question: str, context: dict | None) -> str | None:
    if not _is_resolution_question(question):
        return None

    order_id = _extract_order_id(question)
    category = _extract_category(question)

    if order_id:
        rows = [r for r in db.get_all_exceptions() if r["order_id"] == order_id]
        if not rows:
            return f"No record of {order_id} in the last reconciliation run."
        category = rows[0]["category"]
    elif category is None and context:
        order_id = context.get("last_order_id")
        category = context.get("last_category")
        if order_id and not category:
            rows = [r for r in db.get_all_exceptions() if r["order_id"] == order_id]
            if rows:
                category = rows[0]["category"]

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


def _answer(question: str, context: dict | None) -> tuple[str, dict]:
    referent = dict(context) if context else {}

    order_id = _extract_order_id(question)
    category = _extract_category(question)
    if order_id:
        referent["last_order_id"] = order_id
        rows = [r for r in db.get_all_exceptions() if r["order_id"] == order_id]
        if rows:
            referent["last_category"] = rows[0]["category"]
    elif category:
        referent["last_category"] = category
        referent.pop("last_order_id", None)

    if order_id and not _is_resolution_question(question):
        return _find_order(order_id), referent

    guidance = _resolution_guidance(question, context)
    if guidance is not None:
        return guidance, referent

    for handler in (_category_count, _open_count, _resolution_rate, _category_breakdown):
        result = handler(question)
        if result is not None:
            return result, referent

    return (
        "I don't have a way to answer that from the reconciliation data. "
        "Try asking about a specific order (\"what happened to order_1032\"), "
        "a category count (\"how many DUPLICATE exceptions\"), "
        "open items (\"how many are open\"), the resolution rate, or "
        "\"how can it be resolved\" once an order or category has come up.",
        referent,
    )


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
