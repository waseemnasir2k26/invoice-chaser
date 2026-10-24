"""Payment -> invoice matching.

A false "paid" is the worst thing this tool can do (the client is never chased), so the
rules are asymmetric. A pair is AUTO-matched (counted as paid) only when

    the invoice id appears in the payment text
 OR the amount is exact AND the payer is the client: every client-name word equals a
    payer word (whole words, not substrings) and the payer has no extra words other than
    legal suffixes (Ltd, LLC, Inc, GmbH, Pvt, Co ...).

Everything else that looks plausible is a SUGGESTION: the invoice becomes "needs review",
nothing is counted as paid, and no reminder is drafted. Confidence (0..1) is for ordering
and display:
    invoice id in payment text   +0.60
    amount equal (in tolerance)  +0.45   (lower: +0.10, higher: +0.05)
    client name similarity       +0.30 * similarity (>= 0.6)
"""
from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
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
NAME_LOOKBACK_DAYS = 60  # name-only candidates are searched from this long before the invoice date

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


@lru_cache(maxsize=None)
def _name_words(s: str) -> frozenset:
    return frozenset(norm_name(s).split())


def _prefixes(normed: str) -> frozenset:
    return frozenset(w[:4] for w in normed.split())


def strong_name(client: str, payer: str) -> bool:
    """The payer IS the client: same words once legal suffixes are ignored. Whole words only,
    so "Acme" != "Acme Holdings", "Tom Lee" != "Tom Leeds", "Alpha Studio" != "Alpha Labs"."""
    c = _name_words(client)
    return bool(c) and c == _name_words(payer)


@lru_cache(maxsize=262144)
def _text_sim(c: str, t: str) -> float:
    """Similarity of normalised client name c to normalised text t (whole string and word windows)."""
    if f" {c} " in f" {t} ":
        return 1.0
    cw, tw = c.split(), t.split()
    sm = SequenceMatcher(None)
    sm.set_seq2(c)
    sm.set_seq1(t)
    # Only values >= 0.6 matter downstream, so a cheap upper bound below that is returned as is.
    best = sm.quick_ratio()
    if best >= 0.6:
        best = sm.ratio()
    if len(tw) > len(cw) + 1:  # long text (a description): also compare word windows of the client's length
        for size in {len(cw), max(1, len(cw) - 1), len(cw) + 1}:
            for i in range(0, max(1, len(tw) - size + 1)):
                sm.set_seq1(" ".join(tw[i:i + size]))
                if sm.quick_ratio() > max(best, 0.59):
                    best = max(best, sm.ratio())
    return best


def name_similarity(client: str, *texts: str) -> float:
    """Fuzzy 0..1 similarity, used for ordering and display only. Never decides "paid" by itself."""
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
    auto: bool = False
    blocker: str = ""
    via_ref: bool = False
    date_problem: str = ""  # set when the payment is outside the invoice's date window


@dataclass
class Early:
    """A plausible payment that was NOT matched because of its date (often a day/month mix-up)."""
    payment: Payment
    invoice: Invoice
    confidence: float
    problem: str


@dataclass
class _Info:
    """Per-payment text features, computed once."""
    text: str
    tokens: frozenset
    prefixes: frozenset


@dataclass
class _Inv:
    """Per-invoice features, computed once."""
    inv: Invoice
    tol: Decimal
    itoks: list
    cpre: frozenset


def _info(p: Payment) -> _Info:
    text = f"{p.description} {p.payer}"
    return _Info(text, frozenset(_tokens(text)),
                 _prefixes(norm_name(p.payer)) | _prefixes(norm_name(p.description)))


def _rate(pay_cur: str, inv_cur: str, rates: dict[str, str]) -> Decimal | None:
    if pay_cur == inv_cur:
        return Decimal(1)
    r = rates.get(f"{pay_cur}/{inv_cur}")
    return Decimal(str(r)) if r else None


