"""Payment -> invoice matching.

Each (invoice, payment) pair gets a confidence in 0..1 built from:
  invoice id found in the payment text   +0.60
  amount equal (within tolerance)        +0.45   (less: +0.10, more: +0.05)
  client name similarity (>= 0.6)        +0.30 * similarity
Pairs >= auto_threshold count as matched. Pairs between review_threshold and
auto_threshold are only SUGGESTIONS: the invoice is marked "needs review" and
nothing is marked paid on their strength.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher

from .config import Config
from .ladder import days_overdue, stage_for
from .models import Invoice, InvoiceResult, Match, Payment

SETTLED = {"paid", "settled"}
VOID = {"void", "voided", "cancelled", "canceled", "written off", "writtenoff"}
_SUFFIXES = {"ltd", "limited", "llc", "llp", "inc", "incorporated", "co", "corp", "corporation", "gmbh", "pvt", "pty", "sa", "bv", "the"}


def ref_hit(invoice_id: str, text: str) -> bool:
    """True if the invoice id appears in text, tolerant of separators (INV-12, INV 12, inv#12)
    but not of longer ids (INV-120 must not match INV-12)."""
    tokens = re.findall(r"[A-Za-z]+|\d+", invoice_id)
    if not tokens or not text:
        return False
    pat = r"(?<![A-Za-z0-9])" + r"[\s\-_#./]*".join(re.escape(t) for t in tokens) + r"(?![A-Za-z0-9])"
    return re.search(pat, text, re.IGNORECASE) is not None


def norm_name(s: str) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(w for w in s.split() if w not in _SUFFIXES)


def name_similarity(client: str, *texts: str) -> float:
    c = norm_name(client)
    if not c:
        return 0.0
    cw = c.split()
    best = 0.0
    for text in texts:
        t = norm_name(text)
        if not t:
            continue
        if f" {c} " in f" {t} ":
            return 1.0
        best = max(best, SequenceMatcher(None, c, t).ratio())
        tw = t.split()
        for size in {len(cw), max(1, len(cw) - 1), len(cw) + 1}:
            for i in range(0, max(1, len(tw) - size + 1)):
                window = " ".join(tw[i:i + size])
                best = max(best, SequenceMatcher(None, c, window).ratio())
    return best


@dataclass
class Scored:
    confidence: float
    reason: str
    rate: Decimal


def _rate(pay_cur: str, inv_cur: str, rates: dict[str, str]) -> Decimal | None:
    if pay_cur == inv_cur:
        return Decimal(1)
    r = rates.get(f"{pay_cur}/{inv_cur}")
    return Decimal(str(r)) if r else None


def score_pair(inv: Invoice, pay: Payment, cfg: Config) -> Scored | None:
    rate = _rate(pay.currency, inv.currency, cfg.fx_rates)
    if rate is None:
        return None
    if pay.date < inv.issued or (pay.date - inv.issued).days > cfg.date_window_days:
        return None
    tol = inv.amount * Decimal(str(cfg.fx_tolerance_pct)) / 100
    conv = pay.amount * rate
    text = f"{pay.description} {pay.payer}"
    conf, why = 0.0, []
    if ref_hit(inv.id, text):
        conf += 0.60
        why.append(f"invoice id {inv.id} in payment text")
    if abs(conv - inv.amount) <= tol:
        conf += 0.45
        if conv == inv.amount:
            why.append("exact amount")
        else:
            why.append(f"amount within {cfg.fx_tolerance_pct:g}% tolerance ({conv:.2f} vs {inv.amount:.2f})")
    elif conv < inv.amount:
        conf += 0.10
        why.append(f"partial amount ({conv:.2f} of {inv.amount:.2f})")
    else:
        conf += 0.05
        why.append(f"amount higher than invoice ({conv:.2f} vs {inv.amount:.2f})")
    sim = name_similarity(inv.client, pay.payer, pay.description)
    if sim >= 0.6:
        conf += 0.30 * sim
        why.append(f"client name similarity {sim:.2f}")
    if rate != 1:
        why.append(f"converted at {rate}")
    return Scored(round(min(conf, 1.0), 2), "; ".join(why), rate)


def match_all(invoices: list[Invoice], payments: list[Payment], today: date, cfg: Config):
    """Returns (results in invoice order, leftover list of (Payment, remaining Decimal))."""
    tolpct = Decimal(str(cfg.fx_tolerance_pct)) / 100
    active = [i for i in invoices if i.status not in SETTLED and i.status not in VOID]
    pairs = []
    for inv in active:
        for pi, p in enumerate(payments):
            s = score_pair(inv, p, cfg)
            if s and s.confidence >= cfg.review_threshold:
                pairs.append((s, inv, pi))
    pairs.sort(key=lambda t: (-t[0].confidence, t[1].due, t[1].id, payments[t[2]].date))

    paid = {i.id: Decimal(0) for i in invoices}
    confirmed: dict[str, list[Match]] = {i.id: [] for i in invoices}
    suggested: dict[str, list[Match]] = {i.id: [] for i in invoices}
    rem = {pi: p.amount for pi, p in enumerate(payments)}

    for s, inv, pi in pairs:
        if s.confidence < cfg.auto_threshold:
            continue
        inv_rem = inv.amount - paid[inv.id]
        if inv_rem <= inv.amount * tolpct or rem[pi] <= 0:
            continue
        alloc = min(rem[pi] * s.rate, inv_rem)
        paid[inv.id] += alloc
        rem[pi] -= alloc / s.rate
        p = payments[pi]
        confirmed[inv.id].append(Match(inv.id, p.id, p.date, p.description, s.confidence, s.reason, alloc))

    for s, inv, pi in pairs:
        if s.confidence >= cfg.auto_threshold or rem[pi] <= 0:
            continue
        if paid[inv.id] >= inv.amount * (1 - tolpct) or len(suggested[inv.id]) >= 2:
            continue
        p = payments[pi]
        suggested[inv.id].append(Match(inv.id, p.id, p.date, p.description, s.confidence, s.reason))

    results = []
    for inv in invoices:
        tol = inv.amount * tolpct
        overdue = days_overdue(inv.due, today)
        if inv.status in VOID:
            results.append(InvoiceResult(inv, "void", Decimal("0.00"), Decimal("0.00"), 0, None, 1.0, note="marked void in invoices.csv"))
            continue
        if inv.status in SETTLED:
            results.append(InvoiceResult(inv, "paid", inv.amount, Decimal("0.00"), 0, None, 1.0, note="marked paid in invoices.csv"))
            continue
        got, ms, sg = paid[inv.id], confirmed[inv.id], suggested[inv.id]
        if got >= inv.amount - tol:
            st, out, stage = "paid", Decimal(0), None
        elif got > 0:
            st, out = "partial", inv.amount - got
            stage = stage_for(overdue, cfg.stage_days)
        elif sg:
            st, out, stage = "needs_review", inv.amount, None
        elif overdue > 0:
            st, out, stage = "overdue", inv.amount, stage_for(overdue, cfg.stage_days)
        else:
            st, out, stage = "open", inv.amount, None
        if ms:
            conf = min(m.confidence for m in ms)
        elif sg:
            conf = max(m.confidence for m in sg)
        else:
            conf = 0.0
        note = ""
        if st == "needs_review":
            note = "possible payment found but confidence is low: no reminder drafted until you confirm"
        elif st == "partial" and sg:
            note = "further possible payment needs review"
        if st == "paid":
            overdue = 0
        results.append(InvoiceResult(inv, st, got.quantize(Decimal("0.01")), out.quantize(Decimal("0.01")),
                                     overdue, stage, conf, ms, sg, note))
    leftovers = [(p, rem[pi]) for pi, p in enumerate(payments) if rem[pi] > Decimal("0.005")]
    return results, leftovers
