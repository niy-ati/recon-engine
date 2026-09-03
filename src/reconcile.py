"""
Multi-source settlement reconciliation engine.

Pass 1   settlement <-> bank, key = UTR, tolerance = amount + date.
Pass 2   settlement <-> ledger, key = order_id.
Pass 2.5 narration_rules lookup (db.py) -- a narration confirmed by a human
         once via review_server.py resolves deterministically on repeat.
Pass 2.6 narration_templates lookup (db.py) -- a differently-numbered
         narration from the same recurring template a human already
         confirmed once resolves too, generalizing only the digit
         reference, never the surrounding text.
Pass 2.75 exact digit reference (no learned pattern needed at all).
Pass 3   fuzzy candidate narrowing (difflib) for unresolved ledger rows.
Pass 4   LLM tie-break over that shortlist, confidence-gated
         (validation_gate.resolve_with_gate). Only reached after 1/2/2.5/2.6/2.75/3 fail.
final    anything still unresolved is bucketed into a named exception
         category with a human-readable reason.

Settlement data comes through ingest.py's normalization layer, so a live
Razorpay API source and the synthetic CSV arrive in the same shape before
matching logic sees them. Bank and ledger data stay synthetic-only -- no
live bank/Tally API is available in this environment.
"""
import csv
import difflib
import re
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict
from pathlib import Path

from validation_gate import resolve_with_gate, CONFIDENCE_AUTO_ACCEPT
from db import get_all_narration_rules, get_narration_templates
import ingest

