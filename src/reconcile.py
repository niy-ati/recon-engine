"""
Multi-source settlement reconciliation engine.

Pass 1   settlement <-> bank, key = UTR, tolerance = amount + date.
Pass 2   settlement <-> ledger, key = order_id.
Pass 2.5 narration_rules lookup (db.py) -- a narration confirmed by a human
         once via review_server.py resolves deterministically on repeat.
Pass 3   fuzzy candidate narrowing (difflib) for unresolved ledger rows.
Pass 4   LLM tie-break over that shortlist, confidence-gated
         (llm_matcher.resolve_with_gate). Only reached after 1/2/2.5/3 fail.
final    anything still unresolved is bucketed into a named exception
         category with a human-readable reason.

Settlement data comes through ingest.py's normalization layer, so a live
Razorpay API source and the synthetic CSV arrive in the same shape before
matching logic sees them. Bank and ledger data stay synthetic-only -- no
live bank/Tally API is available in this environment.
"""
import csv
import difflib
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path

from llm_matcher import resolve_with_gate
from db import get_narration_rule
import ingest

DATE_TOLERANCE_DAYS = 2
AMOUNT_TOLERANCE = 0.01  # rupees
DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def load_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def base_settlement_id(settlement_id):
    return settlement_id[:-4] if settlement_id.endswith("_dup") else settlement_id


