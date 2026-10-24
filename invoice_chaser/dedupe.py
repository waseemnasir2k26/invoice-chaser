"""Duplicate payment rows must never settle two invoices.

1. The same transfer id twice in one source (overlapping statements): the second row is
   dropped, with a warning.
2. The same payment seen in two different sources (a Wise statement AND your bank export):
   both rows are kept but flagged `suspect`, so neither can be counted as paid on its own.
   They land in "needs review" and you decide.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from .matching import norm_name
from .models import Payment


def _squash(t: str) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", (t or "").casefold()).split())


def payment_row(p) -> dict:
    """What the ledger remembers about a confirmed payment, to recognise it again if a re-export changes its id."""
    return {"key": (p.get("key") or f"{p.get('source', '')}:{p['id']}") if isinstance(p, dict) else f"{p.source}:{p.id}",
            "date": (p["date"] if isinstance(p, dict) else p.date.isoformat()),
            "amount": str(p["amount"] if isinstance(p, dict) else p.amount),
            "currency": p["currency"] if isinstance(p, dict) else p.currency,
            "payer": _squash(p["payer"] if isinstance(p, dict) else p.payer),
            "text": "" if (p["payer"] if isinstance(p, dict) else p.payer) else
            _squash(p["description"] if isinstance(p, dict) else p.description)[:80]}


def repeat_of(p, rows: list[dict]) -> dict | None:
    """The confirmed payment this one looks like (date within a day, same amount, currency and payer), or None.
    A cosmetic change to the description or punctuation does not make a payment a new one."""
    me = payment_row(p)
    try:
        d, amt = date.fromisoformat(me["date"]), Decimal(me["amount"])
    except (ValueError, ArithmeticError):
        return None
    for r in rows:
        try:
            if abs((date.fromisoformat(r["date"]) - d).days) > 1 or Decimal(r["amount"]) != amt:
                continue
        except (ValueError, ArithmeticError, KeyError):
            continue
        if r.get("currency") != me["currency"]:
            continue
        if me["payer"] and r.get("payer") == me["payer"]:
            return r
        if not me["payer"] and not r.get("payer") and me["text"] and r.get("text") == me["text"]:
            return r
    return None


def _same_payer(a: Payment, b: Payment) -> bool:
    na, nb = norm_name(a.payer), norm_name(b.payer)
    ta = set(norm_name(f"{a.payer} {a.description}").split())
    tb = set(norm_name(f"{b.payer} {b.description}").split())
    if na and nb:
        return set(na.split()) <= tb or set(nb.split()) <= ta
    if na:
        return set(na.split()) <= tb
    if nb:
        return set(nb.split()) <= ta
    return False


def dedupe_payments(payments: list[Payment], warn) -> list[Payment]:
    kept: list[Payment] = []
    seen: dict[tuple[str, str], Payment] = {}
    for p in payments:
        key = (p.source, p.id)
        q = seen.get(key)
        if q is not None:
            if (q.amount, q.currency, q.date) == (p.amount, p.currency, p.date):
                warn(f"duplicate payment row dropped: {p.source} id {p.id} appears twice "
                     f"({p.date}, {p.amount} {p.currency})")
                continue
            reason = f"{p.source} id {p.id} is used by two different payments"
            warn(f"{reason}; both kept but marked for review")
            p.suspect = q.suspect = reason
        else:
            seen[key] = p
        kept.append(p)

    groups: dict[tuple, list[Payment]] = {}
    for p in kept:
        groups.setdefault((p.currency, p.amount), []).append(p)
    for group in groups.values():
        group.sort(key=lambda p: p.date)
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if (b.date - a.date).days > 1:  # sorted by date: nothing later can be a duplicate of a
                    break
                if a.source != b.source and _same_payer(a, b):
                    a.suspect = a.suspect or f"may be the same payment as {b.source} {b.id}"
                    b.suspect = b.suspect or f"may be the same payment as {a.source} {a.id}"
                    warn(f"possible duplicate across sources: {a.source} {a.id} and {b.source} {b.id} "
                         f"({a.amount} {a.currency}, {a.date}); neither will be counted as paid until you review")
    return kept
