"""CSV parsers with tolerant header matching.

Header names are normalised (lower-case, letters and digits only) and matched
against alias lists, so "Transaction Date", "transaction_date" and
"TRANSACTION DATE" are all the same column.

Nothing is dropped silently: ignored rows (refunds, outgoing money, pending rows,
bad rows) are counted in ParseOptions.warnings / .ignored and shown in review.html.
"""
from __future__ import annotations

import codecs
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .models import Invoice, Payment


class ParseError(ValueError):
    pass


@dataclass
class ParseOptions:
    date_order: str | None = None  # dmy | mdy | ymd | None (auto: ISO, then day-first, then month-first)
    default_currency: str = ""  # what a bare "$" means
    strict: bool = False  # True: a bad row aborts the run instead of being skipped with a warning
    warnings: list[str] = field(default_factory=list)
    ignored: list[Payment] = field(default_factory=list)  # negative rows: refunds, fees, withdrawals

    def warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)


def norm(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


# ---------------------------------------------------------------- dates

_NAMED = ["%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y"]
_ISO = ["%Y-%m-%d", "%Y/%m/%d"]
_ORDERS = {
    "dmy": ["%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"],
    "mdy": ["%m-%d-%Y", "%m/%d/%Y", "%m.%d.%Y"],
    "ymd": ["%Y.%m.%d"],
}
DATE_FORMATS = _ISO + _ORDERS["dmy"] + _ORDERS["mdy"] + _NAMED  # auto mode: day-first wins when ambiguous


def parse_date(s: str, fmt: str | None = None, order: str | None = None) -> date:
    s = (s or "").strip()
    if not s:
        raise ParseError("empty date")
    if fmt:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError as e:
            raise ParseError(f"date {s!r} does not match {fmt!r}") from e
    if order is not None and order not in _ORDERS:
        raise ParseError(f"date order must be dmy, mdy or ymd, got {order!r}")
    formats = DATE_FORMATS if order is None else _ISO + _ORDERS[order] + _NAMED
    candidates = [s]
    for sep in ("T", " "):
        if sep in s:
            candidates.append(s.split(sep)[0])
    candidates.append(re.sub(r"\s+\d{1,2}:\d{2}.*$", "", s))
    for c in candidates:
        for f in formats:
            try:
                return datetime.strptime(c, f).date()
            except ValueError:
                continue
    raise ParseError(f"unrecognised date {s!r}" + (f" (date order {order})" if order else ""))


_NUMERIC_DATE = re.compile(r"^\s*(\d{1,2})[-/.](\d{1,2})[-/.]\d{4}")


def note_date_ambiguity(path, raws: list[str], opts: ParseOptions) -> None:
    """Warn when no row in the file can tell day-first from month-first."""
    if opts.date_order is not None:
        return
    ambiguous = decisive = 0
    for raw in raws:
        m = _NUMERIC_DATE.match(raw or "")
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12 or b > 12:
            decisive += 1
        elif a != b:
            ambiguous += 1
    if ambiguous and not decisive:
        opts.warn(f"{Path(path).name}: every dated row could be day-first or month-first; read as day-first. "
                  f"Check the dates, or pass --date-format dmy|mdy|ymd.")


# ---------------------------------------------------------------- amounts and currency

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


_SYMBOLS = {"€": "EUR", "£": "GBP", "US$": "USD", "USD$": "USD"}


def norm_currency(raw: str, opts: ParseOptions, where: str) -> str:
    """ISO code from what the file says. "$" is only read as a currency when default_currency is set."""
    s = (raw or "").strip()
    u = s.upper()
    if re.fullmatch(r"[A-Z]{3}", u):
        return u
    if s in _SYMBOLS:
        return _SYMBOLS[s]
    if s == "$":
        if opts.default_currency:
            return opts.default_currency
        opts.warn(f"{where}: currency '$' is ambiguous (USD, AUD, CAD...); set default_currency in the config. "
                  f"Those rows will not match anything.")
        return "$"
    letters = re.sub(r"[^A-Za-z]", "", s).upper()
    if len(letters) == 3:
        return letters
    opts.warn(f"{where}: currency {raw!r} is not an ISO code (like USD, EUR, GBP); rows with it will not match anything.")
    return u or "?"


# ---------------------------------------------------------------- file reading

def _decode(raw: bytes) -> str:
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16")
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig")
    head = raw[:200]
    if len(head) >= 4 and head.count(b"\x00") >= len(head) // 3:  # BOM-less UTF-16 (Excel "Unicode text")
        return raw.decode("utf-16-le" if head[1:2] == b"\x00" else "utf-16-be")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _open_rows(path: str | Path):
    text = _decode(Path(path).read_bytes())
    if not text.strip():
        raise ParseError(f"{path}: file is empty")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text, newline=""), dialect=dialect)
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