def reconcile(data_dir=DEFAULT_DATA_DIR, settlement_source="synthetic"):
    settlements = ingest.load_settlements(source=settlement_source)
    bank = load_csv(f"{data_dir}/bank_statement.csv")
    ledger = load_csv(f"{data_dir}/internal_ledger.csv")

    results = []
    matched_settlement_ids = set()
    matched_bank_rows = set()
    matched_ledger_rows = set()

    # ---------- PASS 1: settlement -> bank ----------
    bank_by_utr = defaultdict(list)
    for i, b in enumerate(bank):
        bank_by_utr[b["utr"]].append(i)

    for s in settlements:
        record = {"order_id": s["order_id"], "settlement_id": s["settlement_id"],
                  "net": float(s["net"]), "gst_on_mdr": float(s["gst_on_mdr"]),
                  "status": None, "category": None, "reason": None, "stage": []}

        candidates = bank_by_utr.get(s["utr"], [])
        bank_match = None
        for bi in candidates:
            if bi in matched_bank_rows:
                continue
            b = bank[bi]
            amt_diff = abs(float(b["credited_amount"]) - float(s["net"]))
            date_diff = abs((parse_date(b["value_date"]) - parse_date(s["settlement_date"])).days)
            if amt_diff <= AMOUNT_TOLERANCE and date_diff <= DATE_TOLERANCE_DAYS:
                bank_match = bi
                break
            elif amt_diff <= AMOUNT_TOLERANCE and date_diff > DATE_TOLERANCE_DAYS:
                bank_match = bi
                record["stage"].append(f"date offset {date_diff}d beyond {DATE_TOLERANCE_DAYS}d tolerance")

        if bank_match is not None:
            matched_bank_rows.add(bank_match)
            matched_settlement_ids.add(s["settlement_id"])
            record["stage"].append("PASS1: settlement<->bank matched on UTR+amount+date")
        elif s.get("on_hold"):
            # Real, documented field on the settlement recon line. Checked
            # before the rounding fallback below since there's no bank
            # credit to be "near" when the payout hasn't moved at all.
            matched_settlement_ids.add(s["settlement_id"])
            record["category"] = "ON_HOLD_BY_RAZORPAY"
            record["reason"] = ("This payment is known to be held by Razorpay (on_hold=true in "
                                 "their own settlement recon API) -- this is not a reconciliation "
                                 "error and not evidence of a lost transaction. It's a status your "
                                 "books should reflect honestly, distinct from a normal T+2 pipeline "
                                 "delay or a genuinely unexplained missing settlement.")
            record["status"] = "EXCEPTION"
            record["stage"].append("PASS1: on_hold=true on the settlement recon line")
        else:
            # Same UTR, amount off by a small margin -> rounding/fee drift.
            # Tracks the closest candidate by amt_diff rather than the last
            # one iterated, so multiple same-UTR rows within tolerance
            # resolve to the nearest match, not an arbitrary one.
            near = None
            for bi in candidates:
                if bi in matched_bank_rows:
                    continue
                b = bank[bi]
                amt_diff = abs(float(b["credited_amount"]) - float(s["net"]))
                if amt_diff <= 2.0 and (near is None or amt_diff < near[1]):
                    near = (bi, amt_diff)
            if near:
                bi, diff = near
                matched_bank_rows.add(bi)
                matched_settlement_ids.add(s["settlement_id"])
                record["category"] = "ROUNDING" if diff < 1.0 else "TAX_DEDUCTION"
                record["reason"] = f"Bank credit found for this UTR but net amount differs by Rs.{diff:.2f} -- likely GST-on-MDR rounding drift, not a genuine mismatch."
                record["status"] = "MATCHED_WITH_VARIANCE"
            else:
                # Duplicate settlement_id for the same order/UTR -> compare
                # base IDs both ways so the label doesn't depend on which
                # of the pair claims the bank match first.
                if any(
                    base_settlement_id(other["settlement_id"]) == base_settlement_id(s["settlement_id"])
                    and other is not s
                    for other in settlements
                ):
                    record["category"] = "DUPLICATE"
                    record["reason"] = "This settlement_id appears twice for the same order/UTR; only one bank credit exists. Likely a duplicate export row, not a missing payout."
                    record["status"] = "EXCEPTION"
                else:
                    record["category"] = "UNEXPLAINED"
                    record["reason"] = "No bank credit found for this UTR within tolerance. Could be pending settlement, or a reporting error -- needs manual bank statement check."
                    record["status"] = "EXCEPTION"

        if record["status"] is None:
            record["status"] = "MATCHED"

        # ---------- PASS 2: settlement -> ledger via order_id ----------
        ledger_match = None
        for li, l in enumerate(ledger):
            if li in matched_ledger_rows:
                continue
            if l["order_ref"] == s["order_id"]:
                ledger_match = li
                break

        if ledger_match is not None:
            matched_ledger_rows.add(ledger_match)
            l = ledger[ledger_match]
            gst_diff = abs(float(l["gst_line"]) - float(s["gst_on_mdr"]))
            if gst_diff > 1.0 and record["category"] is None:
                record["category"] = "TAX_DEDUCTION"
                record["reason"] = f"GST-on-MDR in ledger (Rs.{l['gst_line']}) differs from settlement report (Rs.{s['gst_on_mdr']}) by Rs.{gst_diff:.2f} -- check against Razorpay's monthly tax invoice before filing ITC."
                record["status"] = "MATCHED_WITH_VARIANCE"
            elif "REFUND" in l["narration"].upper() and record["category"] is None:
                record["category"] = "PARTIAL_PAYMENT"
                record["reason"] = "Ledger narration indicates a partial refund was netted into this settlement -- net amount is gross minus refund, not a mismatch."
                record["status"] = "MATCHED_WITH_VARIANCE"
            record["stage"].append("PASS2: settlement<->ledger matched on order_id")
        else:
            record["_needs_pass3"] = True

        results.append(record)

    # ---------- PASS 2.5: narration_rules lookup ----------
    # A narration a human already confirmed once resolves deterministically,
    # skipping fuzzy matching and the arbiter entirely.
    for li, l in enumerate(ledger):
        if li in matched_ledger_rows or l["order_ref"]:
            continue
        hit = get_narration_rule(l["narration"])
        if hit is None:
            continue
        matched_ledger_rows.add(li)
        for r in results:
            if r["order_id"] == hit["order_id"] and r.get("_needs_pass3") and r["category"] is None:
                r["stage"].append(
                    f"PASS2.5: narration_rules match -- narration '{l['narration']}' was "
                    f"human-confirmed on {hit['confirmed_at']} -> order_id {hit['order_id']}"
                )
                r["status"] = "MATCHED_LEARNED_PATTERN"
                r["narration"] = l["narration"]
                del r["_needs_pass3"]
                break

    # ---------- PASS 2.75: unambiguous exact digit reference ----------
    # If exactly one still-unmatched order's numeric suffix appears verbatim
    # in a narration, that's the same signal Pass 2 trusts via order_ref,
    # just embedded in free text -- resolve it without spending an arbiter
    # call. Zero or multiple hits are genuine ambiguity, left for Pass 3/4.
    #
    # `r["category"] is None` excludes settlements already resolved to a
    # definite category in Pass 1 (in practice, DUPLICATE settlements whose
    # ledger row was claimed by their sibling in Pass 2, so they still
    # carry `_needs_pass3`). Without this guard those order_ids stay
    # eligible as fuzzy-match candidates below and can crowd a genuine
    # match out of the shortlist -- already-resolved settlements don't need
    # arbitration.
    unmatched_order_ids = [r["order_id"] for r in results if r.get("_needs_pass3") and r["category"] is None]
    for li, l in enumerate(ledger):
        if li in matched_ledger_rows or l["order_ref"]:
            continue
        exact_hits = [oid for oid in unmatched_order_ids if oid.split("_")[1] in l["narration"]]
        if len(exact_hits) != 1:
            continue
        oid = exact_hits[0]
        matched_ledger_rows.add(li)
        for r in results:
            if r["order_id"] == oid and r.get("_needs_pass3") and r["category"] is None:
                r["stage"].append(
                    f"PASS2.75: unambiguous exact digit reference -- narration '{l['narration']}' "
                    f"contains order {oid}'s number and no other unmatched order's -- resolved "
                    f"deterministically, no arbiter call needed"
                )
                r["status"] = "MATCHED_EXACT_REFERENCE"
                r["narration"] = l["narration"]
                del r["_needs_pass3"]
                break

    # ---------- PASS 3 + 4: fuzzy shortlist -> confidence-gated arbiter ----------
    # Everything reaching here has no exact digit match (needs fuzzy
    # similarity) or multiple conflicting ones (needs disambiguation).
    # Pass 3 builds the shortlist via difflib; the arbiter in llm_matcher.py
    # picks from it (Pass 4) and its confidence gate decides auto_applied.
    unmatched_order_ids = [r["order_id"] for r in results if r.get("_needs_pass3") and r["category"] is None]
    candidate_strings = {f"order {oid}": oid for oid in unmatched_order_ids}
    fuzzy_matches = []
    for li, l in enumerate(ledger):
        if li in matched_ledger_rows:
            continue
        if l["order_ref"]:  # had an order_ref but didn't match a settlement -> true orphan
            continue

        exact_hits = [oid for oid in unmatched_order_ids if oid.split("_")[1] in l["narration"]]
        similarity_hits = [candidate_strings[s] for s in
                            difflib.get_close_matches(l["narration"], list(candidate_strings.keys()), n=3, cutoff=0.3)]
        shortlist = list(dict.fromkeys(exact_hits + similarity_hits))[:3]
        if not shortlist:
            continue

        arb = resolve_with_gate(l["narration"], shortlist)
        if arb.candidate_id is None:
            continue

        matched_ledger_rows.add(li)
        for r in results:
            if r["order_id"] == arb.candidate_id and r.get("_needs_pass3") and r["category"] is None:
                r["stage"].append(
                    f"PASS3: shortlist={shortlist} -> PASS4 arbiter picked {arb.candidate_id} "
                    f"(confidence={arb.confidence:.2f}, auto_applied={arb.auto_applied}) :: {arb.reason}"
                )
                if arb.auto_applied:
                    r["status"] = "MATCHED_AI_ASSISTED"
                else:
                    r["status"] = "MATCHED_LOW_CONFIDENCE"
                    r["category"] = r["category"] or "FUZZY_MATCH_NEEDS_REVIEW"
                r["narration"] = l["narration"]
                fuzzy_matches.append((arb.candidate_id, l["invoice_id"]))
                del r["_needs_pass3"]
                break

    # Anything still needing Pass 3 with no fuzzy hit has no ledger link at all.
    for r in results:
        if r.get("_needs_pass3"):
            r["category"] = r["category"] or "UNEXPLAINED"
            r["reason"] = r["reason"] or "Settlement row has no corresponding ledger/invoice entry -- order may exist outside the primary OMS."
            if r["status"] == "MATCHED":
                r["status"] = "EXCEPTION"
            del r["_needs_pass3"]

    # ---------- Bank rows with no settlement at all ----------
    orphan_bank = [b for i, b in enumerate(bank) if i not in matched_bank_rows]
    for b in orphan_bank:
        results.append({"order_id": None, "settlement_id": None, "net": float(b["credited_amount"]),
                         "status": "EXCEPTION", "category": "UNEXPLAINED",
                         "reason": f"Bank credit of Rs.{b['credited_amount']} on {b['value_date']} has no matching settlement_id anywhere in the settlement report.",
                         "stage": ["No settlement counterpart found"]})

    # ---------- Ledger rows never matched at all ----------
    for li, l in enumerate(ledger):
        if li not in matched_ledger_rows:
            is_afa = "AFA_MANDATE_HOLD" in l["narration"]
            results.append({"order_id": l["order_ref"] or None, "settlement_id": None, "net": float(l["amount"]),
                             "status": "EXCEPTION",
                             "category": "AFA_MANDATE_HOLD" if is_afa else "UNEXPLAINED",
                             "reason": ("Charge never settled -- ledger shows a subscription renewal that crossed the RBI e-mandate AFA threshold (>Rs.15,000) and needs a compliant step-up re-authentication, not a blind retry."
                                        if is_afa else
                                        "Ledger entry has no matching settlement or bank record -- possibly an invoice raised for a payment that was never actually captured."),
                             "stage": ["No settlement or bank counterpart found"]})

    return results


