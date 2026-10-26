"""The ONE gate every client-addressed draft must pass (normal reminders, CHECK FIRST reminders, and any future path).

`client_draft_allowed(invoice, ledger, holds)` answers: may a reminder that goes to the CLIENT be written for this invoice now?
Internal notes to you are not client-addressed and do not go through it.

ledger: {invoice id (lower case): {"kind": "paid" | "received", "date": str, "snapshot": dict, "gone": bool}}
        one entry per confirmation or mark-paid. "gone" is True when the confirmed invoice id is no longer in invoices.csv.
holds:  list of Hold, every reason an invoice waits for you (built once per run in cli.py).

Returns (True, "") or (False, reason). A held invoice with a possible payment that may have arrived never gets a client reminder.
"""
from __future__ import annotations

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
    for other, e in ledger.items():  # a confirmed invoice that was renamed (old id gone) with the same client, amount and currency
        if other == key or not e.get("gone"):
            continue
        s = e.get("snapshot") or {}
        if (str(s.get("client", "")).strip().casefold() == invoice.client.strip().casefold()
                and same_money(s.get("amount"), invoice.amount) and str(s.get("currency", "")) == str(invoice.currency)):
            return False, f"you confirmed {other.upper()} (now gone from invoices.csv) for the same client, amount and currency: it may be this invoice renamed"
    return True, ""
