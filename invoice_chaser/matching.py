"""Payment -> invoice matching: finds SUGGESTIONS. It never produces "paid".

Evidence is ranked by the table in decision.py (invoice id as a strict whole token, or exact
amount from the client itself). This module finds candidate pairs without ever building the
all-pairs product, applies that table, and allocates payments to invoices. The result statuses are
suggested_paid / partial_suggested / needs_review / overdue / open (plus paid and void when the
invoice list itself says so). Only `invoice-chaser confirm` turns a suggestion into paid.

Confidence (0..1) is only for ordering and display:
    invoice id in payment text   +0.60
    amount equal (in tolerance)  +0.45   (lower: +0.10, higher: +0.05)
    client name similarity       +0.30 * similarity (>= 0.6)

Candidate pairs per invoice are bounded: payments that name the invoice id, payments from a
payer who IS the client inside the date window (the K nearest by date), and payments with the
exact invoice amount, but only when there are at most K of those. If more than K payments share
the amount and nothing else identifies them, the invoice goes to "needs review" in bulk: it is
never paired with all of them.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher
from functools import lru_cache

from .config import Config
from .decision import IdIndex, company_key, field_texts, has_negative_context, same_company
from .ladder import days_overdue, stage_for
from .models import Invoice, InvoiceResult, Match, Payment

import re

SETTLED = {"paid", "settled"}
VOID = {"void", "voided", "cancelled", "canceled", "written off", "writtenoff"}
_SUFFIXES = {"ltd", "limited", "llc", "llp", "inc", "incorporated", "co", "corp", "corporation", "gmbh", "pvt", "pty", "sa", "bv", "the"}
NAME_LOOKBACK_DAYS = 60  # name-path candidates are searched from this long before the invoice date
MAX_CANDIDATES = 20  # K: candidates per invoice from the amount / payer / id indexes
MAX_NAMED = 3  # a payment naming more known invoice ids than this is never paired: it goes to review
# Rules the tests switch off one at a time to prove each test depends on its rule.
FUTURE_CHECK = True
COUNT_SETTLED_PEERS = True
CHECK_NAME_IDENTITY = True


def ref_hit(invoice_id: str, text: str, prefixes=()) -> bool:
    """True if the invoice id appears in text as a complete token (see decision.IdIndex)."""
    return invoice_id in IdIndex([invoice_id], prefixes).find(text)


@lru_cache(maxsize=None)
def norm_name(s: str) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(w for w in s.split() if w not in _SUFFIXES)


def strong_name(client: str, payer: str) -> bool:
    """The payer IS the client (see decision.same_company)."""
    return same_company(client, payer)


@lru_cache(maxsize=262144)
def _text_sim(c: str, t: str) -> float:
    """Similarity of normalised client name c to normalised text t (whole string and word windows)."""
    if f" {c} " in f" {t} ":
        return 1.0
    cw, tw = c.split(), t.split()
    sm = SequenceMatcher(None)
    sm.set_seq2(c)
    sm.set_seq1(t)
    best = sm.quick_ratio()  # only values >= 0.6 matter downstream
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
    """Fuzzy 0..1 similarity, for ordering and display only. It never decides "paid"."""
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
    """Per-payment features, computed once."""
    text: str
    ids: frozenset  # known invoice ids named in the payment (strict whole tokens, fields searched separately)
    foreign: frozenset  # id-like tokens of the same family that are NOT in the invoice list
    neg: bool  # refund / reversal / failure wording somewhere in the payment (before the client's own name is removed)
    texts: tuple = ()  # the fields that were searched, to re-check the wording per invoice


@dataclass
class _Inv:
    inv: Invoice
    tol: Decimal
    today: date
    peers: tuple = ()  # other invoices of the same client with the same price (settled ones included)
    ambiguous_name: bool = False  # two different clients share this name (different e-mail addresses)


def _info_for(p: Payment, index: IdIndex) -> _Info:
    ids: set[str] = set()
    foreign: set[str] = set()
    neg = False
    texts = tuple(field_texts(p.description, p.payer))
    for text in texts:
        i, f = index.analyse(text, MAX_NAMED)
        ids |= i
        foreign |= f
        neg = neg or has_negative_context(text)
    return _Info(f"{p.description} | {p.payer}", frozenset(ids), frozenset(foreign), neg, texts if neg else ())


def _rate(pay_cur: str, inv_cur: str, rates: dict[str, str]) -> Decimal | None:
    if pay_cur == inv_cur:
        return Decimal(1)
    r = rates.get(f"{pay_cur}/{inv_cur}")
    return Decimal(str(r)) if r else None


def _score(ctx: _Inv, pay: Payment, info: _Info, cfg: Config, rate: Decimal) -> Scored:
    inv = ctx.inv
    problem = ""
    if FUTURE_CHECK and pay.date > ctx.today:
        problem = f"dated in the future ({pay.date.isoformat()} is after the run date {ctx.today.isoformat()})"
    elif pay.date < inv.issued:
        problem = f"dated {(inv.issued - pay.date).days} day(s) before the invoice was issued (check the date format)"
    elif (pay.date - inv.issued).days > cfg.date_window_days:
        problem = f"dated {(pay.date - inv.issued).days} days after the invoice (window is {cfg.date_window_days})"
    conv = pay.amount * rate
    ids = info.ids
    ref = inv.id in ids
    unique = ref and len(ids) == 1
    exact = conv == inv.amount
    within = abs(conv - inv.amount) <= ctx.tol
    less = conv < inv.amount and not within
    more = conv > inv.amount and not within
    company = same_company(inv.client, pay.payer)
    neg = info.neg
    # No cleverness: the client's name is never cut out of the text. A client whose own name contains a refund or failure
    # word (Refund Ltd, Pending Ventures) is a human check every time.
    client_neg = neg and has_negative_context(inv.client)

    # ---- the evidence table (decision.py): STRONG evidence becomes a suggestion, never "paid" ----
    path_a = unique and not info.foreign and not neg and pay.amount > 0 and (within or less)
    path_b = ((not ids) and not info.foreign and exact and rate == 1 and company and not neg
              and pay.amount > 0 and not ctx.peers and not ctx.ambiguous_name)
    auto = (path_a or path_b) and not pay.suspect and not problem

    # ---- confidence: ordering and display only ----
    conf, why = 0.0, []
    if ref:
        conf += 0.60
        why.append(f"invoice id {inv.id} in payment text")
    sim = 1.0 if company else 0.0
    if not company:
        sim = name_similarity(inv.client, pay.payer, pay.description) if pay.payer or pay.description else 0.0
    if exact:
        conf += 0.45
        why.append("exact amount")
    elif within:
        if ref or sim >= 0.6:
            conf += 0.45
            why.append(f"amount within {cfg.fx_tolerance_pct:g}% tolerance ({conv:.2f} vs {inv.amount:.2f})")
    elif less:
        conf += 0.10
        why.append(f"partial amount ({conv:.2f} of {inv.amount:.2f})")
    else:
        conf += 0.05
        why.append(f"amount higher than invoice ({conv:.2f} vs {inv.amount:.2f})")
    if sim >= 0.6:
        conf += 0.30 * sim
        why.append("payer is the client" if company else f"client name similarity {sim:.2f}")
    if rate != 1:
        why.append(f"converted at {rate}")
    conf = round(min(conf, 1.0), 2)

    blocker = ""
    if auto:
        conf = max(conf, cfg.auto_threshold)
    else:
        parts = []
        if problem:
            parts.append(f"payment {problem}")
        if pay.suspect:
            parts.append(pay.suspect)
        if client_neg:
            parts.append("client name contains a refund word: please check")
        elif neg:
            parts.append("the payment mentions a refund, reversal, failure or non-receipt")
        if info.foreign:
            parts.append("the payment names an invoice id that is not in your invoice list (" + ", ".join(sorted(info.foreign)) + ")")
        if ref and not unique:
            parts.append("the payment names several invoices (" + ", ".join(sorted(ids)) + ")")
        elif ref and more:
            parts.append(f"the amount is higher than the invoice ({conv:.2f} vs {inv.amount:.2f})")
        elif not ref and ids:
            parts.append("the payment names a different invoice (" + ", ".join(sorted(ids)) + ")")
        elif not ref and not info.foreign:
            parts.append("no invoice id in the payment")
            if not company:
                parts.append(f"payer {pay.payer!r} is not exactly {inv.client!r} (same words and the same Ltd/Inc/LLC kind)"
                             if pay.payer else "the payment has no payer name")
            elif not exact:
                parts.append("the amount is not exact")
            elif rate != 1:
                parts.append("the payment is in another currency")
            elif ctx.peers:
                parts.append("another invoice of this client has the same amount ("
                             + ", ".join(sorted(o.id for o in ctx.peers)) + "), settled or not: cannot tell which one this pays")
            elif ctx.ambiguous_name:
                parts.append(f"two different clients are named {inv.client!r} (different e-mail addresses)")
        if not parts:
            parts.append(f"confidence {conf} is below {cfg.auto_threshold}")
        blocker = "; ".join(parts)
        conf = min(conf, max(round(cfg.auto_threshold - 0.01, 2), cfg.review_threshold))
    return Scored(conf, "; ".join(why), rate, auto, blocker, ref, problem)


def score_pair(inv: Invoice, pay: Payment, cfg: Config, today: date | None = None) -> Scored | None:
    """Score one pair in isolation (match_all uses the same scoring on indexed candidates)."""
    rate = _rate(pay.currency, inv.currency, cfg.fx_rates)
    if rate is None:
        return None
    index = IdIndex([inv.id], cfg.ref_prefixes)
    ctx = _Inv(inv, inv.amount * Decimal(str(cfg.fx_tolerance_pct)) / 100, today or date(2100, 1, 1))
    s = _score(ctx, pay, _info_for(pay, index), cfg, rate)
    return None if s.date_problem else s


def match_all(invoices, payments, today, cfg, excluded=None):
    results, leftovers, _early = match_all_ex(invoices, payments, today, cfg, excluded)
    return results, leftovers


def match_all_ex(invoices: list[Invoice], payments: list[Payment], today: date, cfg: Config, excluded=None):
    """Returns (results in invoice order, leftover [(Payment, remaining)], early [Early]).

    excluded: {(lower-case invoice id, "source:payment id")} pairs the user rejected: never suggested again.

    Candidate pairs per invoice, never the all-pairs product:
      * payments that name this invoice id (a payment naming more than MAX_NAMED known ids is never
        paired, and an invoice named by more than K payments is not paired with them: both go to review);
      * payments from a payer who IS the client inside the date window (the K nearest the due date);
      * payments with exactly the invoice amount, only when there are at most K of them (otherwise
        the invoice goes to review in bulk).
    """
    tolpct = Decimal(str(cfg.fx_tolerance_pct)) / 100
    excluded = excluded or set()
    active = [i for i in invoices if i.status not in SETTLED and i.status not in VOID]
    K = MAX_CANDIDATES

    index = IdIndex((i.id for i in invoices), cfg.ref_prefixes)  # all ids: a paid invoice's id still makes a payment ambiguous
    infos = [_info_for(p, index) for p in payments]

    by_cur: dict[str, list[int]] = {}
    for pi, p in enumerate(payments):
        by_cur.setdefault(p.currency, []).append(pi)
    amt_idx = {}
    for cur, idxs in by_cur.items():
        idxs.sort(key=lambda i: payments[i].amount)
        amt_idx[cur] = ([payments[i].amount for i in idxs], idxs)
    by_id: dict[str, list[int]] = defaultdict(list)
    multi_named: dict[str, list] = {}  # invoice id -> [first payment id that names it, how many invoices that payment names, count]
    payer_raw: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
    for pi, inf in enumerate(infos):
        if len(inf.ids) > MAX_NAMED:
            for iid in inf.ids:
                if iid in multi_named:
                    multi_named[iid][2] += 1
                else:
                    multi_named[iid] = [payments[pi].id, len(inf.ids), 1]
        else:
            for iid in inf.ids:
                by_id[iid].append(pi)
        key = company_key(payments[pi].payer) if payments[pi].payer else None
        if key:
            payer_raw[key].append((payments[pi].date.toordinal(), pi))
    payer_idx = {}
    for key, lst in payer_raw.items():
        lst.sort()
        payer_idx[key] = ([o for o, _ in lst], [pi for _, pi in lst])

    by_name: dict[tuple, list[Invoice]] = defaultdict(list)
    for i in invoices:
        k = company_key(i.client)
        if k:
            by_name[k].append(i)

    eps = Decimal("0.0000001")
    pairs = []
    early: list[Early] = []
    bulk: dict[str, str] = {}  # invoice id -> why it is held in bulk, without pairing
    for inv in active:
        rates = {}
        for cur in by_cur:
            r = _rate(cur, inv.currency, cfg.fx_rates)
            if r is not None:
                rates[cur] = r
        tol = inv.amount * tolpct
        key = company_key(inv.client)
        group = by_name.get(key, [])
        peers = tuple(o for o in group if o is not inv and o.amount == inv.amount and o.currency == inv.currency
                      and (COUNT_SETTLED_PEERS or (o.status not in SETTLED and o.status not in VOID)))
        emails = {o.email.strip().lower() for o in group if o.email.strip()}
        ctx = _Inv(inv, tol, today, peers, bool(CHECK_NAME_IDENTITY and len(emails) > 1))
        if inv.id in multi_named:
            pid, n_named, count = multi_named[inv.id]
            bulk[inv.id] = (f"payment {pid} names more than {MAX_NAMED} invoices at once"
                            + (f" (and {count - 1} more like it)" if count > 1 else "")
                            + ": it cannot be matched to one of them; check your statement before chasing")
        named_here = by_id.get(inv.id, [])
        if len(named_here) > K:
            bulk[inv.id] = (f"{len(named_here)} payments name this invoice id: too many to pair; "
                            f"check your statement before chasing")
            named_here = []
        if not rates:
            continue
        cand: set[int] = set(named_here)
        n_same = 0
        for cur, r in rates.items():
            amts, idxs = amt_idx[cur]
            lo = bisect_left(amts, inv.amount / r - eps)
            hi = bisect_right(amts, inv.amount / r + eps)
            n_same += hi - lo
            if n_same <= K:
                cand.update(idxs[lo:hi])
        if n_same > K:  # a shared price point: pairing would be quadratic and meaningless
            bulk.setdefault(inv.id, f"{n_same} payments have exactly this amount and nothing identifies yours (no invoice id, "
                                    f"payer is not this client): too many to list; check your statement before chasing")
        if key in payer_idx:
            ords, idxs = payer_idx[key]
            o = inv.issued.toordinal()
            a, b = bisect_left(ords, o - NAME_LOOKBACK_DAYS), bisect_right(ords, o + cfg.date_window_days)
            if b - a > K:  # keep the K payments nearest the due date
                mid = bisect_left(ords, inv.due.toordinal(), a, b)
                a = max(a, min(mid - K // 2, b - K))
                b = a + K
            cand.update(idxs[a:b])
        for pi in sorted(cand):
            p = payments[pi]
            rate = rates.get(p.currency)
            if rate is None or len(infos[pi].ids) > MAX_NAMED or (inv.id.strip().lower(), f"{p.source}:{p.id}") in excluded:
                continue  # a payment that names many invoices is never paired with any of them
            sc = _score(ctx, p, infos[pi], cfg, rate)
            if sc.date_problem:
                if sc.confidence >= cfg.review_threshold:
                    early.append(Early(p, inv, sc.confidence, sc.date_problem))
                    pairs.append((sc, inv, pi))
                continue
            if sc.auto or sc.confidence >= cfg.review_threshold:
                pairs.append((sc, inv, pi))

    # A name-path suggestion (no invoice id) that fits more than one invoice is ambiguous: do not guess.
    by_payment: dict[int, list] = defaultdict(list)
    for item in pairs:
        if item[0].auto and not item[0].via_ref:
            by_payment[item[2]].append(item)
    cap = max(round(cfg.auto_threshold - 0.01, 2), cfg.review_threshold)
    for pi, items in by_payment.items():
        if len(items) > 1:
            ids = ", ".join(i.id for _, i, _ in items)
            for s, inv, _ in items:
                s.auto = False
                s.blocker = f"payment fits several invoices ({ids}) and carries no invoice id to tell them apart"
                s.confidence = min(s.confidence, cap)
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
        confirmed[inv.id].append(Match(inv.id, p.id, p.date, p.description, s.confidence, s.reason, alloc,
                                       payer=p.payer, source=p.source, currency=p.currency, amount=p.amount,
                                       named=tuple(sorted(infos[pi].ids))))

    for s, inv, pi in pairs:
        if s.auto or rem[pi] <= 0:
            continue
        if paid[inv.id] >= inv.amount * (1 - tolpct) or len(suggested[inv.id]) >= 2:
            continue
        p = payments[pi]
        suggested[inv.id].append(Match(inv.id, p.id, p.date, p.description, s.confidence, s.reason,
                                       blocker=s.blocker or f"confidence {s.confidence} is below {cfg.auto_threshold}",
                                       payer=p.payer, source=p.source, currency=p.currency, amount=p.amount,
                                       named=tuple(sorted(infos[pi].ids))))

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
        stage = None
        if got >= inv.amount - tol:
            st, out = "suggested_paid", Decimal(0)
            why = "waiting for your confirm: strong evidence, but only you can mark it paid"
        elif got > 0:
            st, out = "partial_suggested", inv.amount - got
            why = f"{got:.2f} of {inv.amount:.2f} found; confirm it to record the part-payment and chase the rest"
        elif sg:
            st, out = "needs_review", inv.amount
            why = "; ".join(f"{m.payment_id}: {m.blocker}" for m in sg)
        elif inv.id in bulk:
            st, out = "needs_review", inv.amount
            why = bulk[inv.id]
        elif overdue > 0:
            st, out, stage = "overdue", inv.amount, stage_for(overdue, cfg.stage_days)
            why = "no payment in the exports carries this invoice id, amount or client name"
        else:
            st, out = "open", inv.amount
            why = f"not due until {inv.due.isoformat()}"
        if ms:
            conf = min(m.confidence for m in ms)
        elif sg:
            conf = max(m.confidence for m in sg)
        else:
            conf = 0.0
        note = ""
        if st == "needs_review":
            note = "possible payment found but it is not certain: no reminder drafted until you check"
        elif st in ("suggested_paid", "partial_suggested"):
            note = "no reminder is drafted while a payment is suggested: confirm or reject it"
            if sg:
                note += "; a further possible payment needs review"
        r = InvoiceResult(inv, st, got.quantize(Decimal("0.01")), out.quantize(Decimal("0.01")),
                          overdue, stage, conf, ms, sg, note)
        r.why_not_paid = why
        results.append(r)
    leftovers = [(p, rem[pi]) for pi, p in enumerate(payments) if rem[pi] > Decimal("0.005")]
    return results, leftovers, early