def _score(ctx: _Inv, pay: Payment, info: _Info, cfg: Config, rate: Decimal) -> Scored:
    inv = ctx.inv
    problem = ""
    if pay.date < inv.issued:
        problem = f"dated {(inv.issued - pay.date).days} day(s) before the invoice was issued (check the date format)"
    elif (pay.date - inv.issued).days > cfg.date_window_days:
        problem = f"dated {(pay.date - inv.issued).days} days after the invoice (window is {cfg.date_window_days})"
    conv = pay.amount * rate
    ref = bool(ctx.itoks) and all(t in info.tokens for t in ctx.itoks) and ref_hit(inv.id, info.text)
    exact = conv == inv.amount
    conf, why = 0.0, []
    if ref:
        conf += 0.60
        why.append(f"invoice id {inv.id} in payment text")
    # fuzzy name matching only when the names share a word prefix: keeps this fast on big exports
    strong = strong_name(inv.client, pay.payer)
    sim = 1.0 if strong else 0.0
    if not strong and ctx.cpre & info.prefixes:
        sim = name_similarity(inv.client, pay.payer, pay.description)
    if exact:
        conf += 0.45
        why.append("exact amount")
    elif abs(conv - inv.amount) <= ctx.tol:
        if ref or sim >= 0.6:  # a fee/FX shortfall is only a signal when something else points at this invoice
            conf += 0.45
            why.append(f"amount within {cfg.fx_tolerance_pct:g}% tolerance ({conv:.2f} vs {inv.amount:.2f})")
    elif conv < inv.amount:
        conf += 0.10
        why.append(f"partial amount ({conv:.2f} of {inv.amount:.2f})")
    else:
        conf += 0.05
        why.append(f"amount higher than invoice ({conv:.2f} vs {inv.amount:.2f})")
    if sim >= 0.6:
        conf += 0.30 * sim
        why.append(f"client name similarity {sim:.2f}")
    if rate != 1:
        why.append(f"converted at {rate}")
    conf = round(min(conf, 1.0), 2)
    ok = ref or (exact and strong)
    auto = conf >= cfg.auto_threshold and ok and not pay.suspect and not problem
    blocker = ""
    if not auto:
        parts = []
        if problem:
            parts.append(f"payment {problem}")
        if pay.suspect:
            parts.append(pay.suspect)
        if not ref:
            parts.append("no invoice id in the payment text")
            if not strong:
                parts.append(f"payer {pay.payer!r} is not exactly {inv.client!r}" if pay.payer
                             else "the payment has no payer name")
            elif not exact:
                parts.append("amount is not exact")
        if conf < cfg.auto_threshold and not parts:
            parts.append(f"confidence {conf} is below {cfg.auto_threshold}")
        blocker = "; ".join(parts)
        if conf >= cfg.auto_threshold:  # would have passed on score alone: show it as strictly below auto
            conf = min(conf, max(round(cfg.auto_threshold - 0.01, 2), cfg.review_threshold))
    return Scored(conf, "; ".join(why), rate, auto, blocker, ref, problem)


def score_pair(inv: Invoice, pay: Payment, cfg: Config) -> Scored | None:
    """Score one pair in isolation (match_all uses the same scoring on indexed candidates)."""
    rate = _rate(pay.currency, inv.currency, cfg.fx_rates)
    if rate is None:
        return None
    ctx = _Inv(inv, inv.amount * Decimal(str(cfg.fx_tolerance_pct)) / 100, _tokens(inv.id), _prefixes(norm_name(inv.client)))
    s = _score(ctx, pay, _info(pay), cfg, rate)
    return None if s.date_problem else s


def match_all(invoices, payments, today, cfg):
    results, leftovers, _early = match_all_ex(invoices, payments, today, cfg)
    return results, leftovers


