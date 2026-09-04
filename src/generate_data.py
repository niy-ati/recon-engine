"""
Generates three synthetic data sources that mirror what a merchant on
Razorpay reconciles every settlement cycle:

  1. settlement_report.csv  -- Razorpay's dashboard export
       (settlement_id, payment_id, order_id, gross, mdr, gst_on_mdr, net, utr, settlement_date, on_hold,
        method, dispute_id)
  2. bank_statement.csv     -- the merchant's bank statement
       (utr, credited_amount, value_date, narration)
  3. internal_ledger.csv    -- the merchant's Tally/QuickBooks-style export
       (invoice_id, order_ref, customer, amount, narration, tax_line)

Injected failure modes:
  - timing offset between settlement date and bank value date
  - duplicated settlement row
  - partial refund netted into a later settlement
  - GST-on-MDR rounding drift
  - inconsistent narration text across ledger vs. settlement (still
    contains the exact order digits -- resolves in Pass 2.75, no LLM)
  - an OCR/typo-corrupted order reference (digit swapped for a visually
    similar letter) -- breaks exact-digit matching, needs Pass 3/4
  - orphan rows with no counterpart anywhere
  - AFA_MANDATE_HOLD: a subscription charge blocked by the RBI e-mandate
    AFA threshold, never re-attempted
  - ON_HOLD_BY_RAZORPAY: a settlement Razorpay's own API flags
    on_hold=true -- distinct from AFA_MANDATE_HOLD (a regulatory retry
    constraint) and from plain UNEXPLAINED (no information at all)
  - UTR_LEVEL_MISMATCH: the settlement report and the bank statement
    disagree on the UTR for the same real transfer (Razorpay's settlement
    UTR is genuinely two-tier -- batch-level vs. per-line -- see README)
    -- the money arrived, just filed under a different UTR than the
    settlement line claims
  - DISPUTED: the settlement recon line carries a real, non-null
    dispute_id -- money genuinely arrived (the bank credit still matches
    cleanly), but is provisionally at risk of a chargeback clawback, a
    fact reconciliation must surface even though the underlying bank
    posting itself looks clean

Known limitation: the overall resolved percentage varies by seed. Held-out
re-measurement against the current code (2026-08-24), five fresh seeds not
tuned against: 42 -> 90.5%, 7 -> 88.0%, 21 -> 88.5%, 99 -> 87.1%,
555 -> 90.9% -- an 87.1%-90.9% range, every one comfortably clear of the
~51% manual baseline. The seed is pinned to 42 for a reproducible default
run, not because 90.5% specifically is a guaranteed constant. Reproduce
the range yourself with `python extras/seed_sweep.py`, which drives this
via the RECON_SEED environment variable below and restores data/ and
output/ to their committed state afterward, whatever seed it's given.
"""
import csv
import os
import random
from datetime import date, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

random.seed(int(os.environ.get("RECON_SEED", "42")))

N_ORDERS = 500  # well past the track's 50+ floor -- enough volume that every
                # category has multiple real examples on the live review site,
                # not just one of each
MDR_RATE = 0.02
GST_RATE = 0.18
START = date(2026, 8, 1)

settlement_rows = []
bank_rows = []
ledger_rows = []

customers = [f"Customer {i}" for i in range(1, 30)]

# Real field on Razorpay's own settlement recon line (see ingest.py's
# module docstring), not tracked anywhere in this project until now.
# Weights are a plausible Indian checkout mix (UPI dominant), not a cited
# Razorpay statistic -- the point is a real, displayable field per row,
# not a claim about the actual market split.
PAYMENT_METHODS = ["UPI", "Card", "Netbanking", "Wallet"]
METHOD_WEIGHTS = [55, 30, 10, 5]


def money(x: float) -> float:
    return round(x, 2)


def random_hex(n: int) -> str:
    # uuid.uuid4() draws from os.urandom, ignoring random.seed(), so IDs
    # would differ on every run even under a fixed seed -- this generator
    # uses the seeded `random` module instead, so the whole batch
    # (including IDs) is byte-for-byte reproducible under the same seed.
    return "".join(random.choices("0123456789abcdef", k=n))