def _bad_row(opts: ParseOptions, path, n: int, err: Exception) -> None:
    if opts.strict:
        raise ParseError(f"{path} row {n}: {err}") from err
    opts.warn(f"{Path(path).name} row {n}: {err} (row skipped)")


# ---------------------------------------------------------------- invoices

def read_invoices(path: str | Path, opts: ParseOptions | None = None) -> list[Invoice]:
    opts = opts or ParseOptions()
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
    c_last = _pick(cm, ["lastreminder", "lastreminderdate", "lastremindersent", "reminded"])
    note_date_ambiguity(path, [_get(r, c_iss) for r in rows] + [_get(r, c_due) for r in rows], opts)
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
                currency=norm_currency(_get(r, c_cur), opts, Path(path).name),
                issued=parse_date(_get(r, c_iss), order=opts.date_order),
                due=parse_date(_get(r, c_due), order=opts.date_order),
                status=_get(r, c_stat).lower(),
                contact=_get(r, c_contact),
                last_reminder=parse_date(_get(r, c_last), order=opts.date_order) if _get(r, c_last) else None,
            )
            if not inv.id:
                raise ParseError("empty invoice id")
            if inv.id in seen:
                raise ParseError(f"duplicate invoice id {inv.id!r}")
            if inv.amount <= 0:
                raise ParseError("amount must be positive")
        except ParseError as e:
            _bad_row(opts, path, n, e)
            continue
        seen.add(inv.id)
        out.append(inv)
    return out


# ---------------------------------------------------------------- payments

_OK_STATUS = {"", "completed", "complete", "processed", "approved", "paid", "success", "successful", "received", "cleared"}


def _join(*parts: str) -> str:
    return " ".join(p.strip() for p in parts if p and p.strip())


def _finish(path, rows_seen: int, out: list[Payment], opts: ParseOptions, skipped_status: int = 0) -> list[Payment]:
    neg = [p for p in opts.ignored if p.source == str(path)]
    if neg:
        opts.warn(f"{Path(path).name}: {len(neg)} negative row(s) ignored (refunds, fees, withdrawals); they are NOT netted "
                  f"against invoices. Listed in review.html.")
    if skipped_status:
        opts.warn(f"{Path(path).name}: {skipped_status} row(s) skipped because their status is not completed (pending, failed...).")
    return out


def _ignored(opts: ParseOptions, source: str, id_: str, d: date, amt: Decimal, cur: str, desc: str, payer: str) -> None:
    opts.ignored.append(Payment(id_, d, amt, cur, desc, payer, source))


def parse_wise(path: str | Path, opts: ParseOptions | None = None) -> list[Payment]:
    """Wise statement CSV. Incoming = positive Amount."""
    opts = opts or ParseOptions()
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
    note_date_ambiguity(path, [_get(r, c_date) for r in rows], opts)
    out = []
    for n, r in enumerate(rows, start=2):
        if not _get(r, c_amt):
            continue
        try:
            amt = parse_amount(_get(r, c_amt))
            d = parse_date(_get(r, c_date), order=opts.date_order)
            cur = norm_currency(_get(r, c_cur), opts, Path(path).name)
        except ParseError as e:
            _bad_row(opts, path, n, e)
            continue
        desc = _join(_get(r, c_desc), _get(r, c_ref), _get(r, c_note))
        if amt < 0:
            _ignored(opts, str(path), _get(r, c_id) or f"wise-{n}", d, amt, cur, desc, _get(r, c_payer))
            continue
        if amt == 0:
            continue
        out.append(Payment(_get(r, c_id) or f"wise-{n}", d, amt, cur, desc, _get(r, c_payer), "wise"))
    return _finish(path, len(rows), out, opts)