def match_all_ex(invoices: list[Invoice], payments: list[Payment], today: date, cfg: Config):
    """Returns (results in invoice order, leftover [(Payment, remaining)], early [Early]).

    Candidate pairs come from three indexes instead of an all-pairs scan: payments with the
    exact invoice amount, payments that contain the invoice id's rarest token, and payments
    whose payer IS the client (inside the invoice's date window). Work per invoice is bounded
    by those, so run time grows roughly linearly with the number of rows. Consequence: a
    payment that differs in amount, carries no invoice id and has a fuzzy-only payer name is
    not even considered; that is a deliberate trade for speed and for fewer false matches."""
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
    payer_raw: dict[frozenset, list[tuple[int, int]]] = {}
    for pi, inf in enumerate(infos):
        for t in inf.tokens:
            tok_idx.setdefault(t, []).append(pi)
        words = _name_words(payments[pi].payer)
        if words:
            payer_raw.setdefault(words, []).append((payments[pi].date.toordinal(), pi))
    payer_idx = {}
    for key, lst in payer_raw.items():
        lst.sort()
        payer_idx[key] = ([o for o, _ in lst], [pi for _, pi in lst])

    eps = Decimal("0.0000001")
    pairs = []
    early: list[Early] = []
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
            # exact amounts only: a +-tolerance band grows with the export and matches unrelated payments.
            # Fee/FX shortfalls are found through the invoice id or the client name instead.
            lo = bisect_left(amts, inv.amount / r - eps)
            hi = bisect_right(amts, inv.amount / r + eps)
            cand.update(idxs[lo:hi])
        ctx = _Inv(inv, tol, _tokens(inv.id), _prefixes(norm_name(inv.client)))
        if ctx.itoks:
            cand.update(min((tok_idx.get(t, ()) for t in ctx.itoks), key=len))
        key = _name_words(inv.client)
        if key in payer_idx:  # payments from exactly this client, inside the invoice's date window
            ords, idxs = payer_idx[key]
            o = inv.issued.toordinal()
            cand.update(idxs[bisect_left(ords, o - NAME_LOOKBACK_DAYS):bisect_right(ords, o + cfg.date_window_days)])
        for pi in sorted(cand):
            p = payments[pi]
            rate = rates.get(p.currency)
            if rate is None:
                continue
            sc = _score(ctx, p, infos[pi], cfg, rate)
            if sc.date_problem:
                if sc.confidence >= cfg.review_threshold:
                    early.append(Early(p, inv, sc.confidence, sc.date_problem))
                    pairs.append((sc, inv, pi))
                continue
            if sc.confidence >= cfg.review_threshold:
                pairs.append((sc, inv, pi))

    # An auto match that rests on name + amount alone (no invoice id) and fits more than one invoice
    # is ambiguous: do not guess which invoice it settles.
    by_payment: dict[int, list] = defaultdict(list)
    for item in pairs:
        if item[0].auto and not item[0].via_ref:
            by_payment[item[2]].append(item)
    for pi, items in by_payment.items():
        if len(items) > 1:
            ids = ", ".join(i.id for _, i, _ in items)
            for s, inv, _ in items:
                s.auto = False
                s.blocker = f"payment fits several invoices ({ids}) and carries no invoice id to tell them apart"
                s.confidence = min(s.confidence, max(round(cfg.auto_threshold - 0.01, 2), cfg.review_threshold))
    pairs.sort(key=lambda t: (-t[0].confidence, t[1].due, t[1].id, payments[t[2]].date))

    paid = {i.id: Decimal(0) for i in invoices}
    confirmed: dict[str, list[Match]] = {i.id: [] for i in invoices}
    suggested: dict[str, list[Match]] = {i.id: [] for i in invoices}
    rem = {pi: p.amount for pi, p in enumerate(payments)}

    for s, inv, pi in pairs:
        if not s.auto:
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
        if s.auto or rem[pi] <= 0:
            continue
        if paid[inv.id] >= inv.amount * (1 - tolpct) or len(suggested[inv.id]) >= 2:
            continue
        p = payments[pi]
        suggested[inv.id].append(Match(inv.id, p.id, p.date, p.description, s.confidence, s.reason,
                                       blocker=s.blocker or f"confidence {s.confidence} is below {cfg.auto_threshold}"))

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
        why = ""
        if got >= inv.amount - tol:
            st, out, stage = "paid", Decimal(0), None
        elif got > 0:
            st, out = "partial", inv.amount - got
            stage = stage_for(overdue, cfg.stage_days)
            why = f"{got:.2f} of {inv.amount:.2f} matched; {out:.2f} still unmatched"
        elif sg:
            st, out, stage = "needs_review", inv.amount, None
            why = "; ".join(f"{m.payment_id}: {m.blocker}" for m in sg)
        elif overdue > 0:
            st, out, stage = "overdue", inv.amount, stage_for(overdue, cfg.stage_days)
            why = "no payment in the exports carries this invoice id, amount or client name"
        else:
            st, out, stage = "open", inv.amount, None
            why = f"not due until {inv.due.isoformat()}"
        if ms:
            conf = min(m.confidence for m in ms)
        elif sg:
            conf = max(m.confidence for m in sg)
        else:
            conf = 0.0
        note = ""
        if st == "needs_review":
            note = "possible payment found but it is not certain: no reminder drafted until you confirm"
        elif st == "partial" and sg:
            note = "further possible payment needs review"
        if st == "paid":
            overdue = 0
        r = InvoiceResult(inv, st, got.quantize(Decimal("0.01")), out.quantize(Decimal("0.01")),
                          overdue, stage, conf, ms, sg, note)
        r.why_not_paid = why
        results.append(r)
    leftovers = [(p, rem[pi]) for pi, p in enumerate(payments) if rem[pi] > Decimal("0.005")]
    return results, leftovers, early