def summarize(results):
    total = len(results)
    matched = sum(1 for r in results if r["status"] == "MATCHED")
    matched_variance = sum(1 for r in results if r["status"] == "MATCHED_WITH_VARIANCE")
    exact_reference = sum(1 for r in results if r["status"] == "MATCHED_EXACT_REFERENCE")
    learned = sum(1 for r in results if r["status"] == "MATCHED_LEARNED_PATTERN")
    ai_assisted = sum(1 for r in results if r["status"] == "MATCHED_AI_ASSISTED")
    low_conf = sum(1 for r in results if r["status"] == "MATCHED_LOW_CONFIDENCE")
    exceptions = sum(1 for r in results if r["status"] == "EXCEPTION")

    by_category = defaultdict(int)
    for r in results:
        if r["category"]:
            by_category[r["category"]] += 1

    resolved = matched + matched_variance + exact_reference + learned + ai_assisted + low_conf

    return {
        "total_rows": total,
        "clean_match_pct": round(100 * matched / total, 1),
        "matched_with_variance_pct": round(100 * matched_variance / total, 1),
        "exact_reference_pct": round(100 * exact_reference / total, 1),
        "learned_pattern_pct": round(100 * learned / total, 1),
        "ai_assisted_auto_applied_pct": round(100 * ai_assisted / total, 1),
        "fuzzy_matched_needs_review_pct": round(100 * low_conf / total, 1),
        "unresolved_exception_pct": round(100 * exceptions / total, 1),
        "overall_resolved_pct": round(100 * resolved / total, 1),
        "exceptions_by_category": dict(by_category),
    }


if __name__ == "__main__":
    results = reconcile()
    summary = summarize(results)

    print("=== RECONCILIATION SUMMARY ===")
    for k, v in summary.items():
        print(f"{k}: {v}")

    print("\n=== EXCEPTION DETAIL (first 10) ===")
    for r in [r for r in results if r["status"] == "EXCEPTION"][:10]:
        print(f"- order={r['order_id']} category={r['category']} :: {r['reason']}")