def parse_payoneer(path: str | Path, opts: ParseOptions | None = None) -> list[Payment]:
    """Payoneer transactions CSV. Header names are unverified; matching is alias-based."""
    opts = opts or ParseOptions()
    fns, rows = _open_rows(path)
    cm = _colmap(fns)
    c_date = _need(cm, ["transactiondate", "date", "completedat", "completiondate"], "date", path, fns)
    c_amt = _need(cm, ["amount", "creditamount"], "amount", path, fns)
    c_cur = _need(cm, ["currency", "amountcurrency"], "currency", path, fns)
    c_desc = _pick(cm, ["description", "memo", "details", "title"])
    c_stat = _pick(cm, ["status"])
    c_id = _pick(cm, ["transactionid", "id", "reference", "paymentid"])
    c_payer = _pick(cm, ["payername", "sender", "from", "payer"])
    note_date_ambiguity(path, [_get(r, c_date) for r in rows], opts)
    out, skipped = [], 0
    for n, r in enumerate(rows, start=2):
        if not _get(r, c_amt):
            continue
        if _get(r, c_stat).lower() not in _OK_STATUS:
            skipped += 1
            continue
        try:
            amt = parse_amount(_get(r, c_amt))
            d = parse_date(_get(r, c_date), order=opts.date_order)
            cur = norm_currency(_get(r, c_cur), opts, Path(path).name)
        except ParseError as e:
            _bad_row(opts, path, n, e)
            continue
        desc = _get(r, c_desc)
        payer = _get(r, c_payer)
        if not payer:
            m = re.match(r"(?i)\s*payment\s+from\s+(.+?)(?:\s+(?:inv|invoice|ref)\b.*)?$", desc)
            payer = m.group(1).strip() if m else ""
        if amt < 0:
            _ignored(opts, str(path), _get(r, c_id) or f"payoneer-{n}", d, amt, cur, desc, payer)
            continue
        if amt == 0:
            continue
        out.append(Payment(_get(r, c_id) or f"payoneer-{n}", d, amt, cur, desc, payer, "payoneer"))
    return _finish(path, len(rows), out, opts, skipped)


def parse_generic(path: str | Path, mapping: dict[str, str], opts: ParseOptions | None = None) -> list[Payment]:
    """Any bank CSV. mapping keys: date, amount | credit, currency, description, payer, id,
    date_format (strptime), default_currency. Values are the column headers in your file."""
    opts = opts or ParseOptions()
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
    default_cur = (mapping.get("default_currency") or opts.default_currency or "").upper()
    if not c_cur and not default_cur:
        raise ParseError("generic mapping needs 'currency' column or 'default_currency'")
    if not fmt:
        note_date_ambiguity(path, [_get(r, c_date) for r in rows], opts)
    out = []
    for n, r in enumerate(rows, start=2):
        if not _get(r, c_amt):
            continue
        try:
            amt = parse_amount(_get(r, c_amt))
            d = parse_date(_get(r, c_date), fmt, opts.date_order)
            cur = norm_currency(_get(r, c_cur), opts, Path(path).name) if c_cur and _get(r, c_cur) else default_cur
        except ParseError as e:
            _bad_row(opts, path, n, e)
            continue
        if amt < 0:
            _ignored(opts, str(path), _get(r, c_id) or f"bank-{n}", d, amt, cur, _get(r, c_desc), _get(r, c_payer))
            continue
        if amt == 0:
            continue
        out.append(Payment(_get(r, c_id) or f"bank-{n}", d, amt, cur, _get(r, c_desc), _get(r, c_payer), "bank"))
    return _finish(path, len(rows), out, opts)
