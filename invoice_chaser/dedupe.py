"""Duplicate payment rows must never settle two invoices.

1. The same transfer id twice in one source (overlapping statements): the second row is
   dropped, with a warning.
2. The same payment seen in two different sources (a Wise statement AND your bank export):
   both rows are kept but flagged `suspect`, so neither can be counted as paid on its own.
   They land in "needs review" and you decide.
"""
from __future__ import annotations

from .matching import norm_name
from .models import Payment


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
