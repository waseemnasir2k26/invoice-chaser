"""sent.json: the record of what YOU decided. Only this file can make an invoice "paid".

The tool suggests, you decide:
  paid      written by `invoice-chaser confirm INV-1` (you accepted a suggested payment) or
            `invoice-chaser mark-paid INV-1` (you checked yourself). Stores a snapshot of the invoice
            (client, amount, currency, due). If invoices.csv later differs from the snapshot, the
            invoice goes back to needs-review: "invoice changed since confirmed".
  received  a confirmed PART-payment: the amount is deducted and the rest is chased.
  rejected  `invoice-chaser reject INV-1 PAYMENT-ID`: that payment is never suggested for that invoice again.
  sent      reminders you actually sent (`invoice-chaser mark-sent INV-1`): the same stage is never
            drafted twice and the next draft can say "following my reminder of <date>".

Format: {"sent": {"INV-1": [{"stage": "nudge", "date": "2026-10-08"}]},
         "paid": {"INV-2": {"date": "2026-10-20", "note": "", "source": "confirm", "payments": ["W1"],
                            "snapshot": {"client": "Acme", "amount": "1200.00", "currency": "USD", "due": "2026-10-01"}}},
         "received": {"INV-3": {"date": "2026-10-20", "amount": "1000.00", "payments": ["W2"], "snapshot": {...}}},
         "rejected": {"INV-4": ["P9"]}}
A legacy flat file ({"INV-1": [...]}) is still read as the "sent" section.
"""
from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

from .ladder import STAGES

_SECTIONS = ("sent", "paid", "received", "rejected")


def _read(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {k: {} for k in _SECTIONS}
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
        if not set(data) & set(_SECTIONS):  # legacy flat format
            data = {"sent": data}
        out = {k: data.get(k, {}) for k in _SECTIONS}
        if not all(isinstance(v, dict) for v in out.values()):
            raise ValueError("every section must be an object")
        for entries in out["sent"].values():
            for e in entries:
                if e["stage"] not in STAGES:
                    raise ValueError(f"unknown stage {e['stage']!r}")
                date.fromisoformat(e["date"])
        for e in list(out["paid"].values()) + list(out["received"].values()):
            date.fromisoformat(e["date"])
        for e in out["received"].values():
            Decimal(e["amount"])
        for e in out["rejected"].values():
            if not isinstance(e, list):
                raise ValueError("rejected entries must be lists")
    except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError) as exc:
        raise ValueError(f"{p}: not a valid sent ledger ({exc})") from exc
    return out


def _write(path: str | Path, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def load_ledger(path: str | Path) -> dict[str, list[dict]]:
    """The 'sent' section: invoice id -> [{stage, date}]."""
    return _read(path)["sent"]


def load_paid(path: str | Path) -> dict[str, dict]:
    return _read(path)["paid"]


def load_received(path: str | Path) -> dict[str, dict]:
    return _read(path)["received"]


def load_rejected(path: str | Path) -> dict[str, list[str]]:
    return _read(path)["rejected"]


def record_sent(path: str | Path, invoice_id: str, stage: str, when: date) -> None:
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {', '.join(STAGES)}")
    data = _read(path)
    entries = data["sent"].setdefault(invoice_id, [])
    entry = {"stage": stage, "date": when.isoformat()}
    if entry not in entries:
        entries.append(entry)
    _write(path, data)


def record_paid(path: str | Path, invoice_id: str, when: date, note: str = "", snapshot: dict | None = None,
                payments=(), source: str = "mark-paid") -> None:
    data = _read(path)
    data["paid"][invoice_id] = {"date": when.isoformat(), "note": note, "source": source,
                                "payments": list(payments), "snapshot": snapshot or {}}
    data["received"].pop(invoice_id, None)  # fully paid now
    _write(path, data)


def record_received(path: str | Path, invoice_id: str, when: date, amount: Decimal, payments=(),
                    snapshot: dict | None = None) -> None:
    data = _read(path)
    data["received"][invoice_id] = {"date": when.isoformat(), "amount": str(amount), "payments": list(payments),
                                    "snapshot": snapshot or {}}
    _write(path, data)


def record_rejected(path: str | Path, invoice_id: str, payment_id: str) -> None:
    data = _read(path)
    ids = data["rejected"].setdefault(invoice_id, [])
    if payment_id not in ids:
        ids.append(payment_id)
    _write(path, data)


def unrecord_paid(path: str | Path, invoice_id: str) -> bool:
    """Remove a confirmation (paid or part-payment). Returns False if there was none."""
    data = _read(path)
    want = invoice_id.strip().lower()
    hit = False
    for section in ("paid", "received"):
        for k in [k for k in data[section] if str(k).strip().lower() == want]:
            del data[section][k]
            hit = True
    if hit:
        _write(path, data)
    return hit


def ci(mapping: dict) -> dict:
    """Case-insensitive view of a ledger section (ids are matched ignoring case and spaces)."""
    return {str(k).strip().lower(): v for k, v in mapping.items()}


def snapshot_of(invoice) -> dict:
    return {"client": invoice.client, "amount": str(invoice.amount), "currency": invoice.currency,
            "due": invoice.due.isoformat()}


def snapshot_diff(snapshot: dict, invoice) -> list[str]:
    """What changed in invoices.csv since the invoice was confirmed. Empty list = unchanged (or no snapshot)."""
    if not snapshot:
        return []
    changes = []
    if snapshot.get("client", "").strip().casefold() != invoice.client.strip().casefold():
        changes.append(f"client {snapshot['client']!r} -> {invoice.client!r}")
    try:
        if Decimal(snapshot.get("amount", "0")) != invoice.amount:
            changes.append(f"amount {snapshot['amount']} -> {invoice.amount}")
    except ArithmeticError:
        changes.append("amount")
    if snapshot.get("currency", "").upper() != invoice.currency.upper():
        changes.append(f"currency {snapshot['currency']} -> {invoice.currency}")
    if snapshot.get("due", "") != invoice.due.isoformat():
        changes.append(f"due {snapshot['due']} -> {invoice.due.isoformat()}")
    return changes


def highest_sent(entries: list[dict] | None) -> tuple[int, str, str] | None:
    """(stage index, stage, date) of the most advanced stage already sent, or None."""
    if not entries:
        return None
    best = max(entries, key=lambda e: (STAGES.index(e["stage"]), e["date"]))
    return STAGES.index(best["stage"]), best["stage"], best["date"]


def last_sent_date(entries: list[dict] | None) -> date | None:
    if not entries:
        return None
    return max(date.fromisoformat(e["date"]) for e in entries)
