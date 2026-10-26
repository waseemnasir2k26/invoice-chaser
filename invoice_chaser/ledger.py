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

Format: {"schema": 1,
         "sent": {"INV-1": [{"stage": "nudge", "date": "2026-10-08"}]},
         "paid": {"INV-2": {"date": "2026-10-20", "note": "", "source": "confirm", "payments": ["wise:W1"],
                            "snapshot": {"client": "Acme", "amount": "1200.00", "currency": "USD", "due": "2026-10-01"},
                            "payment_rows": [{"key": "wise:W1", "date": "2026-10-03", "amount": "1200.00", "currency": "USD", "payer": "acme ltd", "text": ""}]}},
         "received": {"INV-3": {"date": "2026-10-20", "amount": "1000.00", "payments": [...], "snapshot": {...}, "payment_rows": [...]}},
         "rejected": {"INV-4": ["wise:P9"]}, "rejected_rows": {"INV-4": {"wise:P9": {"date": ..., "amount": ..., "currency": ..., "payer": ..., "text": ...}}}}

The file has a schema version. A file without one (or with an older or unknown one) is refused with a clear message, never
guessed at; `invoice-chaser ledger reset --backup` starts fresh and keeps a copy. Future versions migrate old ledgers by
fingerprint (date, amount, currency, payer), so a decision survives a change in how payment ids are made.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import time
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from .ladder import STAGES

SCHEMA = 1
_SECTIONS = ("sent", "paid", "received", "rejected", "rejected_rows")
PRE_RELEASE = ("ledger from a pre-release build (no schema version): start fresh, or run `invoice-chaser ledger reset --backup` "
               "to keep a copy of it and begin a new one")


def _empty() -> dict:
    return {"schema": SCHEMA, **{k: {} for k in _SECTIONS}}


