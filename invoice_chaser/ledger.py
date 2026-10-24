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

import contextlib
import hashlib
import json
import os
import re
import time
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from .ladder import STAGES

_SECTIONS = ("sent", "paid", "received", "rejected", "dropped", "reconfirm")


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
            amount = Decimal(e["amount"])
            if not amount.is_finite() or amount < 0 or amount >= Decimal("1e12"):
                raise ValueError(f"received amount {e['amount']!r} must be a finite, non-negative number")
        for e in out["rejected"].values():
            if not isinstance(e, list):
                raise ValueError("rejected entries must be lists")
        if not isinstance(out["dropped"], dict) or not isinstance(out["reconfirm"], dict):
            raise ValueError("dropped and reconfirm must be objects")
    except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError) as exc:
        raise ValueError(f"{p.name}: not a valid sent ledger ({exc})") from exc
    return out


@contextlib.contextmanager
def _locked(path: str | Path, timeout: float = 30.0):
    """An exclusive lock on <ledger>.lock (msvcrt on Windows, fcntl elsewhere), held for a whole read-modify-write,
    so parallel `mark-sent` / `confirm` runs cannot lose each other's entries."""
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock, "a+b")
    deadline = time.monotonic() + timeout
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            while True:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise ValueError(f"could not lock {lock.name} (another invoice-chaser is writing the ledger)") from None
                    time.sleep(0.02)
        else:
            import fcntl
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise ValueError(f"could not lock {lock.name} (another invoice-chaser is writing the ledger)") from None
                    time.sleep(0.02)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


def _write(path: str | Path, data: dict) -> None:
    """Atomic: write a uniquely named temp file next to the ledger, then replace. Call inside _locked()."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
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


def load_dropped(path: str | Path) -> dict[str, list[str]]:
    return _read(path)["dropped"]


def load_reconfirm(path: str | Path) -> dict[str, dict]:
    """Invoices whose confirmed part-payment was rolled back by a migration: they need your confirm again."""
    return _read(path)["reconfirm"]


def load_payment_rows(path: str | Path) -> list[dict]:
    """Every payment you confirmed, as it looked then: {invoice, key, date, amount, currency, payer}. Used to
    recognise the same payment again after a cosmetic re-export changed its id."""
    data = _read(path)
    rows = []
    for inv, entry in list(data["paid"].items()) + list(data["received"].items()):
        for r in entry.get("payment_rows", []):
            rows.append({"invoice": inv, **r})
    return rows


# Before 1.1.1 a payment without a transaction id was keyed by its ROW NUMBER in the export (wise:wise-3).
# Row numbers move when you export again, so such a key could land on a different client's payment.
_ROW_KEY = re.compile(r"^(wise|payoneer|bank):(wise|payoneer|bank)-\d+$")


def migrate_row_keys(path: str | Path) -> list[str]:
    """Drop ledger entries keyed by a row number. Returns one message per dropped key (also kept in the
    ledger's "dropped" section, so review.html keeps showing them).

    A confirmed invoice stays confirmed; only its row-keyed payment reference goes. A confirmed PART-payment is
    rolled back instead: its amount is no longer tied to any payment we can recognise, and keeping it would count
    the same payment twice when it is suggested again. The invoice is flagged "re-confirm after upgrade"."""
    p = Path(path)
    if not p.exists():
        return []
    with _locked(path):
        data = _read(path)
        messages = []
        for inv, entry in list(data["paid"].items()):
            for k in [k for k in entry.get("payments", []) if _ROW_KEY.match(k)]:
                entry["payments"].remove(k)
                data["dropped"].setdefault(inv, []).append(k)
                messages.append(f"{inv}: dropped payment reference {k} from your confirmation (row-numbered ids are not stable)")
        for inv, entry in list(data["received"].items()):
            bad = [k for k in entry.get("payments", []) if _ROW_KEY.match(k)]
            if bad:
                del data["received"][inv]
                data["dropped"].setdefault(inv, []).extend(bad)
                data["reconfirm"][inv] = {"amount": entry["amount"], "date": entry["date"]}
                messages.append(f"{inv}: your confirmed part-payment of {entry['amount']} was rolled back because it was tied to "
                                f"row-numbered payment ids ({', '.join(bad)}): re-confirm after upgrade")
        for inv, keys in list(data["rejected"].items()):
            bad = [k for k in keys if _ROW_KEY.match(k)]
            for k in bad:
                keys.remove(k)
                data["dropped"].setdefault(inv, []).append(k)
                messages.append(f"{inv}: your rejection of {k} was dropped (row-numbered ids are not stable): "
                                f"if that payment is suggested again, reject it again")
            if not keys:
                del data["rejected"][inv]
        if messages:
            _write(path, data)
        return messages


def record_sent(path: str | Path, invoice_id: str, stage: str, when: date) -> None:
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {', '.join(STAGES)}")
    with _locked(path):
        data = _read(path)
        entries = data["sent"].setdefault(invoice_id, [])
        entry = {"stage": stage, "date": when.isoformat()}
        if entry not in entries:
            entries.append(entry)
        _write(path, data)


def record_paid(path: str | Path, invoice_id: str, when: date, note: str = "", snapshot: dict | None = None,
                payments=(), source: str = "mark-paid", rows=()) -> None:
    with _locked(path):
        data = _read(path)
        data["paid"][invoice_id] = {"date": when.isoformat(), "note": note, "source": source,
                                    "payments": list(payments), "snapshot": snapshot or {}, "payment_rows": list(rows)}
        data["received"].pop(invoice_id, None)  # fully paid now
        data["reconfirm"].pop(invoice_id, None)
        _write(path, data)


def record_received(path: str | Path, invoice_id: str, when: date, amount: Decimal, payments=(),
                    snapshot: dict | None = None, rows=()) -> None:
    if not Decimal(amount).is_finite() or Decimal(amount) < 0:
        raise ValueError(f"received amount {amount!r} must be a finite, non-negative number")
    with _locked(path):
        data = _read(path)
        data["received"][invoice_id] = {"date": when.isoformat(), "amount": str(amount), "payments": list(payments),
                                        "snapshot": snapshot or {}, "payment_rows": list(rows)}
        data["reconfirm"].pop(invoice_id, None)
        _write(path, data)


def record_rejected(path: str | Path, invoice_id: str, payment_id: str) -> None:
    with _locked(path):
        data = _read(path)
        ids = data["rejected"].setdefault(invoice_id, [])
        if payment_id not in ids:
            ids.append(payment_id)
        _write(path, data)


def unrecord_paid(path: str | Path, invoice_id: str) -> bool:
    """Remove a confirmation (paid or part-payment). Returns False if there was none."""
    with _locked(path):
        data = _read(path)
        want = invoice_id.strip().lower()
        hit = False
        for section in ("paid", "received", "reconfirm"):
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
