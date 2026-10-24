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
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher
from functools import lru_cache

from .config import Config
from .ladder import days_overdue, stage_for
from .models import Invoice, InvoiceResult, Match, Payment

SETTLED = {"paid", "settled"}
VOID = {"void", "voided", "cancelled", "canceled", "written off", "writtenoff"}
_SUFFIXES = {"ltd", "limited", "llc", "llp", "inc", "incorporated", "co", "corp", "corporation", "gmbh", "pvt", "pty", "sa", "bv", "the"}


_TOKEN = re.compile(r"[a-z]+|\d+")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


@lru_cache(maxsize=None)
def _ref_pattern(invoice_id: str):
    tokens = re.findall(r"[A-Za-z]+|\d+", invoice_id)
    if not tokens:
        return None
    pat = r"(?<![A-Za-z0-9])" + r"[\s\-_#./]*".join(re.escape(t) for t in tokens) + r"(?![A-Za-z0-9])"
    return re.compile(pat, re.IGNORECASE)


def ref_hit(invoice_id: str, text: str) -> bool:
    """True if the invoice id appears in text, tolerant of separators (INV-12, INV 12, inv#12)
    but not of longer ids (INV-120 must not match INV-12)."""
    pat = _ref_pattern(invoice_id)
    return bool(pat and text and pat.search(text))


@lru_cache(maxsize=None)
def norm_name(s: str) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(w for w in s.split() if w not in _SUFFIXES)


def _prefixes(normed: str) -> frozenset:
    return frozenset(w[:4] for w in normed.split())


@lru_cache(maxsize=262144)
def _text_sim(c: str, t: str) -> float:
    """Similarity of normalised client name c to normalised text t (whole string and word windows)."""
    if f" {c} " in f" {t} ":
        return 1.0
    cw, tw = c.split(), t.split()
    sm = SequenceMatcher(None)
    sm.set_seq2(c)
    sm.set_seq1(t)
    best = sm.ratio()
    for size in {len(cw), max(1, len(cw) - 1), len(cw) + 1}:
        for i in range(0, max(1, len(tw) - size + 1)):
            sm.set_seq1(" ".join(tw[i:i + size]))
            if sm.real_quick_ratio() > best and sm.quick_ratio() > best:
                best = max(best, sm.ratio())
    return best


def name_similarity(client: str, *texts: str) -> float:
    c = norm_name(client)
    if not c:
        return 0.0
    best = 0.0
    for text in texts:
        t = norm_name(text)
        if t:
            best = max(best, _text_sim(c, t))
            if best == 1.0:
                break
    return best


@dataclass
class Scored:
    confidence: float
    reason: str
    rate: Decimal


@dataclass
class _Info:
    """Per-payment text features, computed once."""
    text: str
    tokens: frozenset
    prefixes: frozenset


def _info(p: Payment) -> _Info:
    text = f"{p.description} {p.payer}"
    return _Info(text, frozenset(_tokens(text)),
                 _prefixes(norm_name(p.payer)) | _prefixes(norm_name(p.description)))


def _rate(pay_cur: str, inv_cur: str, rates: dict[str, str]) -> Decimal | None:
    if pay_cur == inv_cur:
        return Decimal(1)
    r = rates.get(f"{pay_cur}/{inv_cur}")
    return Decimal(str(r)) if r else None


def _score(inv: Invoice, pay: Payment, info: _Info, cfg: Config, rate: Decimal, tol: Decimal) -> Scored | None:
    if pay.date < inv.issued or (pay.date - inv.issued).days > cfg.date_window_days:
        return None
    conv = pay.amount * rate
    conf, why = 0.0, []
    itoks = _tokens(inv.id)
    if itoks and all(t in info.tokens for t in itoks) and ref_hit(inv.id, info.text):
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
    # fuzzy name matching only when the names share a word prefix: keeps this fast on big exports
    if _prefixes(norm_name(inv.client)) & info.prefixes:
        sim = name_similarity(inv.client, pay.payer, pay.description)
        if sim >= 0.6:
            conf += 0.30 * sim
            why.append(f"client name similarity {sim:.2f}")
    if rate != 1:
        why.append(f"converted at {rate}")
    return Scored(round(min(conf, 1.0), 2), "; ".join(why), rate)


def score_pair(inv: Invoice, pay: Payment, cfg: Config) -> Scored | None:
    """Score one pair in isolation (match_all uses the same scoring on indexed candidates)."""
    rate = _rate(pay.currency, inv.currency, cfg.fx_rates)
    if rate is None:
        return None
    tol = inv.amount * Decimal(str(cfg.fx_tolerance_pct)) / 100
    return _score(inv, pay, _info(pay), cfg, rate, tol)


def match_all(invoices: list[Invoice], payments: list[Payment], today: date, cfg: Config):
    """Returns (results in invoice order, leftover list of (Payment, remaining Decimal)).

    Candidate pairs come from three indexes instead of an all-pairs scan: payments whose
    amount falls inside the tolerance band, payments that contain the invoice id's rarest
    token, and payments whose names share a word prefix with the client. Each candidate is
    then scored exactly as before."""
    tolpct = Decimal(str(cfg.fx_tolerance_pct)) / 100
    active = [i for i in invoices if i.status not in SETTLED and i.status not in VOID]

    infos = [_info(p) for p in payments]
    by_cur: dict[str, list[int]] = {}
    for pi, p in enumerate(payments):
        by_cur.setdefault(p.currency, []).append(pi)
    amt_idx = {}
    for cur, idxs in by_cur.items():
        idxs.sort(key=lambda i: payments[i].amount)
        amt_idx[cur] = ([payments[i].amount for i in idxs], idxs)
    tok_idx: dict[str, list[int]] = {}
    pre_idx: dict[str, list[int]] = {}
    for pi, inf in enumerate(infos):
        for t in inf.tokens:
            tok_idx.setdefault(t, []).append(pi)
        for x in inf.prefixes:
            pre_idx.setdefault(x, []).append(pi)

    eps = Decimal("0.0000001")
    pairs = []
    for inv in active:
        rates = {}
        for cur in by_cur:
            r = _rate(cur, inv.currency, cfg.fx_rates)
            if r is not None:
                rates[cur] = r
        if not rates:
            continue
        tol = inv.amount * tolpct
        cand: set[int] = set()
        for cur, r in rates.items():
            amts, idxs = amt_idx[cur]
            lo = bisect_left(amts, (inv.amount - tol) / r - eps)
            hi = bisect_right(amts, (inv.amount + tol) / r + eps)
            cand.update(idxs[lo:hi])
        itoks = _tokens(inv.id)
        if itoks:
            cand.update(min((tok_idx.get(t, ()) for t in itoks), key=len))
        cpre = _prefixes(norm_name(inv.client))
        if cpre:
            cand.update(min((pre_idx.get(x, ()) for x in cpre), key=len))
        for pi in sorted(cand):
            p = payments[pi]
            rate = rates.get(p.currency)
            if rate is None:
                continue
            sc = _score(inv, p, infos[pi], cfg, rate, tol)
            if sc and sc.confidence >= cfg.review_threshold:
                pairs.append((sc, inv, pi))
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