def _read(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return _empty()
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise ValueError(f"{p.name}: not a valid sent ledger ({exc})") from exc
    if not isinstance(data, dict) or "schema" not in data:
        raise ValueError(f"{p.name}: {PRE_RELEASE}")
    if data["schema"] != SCHEMA:
        if isinstance(data["schema"], int) and data["schema"] > SCHEMA:
            raise ValueError(f"{p.name}: this ledger was written by a newer invoice-chaser (schema {data['schema']}, this one reads {SCHEMA}): "
                             f"upgrade invoice-chaser instead of editing the ledger")
        raise ValueError(f"{p.name}: schema {data['schema']!r} is not understood by this version. {PRE_RELEASE}")
    try:
        out = {"schema": SCHEMA, **{k: data.get(k, {}) for k in _SECTIONS}}
        if not all(isinstance(out[k], dict) for k in _SECTIONS):
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
        _clean_orphans(Path(path))
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


def atomic_write_text(path: str | Path, text: str, backup: bool = False) -> None:
    """Durable and atomic: write a uniquely named temp file next to the target, flush it to disk (fsync), keep a .bak of
    what is being replaced if asked, then replace. A crash leaves either the old file or the new one, never half of one."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if backup and p.exists():
            shutil.copy2(p, p.with_name(p.name + ".bak"))
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink()


def _clean_orphans(path: Path, older_than: float = 60.0) -> None:
    """Temp files left behind by a run that was killed mid-write."""
    now = time.time()
    for f in path.parent.glob(f"{path.name}.*.tmp"):
        try:
            if now - f.stat().st_mtime > older_than:
                f.unlink()
        except OSError:
            pass


def _normal(data: dict) -> dict:
    """The ledger as it is stored: every section present, plain JSON values (what a re-read returns)."""
    return json.loads(json.dumps({"schema": SCHEMA, **{k: data.get(k, {}) for k in _SECTIONS}}, ensure_ascii=False))


def _norm_id(invoice_id) -> str:
    return str(invoice_id).strip().lower()


def double_bindings(data: dict) -> dict[str, list[str]]:
    """Invariant I1: a payment key may back at most ONE invoice. Returns {payment key: [invoice ids]} for every key that
    backs two or more different invoices (ids compared ignoring case and spaces). Empty dict = the invariant holds."""
    owners: dict[str, dict[str, str]] = {}
    for section in ("paid", "received"):
        for inv, entry in data.get(section, {}).items():
            for key in entry.get("payments", []):
                owners.setdefault(key, {}).setdefault(_norm_id(inv), inv)
    return {key: sorted(ids.values()) for key, ids in owners.items() if len(ids) > 1}


def load_double_bindings(path: str | Path) -> dict[str, list[str]]:
    return double_bindings(_read(path))


def bound_to(data: dict, key: str) -> list[str]:
    """Every invoice whose confirmation (paid or part-payment) is backed by this payment key."""
    out = []
    for section in ("paid", "received"):
        for inv, entry in data.get(section, {}).items():
            if key in entry.get("payments", []):
                out.append(inv)
    return out


def _taken_keys(data: dict) -> dict[str, str]:
    """Every payment key that already has a decision, and what it is: bound to an invoice, or rejected for one."""
    taken = {}
    for section in ("paid", "received"):
        for inv, entry in data.get(section, {}).items():
            for key in entry.get("payments", []):
                taken.setdefault(key, f"confirmed for {inv}")
    for inv, keys in data.get("rejected", {}).items():
        for key in keys:
            taken.setdefault(key, f"rejected for {inv}")
    return taken


def _write(path: str | Path, data: dict) -> bool:
    """Atomic write of the ledger with a .bak of the previous version. Call inside _locked().

    Refuses (writes nothing) when the new ledger would bind a payment key to a second invoice (I1); a write that leaves an
    existing double binding in place is allowed, so that `unbind` can remove it. After writing, the file is re-read and must
    show the change (I2). Returns False, writing nothing, when the data is already what is stored."""
    new = _normal(data)
    try:
        old = _read(path)
    except ValueError:  # an unreadable or pre-release file is being replaced (reset): nothing to compare against
        old = None
    if old is not None and new == old:
        return False
    added = sorted(set(double_bindings(new)) - set(double_bindings(old or {})))
    if added:
        bindings = double_bindings(new)
        key = added[0]
        raise ValueError(f"refused: this would back payment {key} with two invoices ({' and '.join(bindings[key])}). "
                         f"Nothing was written. Run `invoice-chaser unbind <invoice> {key}` for the one that did not receive it")
    atomic_write_text(path, json.dumps(new, indent=2, ensure_ascii=False), backup=True)
    if _read(path) != new:
        raise ValueError(f"the ledger was written but reading it back does not show the change: check {Path(path).name} and its .bak before going on")
    return True


def load_ledger(path: str | Path) -> dict[str, list[dict]]:
    """The 'sent' section: invoice id -> [{stage, date}]."""
    return _read(path)["sent"]


def load_paid(path: str | Path) -> dict[str, dict]:
    return _read(path)["paid"]


def load_received(path: str | Path) -> dict[str, dict]:
    return _read(path)["received"]


def load_rejected(path: str | Path) -> dict[str, list[str]]:
    return _read(path)["rejected"]


def load_rejected_rows(path: str | Path) -> dict[str, dict]:
    """What each rejected payment looked like when you rejected it: {invoice: {key: {date, amount, currency, payer, text}}}."""
    return _read(path)["rejected_rows"]


def load_payment_rows(path: str | Path) -> list[dict]:
    """Every payment you confirmed, as it looked then: {invoice, key, date, amount, currency, payer}. Used to
    recognise the same payment again after a cosmetic re-export changed its id."""
    data = _read(path)
    rows = []
    for inv, entry in list(data["paid"].items()) + list(data["received"].items()):
        for r in entry.get("payment_rows", []):
            rows.append({"invoice": inv, **r})
    return rows


def reset_ledger(path: str | Path, backup: bool) -> Path:
    """Start a fresh ledger. Refuses if there is none at that path (a mistyped --ledger), and without backup=True. The old file is copied to
    <name>.pre-release-<timestamp>[-n] (never over an existing file), and the copy is compared byte for byte with the original before
    anything is reset. Returns the backup path."""
    p = Path(path)
    if not p.exists():
        raise ValueError(f"no ledger at {p.name} (check the --out / --ledger path): nothing to reset")
    if not backup:
        raise ValueError("resetting deletes your decisions (confirmed payments, rejections, sent reminders): add --backup to keep a copy")
    with _locked(path):
        original = p.read_bytes()
        stamp, n = time.strftime("%Y%m%d-%H%M%S"), 0
        while True:
            kept = p.with_name(f"{p.name}.pre-release-{stamp}" + (f"-{n}" if n else ""))
            if not kept.exists():
                break
            n += 1
        kept.write_bytes(original)
        if kept.read_bytes() != original or p.read_bytes() != original:
            kept.unlink(missing_ok=True)
            raise ValueError("the backup did not match the ledger, or the ledger changed while it was being copied: nothing was reset, try again")
        _write(path, _empty())
    return kept


def forget_key(path: str | Path, key: str) -> bool:
    """Remove one payment reference from every decision: the invoice stays confirmed, a rejection of that payment is lifted."""
    with _locked(path):
        data = _read(path)
        hit = False
        for section in ("paid", "received"):
            for entry in data[section].values():
                if key in entry.get("payments", []):
                    entry["payments"] = [k for k in entry["payments"] if k != key]
                    hit = True
                rows = [r for r in entry.get("payment_rows", []) if r.get("key") != key]
                hit = hit or len(rows) != len(entry.get("payment_rows", []))
                entry["payment_rows"] = rows
        for inv in list(data["rejected"]):
            if key in data["rejected"][inv]:
                data["rejected"][inv] = [k for k in data["rejected"][inv] if k != key]
                hit = True
                if not data["rejected"][inv]:
                    del data["rejected"][inv]
        for inv in list(data["rejected_rows"]):
            if key in data["rejected_rows"][inv]:
                del data["rejected_rows"][inv][key]
                hit = True
                if not data["rejected_rows"][inv]:
                    del data["rejected_rows"][inv]
        return hit and _write(path, data)


def forget_invoice(path: str | Path, invoice_id: str) -> bool:
    """Remove every decision about an invoice id (paid, part-payment, rejections). Sent reminders stay."""
    want = invoice_id.strip().lower()
    with _locked(path):
        data = _read(path)
        hit = False
        for section in ("paid", "received", "rejected", "rejected_rows"):
            for k in [k for k in data[section] if str(k).strip().lower() == want]:
                del data[section][k]
                hit = True
        return hit and _write(path, data)


def _check_target_free(data: dict, target: str, source: str) -> None:
    """I4: a rename or rebind never overwrites a decision that already sits on its target."""
    for section in ("paid", "received", "rejected", "rejected_rows", "sent"):
        for k in data[section]:
            if _norm_id(k) == _norm_id(target) and _norm_id(k) != _norm_id(source):
                raise ValueError(f"refused: {target} already has decisions in the ledger ({section}); it is not overwritten. "
                                 f"Look at it first, then `invoice-chaser forget {source}` if {source} is the stale one")


def rename_invoice(path: str | Path, old: str, new: str) -> bool:
    """You renamed an invoice id in invoices.csv: move the decisions about it (paid, part-payment, rejections, sent reminders)."""
    want = old.strip().lower()
    with _locked(path):
        data = _read(path)
        if not any(_norm_id(k) == want for section in ("paid", "received", "rejected", "rejected_rows", "sent") for k in data[section]):
            return False
        _check_target_free(data, new, old)
        for section in ("paid", "received", "rejected", "rejected_rows", "sent"):
            for k in [k for k in data[section] if str(k).strip().lower() == want]:
                data[section][new] = data[section].pop(k)
        return _write(path, data)


def rebind_keys(path: str | Path, mapping: dict[str, str]) -> bool:
    """Point confirmed and rejected payments at the keys they have TODAY (old key -> new key). Used when a payment is found again
    by its fingerprint under a different id; the decision follows the payment instead of being dropped. Refuses a target key that
    already has a decision (I1, I4): a payment key may back one invoice only, and nothing is overwritten."""
    if not mapping:
        return False
    with _locked(path):
        data = _read(path)
        taken = _taken_keys(data)
        for old, new in mapping.items():
            if new in taken:
                raise ValueError(f"refused: payment {new} already has a decision ({taken[new]}). A payment can back one invoice only, "
                                 f"and nothing is overwritten. Compare the two on your statement first")
        for section in ("paid", "received"):
            for entry in data[section].values():
                entry["payments"] = [mapping.get(k, k) for k in entry.get("payments", [])]
                for row in entry.get("payment_rows", []):
                    row["key"] = mapping.get(row.get("key"), row.get("key"))
        for inv, keys in data["rejected"].items():
            data["rejected"][inv] = [mapping.get(k, k) for k in keys]
        for inv, rows in data["rejected_rows"].items():
            data["rejected_rows"][inv] = {mapping.get(k, k): v for k, v in rows.items()}
        return _write(path, data)


def unbind_payment(path: str | Path, invoice_id: str, key: str) -> bool:
    """`invoice-chaser unbind INV PAYMENT`: this invoice's confirmation is removed, because the payment that backed it is not its
    payment. The invoice goes back to a fresh suggestion (it can only return to review, never become paid by this command).
    Returns False if this invoice is not bound to that payment."""
    want = invoice_id.strip().lower()
    with _locked(path):
        data = _read(path)
        hit = False
        for section in ("paid", "received"):
            for inv in [k for k in data[section] if _norm_id(k) == want]:
                if key in data[section][inv].get("payments", []):
                    del data[section][inv]
                    hit = True
        return hit and _write(path, data)


def record_sent(path: str | Path, invoice_id: str, stage: str, when: date) -> bool:
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {', '.join(STAGES)}")
    with _locked(path):
        data = _read(path)
        entries = data["sent"].setdefault(invoice_id, [])
        entry = {"stage": stage, "date": when.isoformat()}
        if entry not in entries:
            entries.append(entry)
        return _write(path, data)


def record_paid(path: str | Path, invoice_id: str, when: date, note: str = "", snapshot: dict | None = None,
                payments=(), source: str = "mark-paid", rows=()) -> bool:
    with _locked(path):
        data = _read(path)
        data["paid"][invoice_id] = {"date": when.isoformat(), "note": note, "source": source,
                                    "payments": list(payments), "snapshot": snapshot or {}, "payment_rows": list(rows)}
        data["received"].pop(invoice_id, None)  # fully paid now
        return _write(path, data)


def record_received(path: str | Path, invoice_id: str, when: date, amount: Decimal, payments=(),
                    snapshot: dict | None = None, rows=()) -> bool:
    if not Decimal(amount).is_finite() or Decimal(amount) < 0:
        raise ValueError(f"received amount {amount!r} must be a finite, non-negative number")
    with _locked(path):
        data = _read(path)
        data["received"][invoice_id] = {"date": when.isoformat(), "amount": str(amount), "payments": list(payments),
                                        "snapshot": snapshot or {}, "payment_rows": list(rows)}
        return _write(path, data)


def apply_confirmations(path: str | Path, ops: list[dict]) -> bool:
    """Several confirmations in ONE locked read-modify-write and ONE atomic write: all of them or none.

    op: {invoice_id, kind: "paid"|"received", when: date, snapshot, keys, rows, amount (received), source}"""
    with _locked(path):
        data = _read(path)
        for op in ops:
            inv = op["invoice_id"]
            if op["kind"] == "paid":
                data["paid"][inv] = {"date": op["when"].isoformat(), "note": "", "source": op.get("source", "confirm"),
                                     "payments": list(op["keys"]), "snapshot": op["snapshot"], "payment_rows": list(op["rows"])}
                data["received"].pop(inv, None)
            else:
                amount = Decimal(op["amount"])
                if not amount.is_finite() or amount < 0:
                    raise ValueError(f"received amount {op['amount']!r} must be a finite, non-negative number")
                data["received"][inv] = {"date": op["when"].isoformat(), "amount": str(amount), "payments": list(op["keys"]),
                                         "snapshot": op["snapshot"], "payment_rows": list(op["rows"])}
        return _write(path, data)


def record_rejected(path: str | Path, invoice_id: str, payment_id: str, row: dict | None = None) -> bool:
    with _locked(path):
        data = _read(path)
        ids = data["rejected"].setdefault(invoice_id, [])
        if payment_id not in ids:
            ids.append(payment_id)
        if row:
            data["rejected_rows"].setdefault(invoice_id, {})[payment_id] = row
        return _write(path, data)


def unrecord_paid(path: str | Path, invoice_id: str) -> bool:
    """Remove a confirmation (paid or part-payment). Returns False if there was none."""
    with _locked(path):
        data = _read(path)
        want = invoice_id.strip().lower()
        hit = False
        for section in ("paid", "received"):
            for k in [k for k in data[section] if str(k).strip().lower() == want]:
                del data[section][k]
                hit = True
        return hit and _write(path, data)


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


def read_ledger(path: str | Path) -> dict:
    """The whole ledger as a dict (validated). Read-only: use the load_* functions for one section."""
    return _read(path)
