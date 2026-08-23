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

RESOLVED_STATUSES = {
    "MATCHED", "MATCHED_WITH_VARIANCE", "MATCHED_EXACT_REFERENCE",
    "MATCHED_LEARNED_PATTERN", "MATCHED_AI_ASSISTED", "MATCHED_LOW_CONFIDENCE",
}


def _find_order(question):
    match = ORDER_ID_PATTERN.search(question)
    if not match:
        return None
    order_id = f"order_{match.group(1)}"
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


def _category_count(question):
    q = question.upper().replace(" ", "_")
    for category in KNOWN_CATEGORIES:
        if category in q or category.replace("_", " ") in question.upper():
            rows = db.get_all_exceptions()
            count = sum(1 for r in rows if r["category"] == category)
            return f"{count} row(s) categorized as {category}."
    if "on hold" in question.lower():
        rows = db.get_all_exceptions()
        count = sum(1 for r in rows if r["category"] == "ON_HOLD_BY_RAZORPAY")
        return f"{count} settlement(s) on hold (ON_HOLD_BY_RAZORPAY)."
    return None


def _open_count(question):
    ql = question.lower()
    if any(kw in ql for kw in ("how many open", "how many pending", "how many need", "how many exceptions", "how many are open")):
        open_rows = db.get_open_exceptions()
        return f"{len(open_rows)} row(s) currently open, needing a decision."
    return None


def _resolution_rate(question):
    ql = question.lower()
    if any(kw in ql for kw in ("resolution rate", "how much resolved", "how much is resolved", "overall resolved", "percent resolved")):
        rows = db.get_all_exceptions()
        if not rows:
            return "No batch persisted yet -- run the pipeline first."
        resolved = sum(1 for r in rows if r["status"] in RESOLVED_STATUSES)
        pct = round(100 * resolved / len(rows), 1)
        return f"{pct}% resolved ({resolved} of {len(rows)} rows)."
    return None


def _category_breakdown(question):
    ql = question.lower()
    if any(kw in ql for kw in ("breakdown", "by category", "exceptions by")):
        rows = db.get_all_exceptions()
        counts = Counter(r["category"] for r in rows if r["category"])
        if not counts:
            return "No categorized exceptions in the last run."
        return "\n".join(f"{cat}: {n}" for cat, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    return None


def answer(question):
    order_answer = _find_order(question)
    if order_answer is not None:
        return order_answer

    for handler in (_category_count, _open_count, _resolution_rate, _category_breakdown):
        result = handler(question)
        if result is not None:
            return result

    return (
        "I don't have a way to answer that from the reconciliation data. "
        "Try asking about a specific order (\"what happened to order_1032\"), "
        "a category count (\"how many DUPLICATE exceptions\"), "
        "open items (\"how many are open\"), or the resolution rate."
    )


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(answer(" ".join(sys.argv[1:])))
    else:
        print("Settlement Q&A -- type a question, or Ctrl+C to exit.")
        while True:
            try:
                q = input("> ")
            except (EOFError, KeyboardInterrupt):
                break
            if q.strip():
                print(answer(q))