for i in range(N_ORDERS):
    order_id = f"order_{1000+i}"
    payment_id = f"pay_{random_hex(14)}"
    settlement_id = f"setl_{random_hex(14)}"
    gross = random.choice([499, 999, 1499, 2499, 4999, 9999])
    mdr = money(gross * MDR_RATE)
    gst_on_mdr = money(mdr * GST_RATE)
    net = money(gross - mdr - gst_on_mdr)
    txn_date = START + timedelta(days=random.randint(0, 20))
    settle_date = txn_date + timedelta(days=2)  # T+2
    utr = f"UTR{random.randint(10**9, 10**10-1)}"
    customer = random.choice(customers)
    method = random.choices(PAYMENT_METHODS, weights=METHOD_WEIGHTS)[0]
    dispute_id = ""  # overridden only in the DISPUTED branch below

    case = random.random()

    # --- 65%: clean match ---
    if case < 0.65:
        settlement_rows.append([settlement_id, payment_id, order_id, gross, mdr, gst_on_mdr, net, utr, settle_date, False, method, dispute_id])
        bank_rows.append([utr, net, settle_date, f"NEFT CR RAZORPAY SETTLEMENT {settlement_id}"])
        ledger_rows.append([f"INV-{1000+i}", order_id, customer, gross, f"Payment received order {order_id} - {customer}", gst_on_mdr])

    # --- 8%: bank credit lands a day late ---
    elif case < 0.73:
        bank_date = settle_date + timedelta(days=1)
        settlement_rows.append([settlement_id, payment_id, order_id, gross, mdr, gst_on_mdr, net, utr, settle_date, False, method, dispute_id])
        bank_rows.append([utr, net, bank_date, f"NEFT CR RAZORPAY SETTLEMENT {settlement_id}"])
        ledger_rows.append([f"INV-{1000+i}", order_id, customer, gross, f"Payment received order {order_id} - {customer}", gst_on_mdr])

    # --- 6%: partial refund netted into settlement ---
    elif case < 0.79:
        refund = money(gross * 0.3)
        net_after_refund = money(net - refund)
        settlement_rows.append([settlement_id, payment_id, order_id, gross, mdr, gst_on_mdr, net_after_refund, utr, settle_date, False, method, dispute_id])
        bank_rows.append([utr, net_after_refund, settle_date, f"NEFT CR RAZORPAY SETTLEMENT {settlement_id}"])
        ledger_rows.append([f"INV-{1000+i}", order_id, customer, gross, f"Payment received order {order_id} - {customer} PARTIAL REFUND {refund}", gst_on_mdr])

    # --- 4%: GST-on-MDR rounding drift ---
    elif case < 0.83:
        drift = random.choice([-0.5, 0.5, 1.0])
        settlement_rows.append([settlement_id, payment_id, order_id, gross, mdr, money(gst_on_mdr + drift), money(net - drift), utr, settle_date, False, method, dispute_id])
        bank_rows.append([utr, money(net - drift), settle_date, f"NEFT CR RAZORPAY SETTLEMENT {settlement_id}"])
        ledger_rows.append([f"INV-{1000+i}", order_id, customer, gross, f"Payment received order {order_id} - {customer}", gst_on_mdr])

    # --- 4%: messy ledger narration, digits intact -- resolves in Pass 2.75 ---
    elif case < 0.87:
        settlement_rows.append([settlement_id, payment_id, order_id, gross, mdr, gst_on_mdr, net, utr, settle_date, False, method, dispute_id])
        bank_rows.append([utr, net, settle_date, f"NEFT CR RAZORPAY SETTLEMENT {settlement_id}"])
        messy = f"pymt rcvd {customer.split()[1]} ord#{1000+i} thx"
        ledger_rows.append([f"INV-{1000+i}", "", customer, gross, messy, gst_on_mdr])

    # --- 3%: OCR/typo-corrupted order reference -- needs Pass 3/4 ---
    elif case < 0.90:
        settlement_rows.append([settlement_id, payment_id, order_id, gross, mdr, gst_on_mdr, net, utr, settle_date, False, method, dispute_id])
        bank_rows.append([utr, net, settle_date, f"NEFT CR RAZORPAY SETTLEMENT {settlement_id}"])
        digits = str(1000 + i)
        corrupted = digits.replace("0", "O", 1) if "0" in digits else digits.replace("1", "l", 1)
        typo_narration = f"pymt rcvd {customer.split()[1]} ord#{corrupted} thx"
        ledger_rows.append([f"INV-{1000+i}", "", customer, gross, typo_narration, gst_on_mdr])

    # --- 3%: duplicated settlement row ---
    elif case < 0.93:
        settlement_rows.append([settlement_id, payment_id, order_id, gross, mdr, gst_on_mdr, net, utr, settle_date, False, method, dispute_id])
        settlement_rows.append([settlement_id + "_dup", payment_id, order_id, gross, mdr, gst_on_mdr, net, utr, settle_date, False, method, dispute_id])
        bank_rows.append([utr, net, settle_date, f"NEFT CR RAZORPAY SETTLEMENT {settlement_id}"])
        ledger_rows.append([f"INV-{1000+i}", order_id, customer, gross, f"Payment received order {order_id} - {customer}", gst_on_mdr])

    # --- 1%: genuinely orphan / UNEXPLAINED ---
    elif case < 0.94:
        bank_rows.append([utr, net, settle_date, f"NEFT CR RAZORPAY SETTLEMENT {settlement_id}"])

    # --- 2%: two-tier UTR mismatch -- the bank posts the exact amount on
    # the exact date, but under a different UTR than the settlement
    # report's own reference (a real quirk of Razorpay's two-tier UTR:
    # batch-level `utr` vs. per-line `settlement_utr` can diverge) ---
    elif case < 0.96:
        reported_utr = utr
        actual_bank_utr = f"UTR{random.randint(10**9, 10**10-1)}"
        settlement_rows.append([settlement_id, payment_id, order_id, gross, mdr, gst_on_mdr, net, reported_utr, settle_date, False, method, dispute_id])
        bank_rows.append([actual_bank_utr, net, settle_date, f"NEFT CR RAZORPAY SETTLEMENT {settlement_id}"])
        ledger_rows.append([f"INV-{1000+i}", order_id, customer, gross, f"Payment received order {order_id} - {customer}", gst_on_mdr])

    # --- 2%: on_hold=true settlement -- fulfilled but no bank credit yet ---
    elif case < 0.98:
        settlement_rows.append([settlement_id, payment_id, order_id, gross, mdr, gst_on_mdr, net, utr, settle_date, True, method, dispute_id])
        ledger_rows.append([f"INV-{1000+i}", order_id, customer, gross, f"Payment received order {order_id} - {customer}", gst_on_mdr])

    # --- 1%: settlement carries an active dispute -- see the DISPUTED
    # entry in the module docstring above ---
    elif case < 0.99:
        dispute_id = f"disp_{random_hex(14)}"
        settlement_rows.append([settlement_id, payment_id, order_id, gross, mdr, gst_on_mdr, net, utr, settle_date, False, method, dispute_id])
        bank_rows.append([utr, net, settle_date, f"NEFT CR RAZORPAY SETTLEMENT {settlement_id}"])
        ledger_rows.append([f"INV-{1000+i}", order_id, customer, gross, f"Payment received order {order_id} - {customer}", gst_on_mdr])

    # --- 1%: AFA/mandate-hold (subscription charge blocked at >15k) ---
    else:
        gross = 18500  # over the RBI AFA threshold
        mdr = money(gross * MDR_RATE)
        gst_on_mdr = money(mdr * GST_RATE)
        ledger_rows.append([f"INV-{1000+i}", order_id, customer, gross,
                             f"Subscription renewal order {order_id} - {customer} - AFA_MANDATE_HOLD pending step-up auth",
                             gst_on_mdr])

random.shuffle(settlement_rows)
random.shuffle(bank_rows)
random.shuffle(ledger_rows)

with open(DATA_DIR / "settlement_report.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["settlement_id", "payment_id", "order_id", "gross", "mdr", "gst_on_mdr", "net", "utr", "settlement_date", "on_hold", "method", "dispute_id"])
    w.writerows(settlement_rows)

with open(DATA_DIR / "bank_statement.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["utr", "credited_amount", "value_date", "narration"])
    w.writerows(bank_rows)

with open(DATA_DIR / "internal_ledger.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["invoice_id", "order_ref", "customer", "amount", "narration", "gst_line"])
    w.writerows(ledger_rows)

print(f"Generated {len(settlement_rows)} settlement rows, {len(bank_rows)} bank rows, {len(ledger_rows)} ledger rows.")
