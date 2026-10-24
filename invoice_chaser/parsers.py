"""CSV parsers with tolerant header matching.

Header names are normalised (lower-case, letters and digits only) and matched
against alias lists, so "Transaction Date", "transaction_date" and
"TRANSACTION DATE" are all the same column.
"""
from __future__ import annotations

import csv
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .models import Invoice, Payment


class ParseError(ValueError):
    pass


def norm(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


DATE_FORMATS = [
    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y",
    "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%Y/%m/%d",
]


def parse_date(s: str, fmt: str | None = None) -> date:
    s = (s or "").strip()
    if not s:
        raise ParseError("empty date")
    if fmt:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError as e:
            raise ParseError(f"date {s!r} does not match {fmt!r}") from e
    candidates = [s]
    for sep in ("T", " "):
        if sep in s:
            candidates.append(s.split(sep)[0])
    candidates.append(re.sub(r"\s+\d{1,2}:\d{2}.*$", "", s))
    for c in candidates:
        for f in DATE_FORMATS:
            try:
                return datetime.strptime(c, f).date()
            except ValueError:
                continue
    raise ParseError(f"unrecognised date {s!r}")


def parse_amount(s: str) -> Decimal:
    t = (s or "").strip()
    if not t:
        raise ParseError("empty amount")
    neg = t.startswith("(") and t.endswith(")")
    t = re.sub(r"[^\d.,\-]", "", t)
    if t.startswith("-"):
        neg, t = True, t[1:]
    t = t.replace("-", "")
    if "," in t and "." in t:
        if t.rfind(",") > t.rfind("."):  # 1.234,56
            t = t.replace(".", "").replace(",", ".")
        else:
            t = t.replace(",", "")
    elif "," in t:
        # 1,234 (thousands) vs 12,50 (decimal comma)
        if re.search(r",\d{2}$", t) and t.count(",") == 1:
            t = t.replace(",", ".")
        else:
            t = t.replace(",", "")
    try:
        v = Decimal(t)
    except InvalidOperation as e:
        raise ParseError(f"unrecognised amount {s!r}") from e
    return -v if neg else v


def _open_rows(path: str | Path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        text = fh.read()
    if not text.strip():
        raise ParseError(f"{path}: file is empty")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    return reader.fieldnames or [], list(reader)


def _colmap(fieldnames: list[str]) -> dict[str, str]:
    return {norm(f): f for f in fieldnames if f}


def _pick(cm: dict[str, str], aliases: list[str]) -> str | None:
    for a in aliases:
        if a in cm:
            return cm[a]
    return None


def _need(cm, aliases, what, path, fieldnames) -> str:
    col = _pick(cm, aliases)
    if not col:
        raise ParseError(f"{path}: no column for {what} (tried {aliases}); found headers: {fieldnames}")
    return col


def _get(r: dict, col: str | None) -> str:
    return (r.get(col) or "").strip() if col else ""


# ---------------------------------------------------------------- invoices

def read_invoices(path: str | Path) -> list[Invoice]:
    fns, rows = _open_rows(path)
    cm = _colmap(fns)
    c_id = _need(cm, ["id", "invoiceid", "invoice", "invoicenumber", "invoiceno", "number"], "invoice id", path, fns)
    c_client = _need(cm, ["client", "clientname", "customer", "customername"], "client", path, fns)
    c_amt = _need(cm, ["amount", "total", "amountdue"], "amount", path, fns)
    c_cur = _need(cm, ["currency", "cur"], "currency", path, fns)
    c_iss = _need(cm, ["issued", "issuedate", "invoicedate", "date"], "issued date", path, fns)
    c_due = _need(cm, ["due", "duedate"], "due date", path, fns)
    c_email = _pick(cm, ["email", "clientemail", "contactemail"])
    c_stat = _pick(cm, ["status"])
    c_contact = _pick(cm, ["contact", "contactname", "firstname"])
    out, seen = [], set()
    for n, r in enumerate(rows, start=2):
        if not any((v or "").strip() for v in r.values() if isinstance(v, str)):
            continue
        try:
            inv = Invoice(
                id=_get(r, c_id),
                client=_get(r, c_client),
                email=_get(r, c_email),
                amount=parse_amount(_get(r, c_amt)),
                currency=_get(r, c_cur).upper(),
                issued=parse_date(_get(r, c_iss)),
                due=parse_date(_get(r, c_due)),
                status=_get(r, c_stat).lower(),
                contact=_get(r, c_contact),
            )
        except ParseError as e:
            raise ParseError(f"{path} row {n}: {e}") from e
        if not inv.id:
            raise ParseError(f"{path} row {n}: empty invoice id")
        if inv.id in seen:
            raise ParseError(f"{path} row {n}: duplicate invoice id {inv.id!r}")
        if inv.amount <= 0:
            raise ParseError(f"{path} row {n}: amount must be positive")
        seen.add(inv.id)
        out.append(inv)
    return out


# ---------------------------------------------------------------- payments

_OK_STATUS = {"", "completed", "complete", "processed", "approved", "paid", "success", "successful", "received", "cleared"}


def _join(*parts: str) -> str:
    return " ".join(p.strip() for p in parts if p and p.strip())


def parse_wise(path: str | Path) -> list[Payment]:
    """Wise statement CSV. Incoming = positive Amount."""
    fns, rows = _open_rows(path)
    cm = _colmap(fns)
    c_date = _need(cm, ["date", "datetime"], "date", path, fns)
    c_amt = _need(cm, ["amount"], "amount", path, fns)
    c_cur = _need(cm, ["currency"], "currency", path, fns)
    c_id = _pick(cm, ["transferwiseid", "id", "transferid"])
    c_desc = _pick(cm, ["description"])
    c_ref = _pick(cm, ["paymentreference", "reference"])
    c_note = _pick(cm, ["note"])
    c_payer = _pick(cm, ["payername", "sendername"])
    out = []
    for n, r in enumerate(rows, start=2):
        if not _get(r, c_amt):
            continue
        try:
            amt = parse_amount(_get(r, c_amt))
            d = parse_date(_get(r, c_date))
        except ParseError as e:
            raise ParseError(f"{path} row {n}: {e}") from e
        if amt <= 0:
            continue
        out.append(Payment(
            id=_get(r, c_id) or f"wise-{n}",
            date=d, amount=amt, currency=_get(r, c_cur).upper(),
            description=_join(_get(r, c_desc), _get(r, c_ref), _get(r, c_note)),
            payer=_get(r, c_payer), source="wise",
        ))
    return out


def parse_payoneer(path: str | Path) -> list[Payment]:
    """Payoneer transactions CSV. Header names are unverified; matching is alias-based."""
    fns, rows = _open_rows(path)
    cm = _colmap(fns)
    c_date = _need(cm, ["transactiondate", "date", "completedat", "completiondate"], "date", path, fns)
    c_amt = _need(cm, ["amount", "creditamount"], "amount", path, fns)
    c_cur = _need(cm, ["currency", "amountcurrency"], "currency", path, fns)
    c_desc = _pick(cm, ["description", "memo", "details", "title"])
    c_stat = _pick(cm, ["status"])
    c_id = _pick(cm, ["transactionid", "id", "reference", "paymentid"])
    c_payer = _pick(cm, ["payername", "sender", "from", "payer"])
    out = []
    for n, r in enumerate(rows, start=2):
        if not _get(r, c_amt):
            continue
        if _get(r, c_stat).lower() not in _OK_STATUS:
            continue
        try:
            amt = parse_amount(_get(r, c_amt))
            d = parse_date(_get(r, c_date))
        except ParseError as e:
            raise ParseError(f"{path} row {n}: {e}") from e
        if amt <= 0:
            continue
        desc = _get(r, c_desc)
        payer = _get(r, c_payer)
        if not payer:
            m = re.match(r"(?i)\s*payment\s+from\s+(.+?)(?:\s+(?:inv|invoice|ref)\b.*)?$", desc)
            payer = m.group(1).strip() if m else ""
        out.append(Payment(
            id=_get(r, c_id) or f"payoneer-{n}",
            date=d, amount=amt, currency=_get(r, c_cur).upper(),
            description=desc, payer=payer, source="payoneer",
        ))
    return out


def parse_generic(path: str | Path, mapping: dict[str, str]) -> list[Payment]:
    """Any bank CSV. mapping keys: date, amount | credit, currency, description, payer, id,
    date_format (strptime), default_currency. Values are the column headers in your file."""
    fns, rows = _open_rows(path)
    cm = _colmap(fns)

    def col(key):
        name = mapping.get(key)
        if not name:
            return None
        c = cm.get(norm(name))
        if not c:
            raise ParseError(f"{path}: mapped column {name!r} for {key!r} not found; headers: {fns}")
        return c

    c_date = col("date")
    c_amt = col("credit") or col("amount")
    if not c_date or not c_amt:
        raise ParseError("generic mapping needs 'date' and ('amount' or 'credit')")
    c_cur, c_desc, c_payer, c_id = col("currency"), col("description"), col("payer"), col("id")
    fmt = mapping.get("date_format")
    default_cur = (mapping.get("default_currency") or "").upper()
    if not c_cur and not default_cur:
        raise ParseError("generic mapping needs 'currency' column or 'default_currency'")
    out = []
    for n, r in enumerate(rows, start=2):
        if not _get(r, c_amt):
            continue
        try:
            amt = parse_amount(_get(r, c_amt))
            d = parse_date(_get(r, c_date), fmt)
        except ParseError as e:
            raise ParseError(f"{path} row {n}: {e}") from e
        if amt <= 0:
            continue
        out.append(Payment(
            id=_get(r, c_id) or f"bank-{n}",
            date=d, amount=amt, currency=(_get(r, c_cur).upper() if c_cur else "") or default_cur,
            description=_get(r, c_desc), payer=_get(r, c_payer), source="bank",
        ))
    return out
