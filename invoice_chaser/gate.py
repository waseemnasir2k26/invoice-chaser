"""The ONE gate every client-addressed draft must pass (normal reminders, CHECK FIRST reminders, and any future path).

`client_draft_allowed(invoice, ledger, holds)` answers: may a reminder that goes to the CLIENT be written for this invoice now?
Internal notes to you are not client-addressed and do not go through it.

ledger: {invoice id (lower case): {"kind": "paid" | "received", "date": str, "snapshot": dict, "gone": bool}}
        one entry per confirmation or mark-paid. "gone" is True when the confirmed invoice id is no longer in invoices.csv.
holds:  list of Hold, every reason an invoice waits for you (built once per run in cli.py).

Returns (True, "") or (False, reason). A held invoice with a possible payment that may have arrived never gets a client reminder.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# hold kinds where the money may already have arrived: never a client reminder, and past the stuck window a CRITICAL STUCK_HOLD line
PAYMENT_HOLDS = ("double_bound", "double_count", "refund_name")


@dataclass(frozen=True)
class Hold:
    kind: str  # "orphan" | "double_bound" | "double_count" | "refund_name" | "review"
    invoice: str = ""  # lower-case id of the invoice held ("" for an orphan, which names several)
    refs: frozenset = field(default_factory=frozenset)  # lower-case ids this hold references (an orphan: its invoice and the ids its candidates name)
    reason: str = ""


def same_money(a, b) -> bool:
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except (InvalidOperation, ValueError, TypeError):
        return False


_PUNCT = re.compile(r"[\W_]+")


def company_words(name) -> tuple | None:
    """A client name as its words only, ignoring punctuation, capitals, spacing, a leading "The" and the legal suffix
    (Renamed Co. = Renamed  Co = Renamed Company). None when the name has no usable words. Deliberately looser than
    decision.company_key (which keeps Ltd apart from Inc): this check decides whether a held invoice may be a renamed one,
    so it errs towards matching."""
    words = _PUNCT.sub(" ", str(name or "").casefold()).split()
    if words and words[0] == "the" and len(words) > 1:
        words = words[1:]
    while len(words) > 1 and words[-1] in _SUFFIXES:
        words.pop()
    return tuple(words) or None


_SUFFIXES = frozenset({"ltd", "limited", "inc", "incorporated", "llc", "llp", "co", "company", "corp", "corporation", "plc", "gmbh",
                       "sdn", "bhd", "pty", "pvt", "pte", "sa", "srl", "bv", "ab", "as", "oy", "kft", "sro", "nv", "spa", "ag"})


def edit_distance(a: str, b: str, limit: int = 2) -> int:
    """Levenshtein distance, stopped early: returns limit + 1 as soon as it is clear the distance is larger than limit."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return min(prev[-1], limit + 1)


def renamed_match(snapshot: dict, old_id: str, invoice) -> list[str]:
    """Why the current invoice may be the confirmed invoice `old_id` renamed and/or edited. Empty list = no reason to think so.
    Any ONE of these is enough (conservative by design; a wrong hold costs one `forget`, a wrong draft costs a client):
      - the same client (company_words: punctuation, capitals, spacing and legal suffix ignored);
      - the same amount and currency as the confirmed snapshot;
      - an id within two edits of the old id (INV-A -> INV-A2, inv-a -> INV-A).
    No snapshot = no reason from the snapshot; the id test still applies."""
    why = []
    if not snapshot:
        snapshot = {}
    a = company_words(snapshot.get("client"))
    if a is not None and a == company_words(invoice.client):
        why.append("same client")
    if snapshot.get("amount") not in (None, "") and same_money(snapshot.get("amount"), invoice.amount)             and str(snapshot.get("currency", "")).upper() == str(invoice.currency).upper():
        why.append("same amount and currency")
    if edit_distance(old_id.strip().lower(), invoice.id.strip().lower()) <= 2:
        why.append("id within two edits")
    return why


def client_draft_allowed(invoice, ledger: dict, holds) -> tuple[bool, str]:
    key = invoice.id.strip().lower()
    holds = list(holds)
    entry = ledger.get(key)
    if entry and entry.get("kind") == "paid":
        return False, f"you confirmed it paid on {entry.get('date')}, so the money may have arrived"
    if entry and entry.get("kind") == "received" and any(key in h.refs for h in holds):
        return False, f"you confirmed a part-payment on {entry.get('date')} and the invoice is held: the money may have arrived"
    for h in holds:  # a renamed or changed decision: the old id is gone, and this invoice is one of the ids it may point at
        if h.kind == "orphan" and key in h.refs:
            return False, "an orphaned decision names this invoice (ORPHANED_DECISION): rebind or forget it first"
    for h in holds:
        if h.kind in PAYMENT_HOLDS and h.invoice == key:
            return False, h.reason or f"held as {h.kind}: the money may have arrived"
    for other, e in ledger.items():  # a confirmed invoice whose old id is gone and that may now be this invoice (renamed and/or edited)
        if other == key or not e.get("gone"):
            continue
        why = renamed_match(e.get("snapshot") or {}, other, invoice)
        if why:
            return False, (f"you confirmed {other.upper()} (now gone from invoices.csv) and this invoice may be it renamed "
                           f"({'; '.join(why)}): rebind or forget it first")
    return True, ""