DATE_TOLERANCE_DAYS = 2
AMOUNT_TOLERANCE = 0.01  # rupees
# A real, separate gate from AMOUNT_TOLERANCE above: that one is for the
# exact passes (1/2/2.5/2.6/2.75), where an amount is expected to line up
# almost exactly. Pass 4's shortlist is narrowed by narration TEXT
# similarity alone -- nothing before this point has ever checked whether
# the amounts involved actually agree, even for a fully gated, trusted-tier
# match. A looser rupee tolerance here, not AMOUNT_TOLERANCE's paisa-level
# one, since this is a sanity check against a wrong candidate, not a
# rounding-drift check against the right one.
PASS4_AMOUNT_SANITY_TOLERANCE_RS = 2.0
DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def load_csv(path: str | Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def base_settlement_id(settlement_id: str) -> str:
    return settlement_id[:-4] if settlement_id.endswith("_dup") else settlement_id


def new_correlation_id() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def reconcile(
    data_dir: str | Path = DEFAULT_DATA_DIR,
    settlement_source: str = "synthetic",
    correlation_id: str | None = None,
) -> list[dict]:
    """Runs Pass 1 through the final categorization step over one batch and
    returns one result dict per settlement/bank/ledger row. See the module
    docstring above for what each pass does.

    settlement_source: 'synthetic' | 'live' | 'with_gateway_b' (see
    ingest.load_settlements). correlation_id: reused as db.py's run_id if
    not given explicitly, one is generated for this call.
    """
    correlation_id = correlation_id or new_correlation_id()

    def log(pass_name, action, detail, confidence=None):
        # Structured, not a free-text string: machine-parseable, and
        # correlation_id ties every entry back to the run that produced it
        # -- the same identifier db.py stores as run_id on the row itself.
        return {
            "pass": pass_name, "action": action, "detail": detail,
            "confidence": confidence,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "correlation_id": correlation_id,
        }

    settlements = ingest.load_settlements(source=settlement_source, data_dir=data_dir)
    bank = load_csv(f"{data_dir}/bank_statement.csv")
    ledger = load_csv(f"{data_dir}/internal_ledger.csv")

    results = []
    matched_bank_rows = set()
    matched_ledger_rows = set()

    # ---------- PASS 1: settlement -> bank ----------
    bank_by_utr = defaultdict(list)
    for i, b in enumerate(bank):
        bank_by_utr[b["utr"]].append(i)

    # Pass 2's own index, built alongside Pass 1's for the same reason:
    # scanning the full ledger for every settlement (`for li, l in
    # enumerate(ledger): if l["order_ref"] == s["order_id"]`) is an O(n x m)
    # cost that's invisible at this demo's 80-150 rows but measured at
    # ~real cost at scale (profiled on a 3,000-row synthetic batch). One
    # dict, built once, same exact-match semantics as the scan it replaces.
    ledger_by_order_ref = defaultdict(list)
    for li, l in enumerate(ledger):
        if l["order_ref"]:
            ledger_by_order_ref[l["order_ref"]].append(li)

    # Every UTR a settlement claims as its own -- computed once, up front,
    # order-independent. The cross-UTR mismatch check below must never
    # touch a bank row whose UTR belongs to some OTHER settlement's own
    # reference, even one not processed yet: that row is someone else's
    # rightful primary match, not a mismatch up for grabs.
    settlement_utrs = {s["utr"] for s in settlements}

    for s in settlements:
        # match_key identifies this logical row across separate runs of the
        # same batch (db.py upserts on it instead of blanket-deleting, so a
        # re-run never wipes a human's earlier confirm/reject decision).
        record = {"order_id": s["order_id"], "settlement_id": s["settlement_id"],
                  "net": float(s["net"]), "gst_on_mdr": float(s["gst_on_mdr"]),
                  "match_key": f"settlement:{s['settlement_id']}",
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
                record["stage"].append(log("1", "date_tolerance_exceeded",
                                            f"date offset {date_diff}d beyond {DATE_TOLERANCE_DAYS}d tolerance"))

        if bank_match is not None:
            matched_bank_rows.add(bank_match)
            record["stage"].append(log("1", "matched", "settlement<->bank matched on UTR+amount+date"))
        elif s.get("on_hold"):
            # Real, documented field on the settlement recon line. Checked
            # before the rounding fallback below since there's no bank
            # credit to be "near" when the payout hasn't moved at all.
            record["category"] = "ON_HOLD_BY_RAZORPAY"
            record["reason"] = ("This payment is known to be held by Razorpay (on_hold=true in "
                                 "their own settlement recon API) -- this is not a reconciliation "
                                 "error and not evidence of a lost transaction. It's a status your "
                                 "books should reflect honestly, distinct from a normal T+2 pipeline "
                                 "delay or a genuinely unexplained missing settlement.")
            record["status"] = "EXCEPTION"
            record["stage"].append(log("1", "on_hold", "on_hold=true on the settlement recon line"))
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
                record["category"] = "ROUNDING" if diff < 1.0 else "TAX_DEDUCTION"
                record["reason"] = f"Bank credit found for this UTR but net amount differs by Rs.{diff:.2f} -- likely GST-on-MDR rounding drift, not a genuine mismatch."
                record["status"] = "MATCHED_WITH_VARIANCE"
                record["stage"].append(log(
                    "1", "amount_drift",
                    f"settlement UTR {s['utr']} matched a bank row within Rs.{diff:.2f}, inside the Rs.2.00 rounding tolerance",
                ))
            else:
                # Two-tier UTR mismatch: Razorpay's real settlement UTR is
                # two-tier (batch-level vs. per-line, see README), so the
                # UTR this settlement reports and the UTR the bank actually
                # posted under can legitimately differ for the same real
                # transfer. Before calling this a missing payout, check
                # every OTHER unmatched bank row (not just this UTR's own
                # candidates) for one that matches on amount+date exactly --
                # but only resolve it if exactly one such row exists.
                # Two-plus is a same-amount/same-day coincidence, genuinely
                # ambiguous, and gets left for the DUPLICATE/UNEXPLAINED
                # path below rather than guessed at.
                cross_utr_hits = []
                for bi, b in enumerate(bank):
                    if bi in matched_bank_rows or b["utr"] in settlement_utrs:
                        continue
                    amt_diff = abs(float(b["credited_amount"]) - float(s["net"]))
                    date_diff = abs((parse_date(b["value_date"]) - parse_date(s["settlement_date"])).days)
                    if amt_diff <= AMOUNT_TOLERANCE and date_diff <= DATE_TOLERANCE_DAYS:
                        cross_utr_hits.append(bi)

                if len(cross_utr_hits) == 1:
                    bi = cross_utr_hits[0]
                    bank_utr = bank[bi]["utr"]
                    matched_bank_rows.add(bi)
                    record["category"] = "UTR_LEVEL_MISMATCH"
                    record["reason"] = (
                        f"No bank row under this settlement's own UTR ({s['utr']}), but a bank credit "
                        f"matching the exact amount and date was found under UTR {bank_utr} instead -- "
                        f"Razorpay's settlement UTR is two-tier (batch-level vs. per-line), so this reads "
                        f"as a reference mismatch, not a missing payout. The money arrived; the UTR label doesn't."
                    )
                    record["status"] = "MATCHED_WITH_VARIANCE"
                    record["stage"].append(log(
                        "1", "utr_mismatch",
                        f"settlement UTR {s['utr']} had no bank row, but amount+date uniquely matched bank UTR {bank_utr}",
                    ))
                # Duplicate settlement_id for the same order/UTR -- compare
                # base IDs both ways so the label doesn't depend on which
                # of the pair claims the bank match first.
                elif any(
                    base_settlement_id(other["settlement_id"]) == base_settlement_id(s["settlement_id"])
                    and other is not s
                    for other in settlements
                ):
                    record["category"] = "DUPLICATE"
                    record["reason"] = "This settlement_id appears twice for the same order/UTR; only one bank credit exists. Likely a duplicate export row, not a missing payout."
                    record["status"] = "EXCEPTION"
                    record["stage"].append(log(
                        "1", "duplicate_settlement_id",
                        f"settlement_id {s['settlement_id']} shares base ID {base_settlement_id(s['settlement_id'])} with another settlement row for UTR {s['utr']}; only one bank credit exists for that base ID",
                    ))
                else:
                    record["category"] = "UNEXPLAINED"
                    record["reason"] = "No bank credit found for this UTR within tolerance. Could be pending settlement, or a reporting error -- needs manual bank statement check."
                    record["status"] = "EXCEPTION"
                    record["stage"].append(log(
                        "1", "no_bank_match",
                        f"no bank row under UTR {s['utr']} within Rs.{AMOUNT_TOLERANCE} / {DATE_TOLERANCE_DAYS}d tolerance, and no unique cross-UTR or duplicate-ID candidate found",
                    ))

        if record["status"] is None:
            record["status"] = "MATCHED"

        # ---------- PASS 2: settlement -> ledger via order_id ----------
        ledger_match = None
        for li in ledger_by_order_ref.get(s["order_id"], []):
            if li not in matched_ledger_rows:
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
            record["stage"].append(log("2", "matched", "settlement<->ledger matched on order_id"))
        else:
            record["_needs_pass3"] = True

        results.append(record)

    # `order_id -> [records]` built once, over the whole batch, instead of
    # every pass below scanning `results` end to end for every unresolved
    # row -- the O(n x m) cost this file used to accept as a known,
    # deliberately-unfixed limitation. Records are the same dict objects
    # as in `results` (not copies), so mutating one through this index
    # mutates the one `results` itself sees too -- same data, just found
    # without a full scan.
    results_by_order_id = defaultdict(list)
    for r in results:
        if r["order_id"] is not None:
            results_by_order_id[r["order_id"]].append(r)

    # ---------- PASS 2.5: narration_rules lookup ----------
    # A narration a human already confirmed once resolves deterministically,
    # skipping fuzzy matching and the arbiter entirely. One query for every
    # learned rule instead of one fresh SQLite connection (re-running the
    # schema/migration script) per unmatched ledger row -- profiled as the
    # single largest cost in the whole pipeline at scale, bigger than the
    # matching logic itself. See db.get_all_narration_rules.
    narration_rules = get_all_narration_rules()
    for li, l in enumerate(ledger):
        if li in matched_ledger_rows or l["order_ref"]:
            continue
        hit = narration_rules.get(l["narration"])
        if hit is None:
            continue
        matched_ledger_rows.add(li)
        for r in results_by_order_id.get(hit["order_id"], []):
            if r.get("_needs_pass3") and r["category"] is None:
                r["stage"].append(log(
                    "2.5", "learned_pattern",
                    f"narration_rules match -- narration '{l['narration']}' was human-confirmed "
                    f"on {hit['confirmed_at']} -> order_id {hit['order_id']}",
                ))
                r["status"] = "MATCHED_LEARNED_PATTERN"
                r["narration"] = l["narration"]
                del r["_needs_pass3"]
                break

    # ---------- PASS 2.6: narration_templates lookup ----------
    # A human confirming order_1042's narration also generalized it into a
    # template with the digit reference replaced by {REF} (db.py's
    # resolve_exception). A DIFFERENT order's narration from the same
    # recurring template (same surrounding text, a new reference) matches
    # here -- but only resolves if the captured digits correspond to
    # exactly one order still needing a match, the same discipline every
    # other deterministic pass in this file holds to. This is the one
    # place narration_rules generalizes beyond an exact string repeat --
    # see README, 'the learned pattern store memorizes exact strings.'
    templates = get_narration_templates()
    compiled_templates = []
    for t in templates:
        parts = t["template"].split("{REF}")
        if len(parts) != 2:
            continue  # defensive: a template must carry exactly one placeholder
        compiled_templates.append((re.compile(re.escape(parts[0]) + r"(\d+)" + re.escape(parts[1])), t))

    if compiled_templates:
        unmatched_order_ids = [r["order_id"] for r in results if r.get("_needs_pass3") and r["category"] is None]
        unmatched_by_suffix = defaultdict(list)
        for oid in unmatched_order_ids:
            unmatched_by_suffix[oid.split("_")[1]].append(oid)

        for li, l in enumerate(ledger):
            if li in matched_ledger_rows or l["order_ref"]:
                continue
            for regex, t in compiled_templates:
                match = regex.search(l["narration"])
                if not match:
                    continue
                candidates = unmatched_by_suffix.get(match.group(1), [])
                if len(candidates) != 1:
                    continue  # zero or ambiguous -- decline, don't guess
                oid = candidates[0]
                matched_ledger_rows.add(li)
                for r in results_by_order_id.get(oid, []):
                    if r.get("_needs_pass3") and r["category"] is None:
                        r["stage"].append(log(
                            "2.6", "learned_template",
                            f"narration_templates match -- narration '{l['narration']}' fits a template "
                            f"human-confirmed on {t['confirmed_at']}, captured reference "
                            f"{match.group(1)} uniquely identifies order {oid}",
                        ))
                        r["status"] = "MATCHED_LEARNED_PATTERN"
                        r["narration"] = l["narration"]
                        del r["_needs_pass3"]
                        break
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
    # Each order's digit suffix computed once per pass, not once per
    # (order, ledger-row) pair -- the substring check below still costs
    # O(candidates) per ledger row (unavoidable without changing what
    # "unambiguous exact reference" means), but this removes the redundant
    # .split() call that used to run for the same order on every single
    # ledger row (profiled: 9,000,000 calls on a 3,000-row batch where a
    # straight per-pair count would predict exactly that).
    unmatched_suffixes = [(oid, oid.split("_")[1]) for oid in unmatched_order_ids]
    for li, l in enumerate(ledger):
        if li in matched_ledger_rows or l["order_ref"]:
            continue
        exact_hits = [oid for oid, suffix in unmatched_suffixes if suffix in l["narration"]]
        if len(exact_hits) != 1:
            continue
        oid = exact_hits[0]
        matched_ledger_rows.add(li)
        for r in results_by_order_id.get(oid, []):
            if r.get("_needs_pass3") and r["category"] is None:
                r["stage"].append(log(
                    "2.75", "exact_reference",
                    f"unambiguous exact digit reference -- narration '{l['narration']}' contains "
                    f"order {oid}'s number and no other unmatched order's -- resolved "
                    f"deterministically, no arbiter call needed",
                ))
                r["status"] = "MATCHED_EXACT_REFERENCE"
                r["narration"] = l["narration"]
                del r["_needs_pass3"]
                break

    # ---------- PASS 3 + 4: fuzzy shortlist -> confidence-gated arbiter ----------
    # Everything reaching here has no exact digit match (needs fuzzy
    # similarity) or multiple conflicting ones (needs disambiguation).
    # Pass 3 builds the shortlist via difflib; validation_gate.resolve_with_gate()
    # picks from it (Pass 4) and its confidence gate decides auto_applied --
    # this file has no way to reach the raw, ungated arbiter directly.
    unmatched_order_ids = [r["order_id"] for r in results if r.get("_needs_pass3") and r["category"] is None]
    unmatched_suffixes = [(oid, oid.split("_")[1]) for oid in unmatched_order_ids]
    candidate_strings = {f"order {oid}": oid for oid in unmatched_order_ids}
    fuzzy_matches = []
    for li, l in enumerate(ledger):
        if li in matched_ledger_rows:
            continue
        if l["order_ref"]:  # had an order_ref but didn't match a settlement -> true orphan
            continue

        exact_hits = [oid for oid, suffix in unmatched_suffixes if suffix in l["narration"]]
        similarity_hits = [candidate_strings[s] for s in
                            difflib.get_close_matches(l["narration"], list(candidate_strings.keys()), n=3, cutoff=0.3)]
        shortlist = list(dict.fromkeys(exact_hits + similarity_hits))[:3]
        if not shortlist:
            continue

        arb = resolve_with_gate(l["narration"], shortlist)
        if arb.candidate_id is None:
            continue

        # A confident, trusted-tier arbiter result still says nothing
        # about whether the AMOUNTS involved actually agree -- narration
        # similarity and net amount are independent signals, and nothing
        # up to this point has ever checked the second one. A
        # same-customer, same-template narration pointing at the wrong
        # one of two orders with different amounts is exactly the case
        # this catches that a text-only shortlist can't. Checked here,
        # not in validation_gate.py, since it needs this file's own
        # settlement data, not the arbiter's own opinion of itself.
        amount_mismatch_note = None
        candidate_amounts = [r["net"] for r in results_by_order_id.get(arb.candidate_id, []) if r.get("net") is not None]
        if candidate_amounts:
            amount_diff = abs(candidate_amounts[0] - float(l["amount"]))
            if amount_diff > PASS4_AMOUNT_SANITY_TOLERANCE_RS:
                arb.auto_applied = False
                amount_mismatch_note = (
                    f"settlement amount Rs.{candidate_amounts[0]:.2f} vs ledger amount Rs.{float(l['amount']):.2f} "
                    f"differ by Rs.{amount_diff:.2f}, beyond a sane tolerance"
                )
                arb.reason += f" [amount check: {amount_mismatch_note}]"

        matched_ledger_rows.add(li)
        for r in results_by_order_id.get(arb.candidate_id, []):
            if r.get("_needs_pass3") and r["category"] is None:
                r["stage"].append(log(
                    "3/4", "arbiter_picked",
                    f"shortlist={shortlist} -> arbiter picked {arb.candidate_id} "
                    f"(auto_applied={arb.auto_applied}) :: {arb.reason}",
                    confidence=arb.confidence,
                ))
                if arb.auto_applied:
                    r["status"] = "MATCHED_AI_ASSISTED"
                    r["reason"] = (
                        f"Narration had no exact order reference, so an AI match was used: "
                        f"'{l['narration']}' was matched to {arb.candidate_id} at {arb.confidence:.0%} "
                        f"confidence from a trusted model tier -- applied automatically, shown here for a spot-check."
                    )
                else:
                    r["status"] = "MATCHED_LOW_CONFIDENCE"
                    r["category"] = r["category"] or "FUZZY_MATCH_NEEDS_REVIEW"
                    held_on_principle = arb.confidence >= CONFIDENCE_AUTO_ACCEPT
                    if amount_mismatch_note:
                        # Distinct from held_on_principle below on purpose:
                        # that branch's own text explicitly says "not
                        # because the match itself looks wrong" -- true
                        # when the only reason is tier trust, false here,
                        # where the amounts genuinely disagree. Reusing
                        # that text for this case would be dishonest about
                        # why the row is actually being held.
                        r["reason"] = (
                            f"AI matched '{l['narration']}' to {arb.candidate_id} at {arb.confidence:.0%} "
                            f"confidence, but {amount_mismatch_note} -- narration similarity alone doesn't "
                            f"confirm this is the right order."
                        )
                    elif held_on_principle:
                        r["reason"] = (
                            f"AI matched '{l['narration']}' to {arb.candidate_id} at {arb.confidence:.0%} "
                            f"confidence, but the model tier that produced it isn't on the trusted "
                            f"auto-apply list -- held for a human to confirm on policy grounds, not because "
                            f"the match itself looks wrong."
                        )
                    else:
                        candidate_note = f"{len(shortlist)} possible orders" if len(shortlist) > 1 else f"order {shortlist[0]}"
                        r["reason"] = (
                            f"AI weighed {candidate_note} against narration '{l['narration']}' and leaned "
                            f"toward {arb.candidate_id}, but only at {arb.confidence:.0%} confidence -- below "
                            f"the {CONFIDENCE_AUTO_ACCEPT:.0%} bar to auto-apply, so a human needs to confirm "
                            f"this is actually the right order."
                        )
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
    # match_key uses the bank row's own UTR: unique per bank credit by
    # construction, so a re-run against the same batch matches this exact
    # orphan back up instead of inserting a lookalike duplicate.
    orphan_bank = [b for i, b in enumerate(bank) if i not in matched_bank_rows]
    for b in orphan_bank:
        results.append({"order_id": None, "settlement_id": None, "net": float(b["credited_amount"]),
                         "match_key": f"bank_orphan:{b['utr']}",
                         "status": "EXCEPTION", "category": "UNEXPLAINED",
                         "reason": f"Bank credit of Rs.{b['credited_amount']} on {b['value_date']} has no matching settlement_id anywhere in the settlement report.",
                         "stage": [log("final", "unexplained", "no settlement counterpart found")]})

    # ---------- Ledger rows never matched at all ----------
    # match_key falls back to invoice_id when order_ref is blank (a messy
    # narration row that still never resolved) -- invoice_id is always
    # present, unlike order_ref.
    for li, l in enumerate(ledger):
        if li not in matched_ledger_rows:
            is_afa = "AFA_MANDATE_HOLD" in l["narration"]
            order_id = l["order_ref"] or None
            results.append({"order_id": order_id, "settlement_id": None, "net": float(l["amount"]),
                             "match_key": f"order_only:{order_id}" if order_id else f"ledger_orphan:{l['invoice_id']}",
                             "status": "EXCEPTION",
                             "category": "AFA_MANDATE_HOLD" if is_afa else "UNEXPLAINED",
                             "reason": ("Charge never settled -- ledger shows a subscription renewal that crossed the RBI e-mandate AFA threshold (>Rs.15,000) and needs a compliant step-up re-authentication, not a blind retry."
                                        if is_afa else
                                        "Ledger entry has no matching settlement or bank record -- possibly an invoice raised for a payment that was never actually captured."),
                             "stage": [log("final", "afa_mandate_hold" if is_afa else "unexplained",
                                           "no settlement or bank counterpart found")]})

    return results


def summarize(results: list[dict]) -> dict:
    """Aggregates reconcile()'s per-row results into the percentages and
    category counts report.py and review_server.py display."""
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

    # MATCHED_LOW_CONFIDENCE is an unconfirmed arbiter candidate sitting in
    # the human review queue -- db.py's own needs_action rule
    # (needs_action = "yes" if status in (EXCEPTION, MATCHED_LOW_CONFIDENCE))
    # already says so. It must NOT be folded into "resolved" here: doing so
    # let the headline resolved percentage quietly count a human's
    # not-yet-made decision as done. Caught by tracing this number against
    # db.py's own definition instead of assuming it was already consistent.
    resolved = matched + matched_variance + exact_reference + learned + ai_assisted

    # Real Rs. amounts, not a forecast: quantifies how much of the cash
    # position a downstream tool like Cashflow Forecaster would see as
    # ambiguous versus how much this run explained or matched versus what's
    # honestly still open or pending a human's confirm. See
    # review_server.py's compute_cash_clarity for the same computation over
    # persisted rows -- kept in sync, same fix applied in both places.
    #
    # DUPLICATE rows are excluded entirely, not counted in any bucket: a
    # duplicate settlement export is a second REPORT of a transaction whose
    # real money already cleared under its sibling row (see
    # test_duplicate_settlement_id_flagged_symmetrically). Counting its net
    # amount as separate "at-risk" cash would double-count money that
    # already landed -- the opposite of honest disclosure.
    cash_at_risk = cash_resolved = cash_pending_review = cash_still_open = 0.0
    for r in results:
        if not r["category"] or r.get("net") is None or r["category"] == "DUPLICATE":
            continue
        amt = float(r["net"])
        cash_at_risk += amt
        if r["status"] == "MATCHED_LOW_CONFIDENCE":
            cash_pending_review += amt
        elif r["status"] == "EXCEPTION":
            cash_still_open += amt
        else:
            cash_resolved += amt

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
        "cash_at_risk": round(cash_at_risk, 2),
        "cash_resolved": round(cash_resolved, 2),
        "cash_pending_review": round(cash_pending_review, 2),
        "cash_still_open": round(cash_still_open, 2),
        "cash_resolved_pct": round(100 * cash_resolved / cash_at_risk, 1) if cash_at_risk else 0.0,
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
