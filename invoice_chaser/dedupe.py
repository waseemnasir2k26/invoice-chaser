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

from .decision import company_key
from .gate import company_words
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


def row_matches(p, row: dict, days: int = 0) -> bool:
    """Is this payment the one the ledger row describes? Date (within `days`), amount, currency and payer (as a company)
    must all agree. An id match alone is never enough: banks reuse ids and two exports can make the same id."""
    me = payment_row(p)
    try:
        if abs((date.fromisoformat(row["date"]) - date.fromisoformat(me["date"])).days) > days:
            return False
        if Decimal(row["amount"]) != Decimal(me["amount"]):
            return False
    except (ValueError, ArithmeticError, KeyError, TypeError):
        return False
    if row.get("currency") != me["currency"]:
        return False
    if me["payer"] or row.get("payer"):
        ka, kb = company_key(me["payer"]) if me["payer"] else None, company_key(row["payer"]) if row.get("payer") else None
        return ka is not None and ka == kb
    return me["text"] == row.get("text", "")


_HASH_ID = re.compile(r"^h-[0-9a-f]{12}$")


def is_hash_id(payment_id: str) -> bool:
    """True for an id made by this tool (no transaction id in the export)."""
    return bool(_HASH_ID.match(payment_id or ""))


def repeat_of(p, rows: list[dict], named=()) -> dict | None:
    """The confirmed payment this one looks like, or None.

    Looks like: date within a day, same amount and currency, and the same company as payer (ACME LTD = Acme Limited).
    NOT a repeat when the two payments carry different real transaction ids from the bank, or when this payment's
    text names an invoice other than the one the confirmed payment was for: those are two payments. The hold applies
    to everything else, including a part-payment remainder equal to the deposit: counting a payment twice could mark an
    invoice paid that is not, so when in doubt it is held for you (`mark-paid` if you checked)."""
    me = payment_row(p)
    my_id = (p["id"] if isinstance(p, dict) else p.id)
    named_l = {str(n).strip().lower() for n in named}
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
        rid = str(r.get("key", "")).split(":", 1)[-1]
        if not is_hash_id(my_id) and not is_hash_id(rid) and my_id != rid:
            continue  # two different transaction ids from the bank: two payments
        if named_l and str(r.get("invoice", "")).strip().lower() not in named_l:
            continue  # it names a different invoice than the one the confirmed payment was for
        if me["payer"] and r.get("payer") and company_key(me["payer"]) is not None and company_key(me["payer"]) == company_key(r["payer"]):
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


def flag_same_payment_in_two_exports(payments: list[Payment], named_ids, warn) -> None:
    """The same payment often appears in two exports under different payer names ("Acme Ltd" in Wise, "Jane Okafor" in the bank
    export). Same amount and currency, within two days, from two different sources, and either the same invoice id named in both
    texts or both payers blank: both are held in needs review. Counting both could mark an invoice paid twice."""
    groups: dict[tuple, list[Payment]] = {}
    for p in payments:
        groups.setdefault((p.currency, p.amount), []).append(p)
    for group in groups.values():
        group.sort(key=lambda p: p.date)
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if (b.date - a.date).days > 2:
                    break
                if a.source == b.source:
                    continue
                shared = set(named_ids(a)) & set(named_ids(b))
                if shared or (not a.payer.strip() and not b.payer.strip()):
                    for x, y in ((a, b), (b, a)):
                        x.suspect = x.suspect or f"possible same payment seen in two exports ({y.source} {y.id})"
                    inv = sorted(shared)[0] if shared else "<invoice>"
                    warn(f"possible same payment seen in two exports: {a.source} {a.id} and {b.source} {b.id} ({a.amount} {a.currency}, "
                         f"{a.date} / {b.date}). Both are held in needs review and neither is suggested as paid. Next step: check your "
                         f"statement. If it is one payment, record it with `invoice-chaser confirm {inv} --payment <id>` naming just one of "
                         f"them; if both are real, name both.", "POSSIBLE_DOUBLE_COUNT")


def _exact_payer(a: Payment, b: Payment) -> bool:
    """Both payers name the same company (ACME LTD = Acme Limited). A blank payer never matches."""
    ka, kb = company_words(a.payer), company_words(b.payer)
    return ka is not None and ka == kb


def dedupe_payments(payments: list[Payment], warn) -> list[Payment]:
    """Rows of ONE file that share a real id, or identical rows of one file, are counted as the file says (a duplicate id
    with identical data inside one file is dropped with a warning). Anything that may be the same money seen twice across
    DIFFERENT FILES is kept on both rows and held as a possible double count, never silently dropped or counted twice:
      - the same id with identical data in two files of one source (overlapping statements, with or without a shared transfer id);
      - the same amount and currency, within a day, same payer company, in two files of one source;
      - the same amount and currency, within a day, same-looking payer, in two different sources (as before)."""
    kept: list[Payment] = []
    seen: dict[tuple[str, str], Payment] = {}
    flagged_pairs: set[frozenset] = set()
    for p in payments:
        key = (p.source, p.id)
        q = seen.get(key)
        if q is not None:
            if (q.amount, q.currency, q.date) == (p.amount, p.currency, p.date):
                if q.file_no == p.file_no:
                    warn(f"duplicate payment row dropped: {p.source} id {p.id} appears twice "
                         f"({p.date}, {p.amount} {p.currency})", "DUPLICATE_PAYMENT_ROW")
                    continue
                # the same payment in two different files of one source: one payment or two? both kept, held for review
                why = f"may be the same payment as {q.source} {q.id} (same id, two different files)"
                q.suspect = q.suspect or why
                p.suspect = p.suspect or f"may be the same payment as {p.source} {q.id} (same id, two different files)"
                flagged_pairs.add(frozenset((id(q), id(p))))
                warn(f"possible duplicate across files: {p.source} id {p.id} appears in two of your {p.source} exports "
                     f"({p.date}, {p.amount} {p.currency}); both are kept and neither will be counted as paid until you review",
                     "POSSIBLE_DOUBLE_COUNT")
                kept.append(p)
                continue
            reason = f"{p.source} id {p.id} is used by two different payments"
            warn(f"{reason}; both kept but marked for review", "ID_COLLISION")
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
                if frozenset((id(a), id(b))) in flagged_pairs:
                    continue  # already flagged above
                if a.source != b.source:
                    same = _same_payer(a, b)
                    kind = "across sources"
                else:  # one source, two different files: an exact repeat (same payer company) may be one payment or two
                    same = a.file_no != b.file_no and _exact_payer(a, b)
                    kind = "across files of one source"
                if same:
                    a.suspect = a.suspect or f"may be the same payment as {b.source} {b.id}"
                    b.suspect = b.suspect or f"may be the same payment as {a.source} {a.id}"
                    warn(f"possible duplicate {kind}: {a.source} {a.id} and {b.source} {b.id} "
                         f"({a.amount} {a.currency}, {a.date}); neither will be counted as paid until you review", "POSSIBLE_DOUBLE_COUNT")
    return kept
