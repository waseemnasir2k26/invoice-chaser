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
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .decision import company_key
from .models import Invoice, Payment
from .notices import Notice


class ParseError(ValueError):
    pass


@dataclass
class ParseOptions:
    date_order: str | None = None  # dmy | mdy | ymd | None (auto: ISO, then day-first, then month-first)
    default_currency: str = ""  # what a bare "$" means
    strict: bool = False  # True: a bad row aborts the run instead of being skipped with a warning
    decimal_comma: bool | None = None  # None: refuse ambiguous amounts like 1,234 / 1.500
    warnings: list[Notice] = field(default_factory=list)
    ignored: list[Payment] = field(default_factory=list)  # negative rows: refunds, fees, withdrawals
    # stable payment ids: see _Ids / finalize_ids
    pending: list = field(default_factory=list)
    case_dup_ids: set = field(default_factory=set)  # lower-case invoice ids that appear twice with different capitals
    files_parsed: int = 0
    _cross: set = field(default_factory=set)

    def warn(self, msg: str, code: str = "NOTE") -> None:
        if msg not in self.warnings:
            self.warnings.append(Notice(msg, code))

    def cross_file_ids(self) -> set:
        """Ids of payments whose exact row (same payer too) appears in more than one file of this run."""
        return set(self._cross)

    def finalize_ids(self) -> None:
        """(Re)assign the ids of every payment without a real transaction id, across ALL files parsed so far.

        id = hash(source, date, amount, currency, normalised description, the payer as a company) + an occurrence index
        that only counts rows with exactly the same full key. So the id never depends on how many OTHER rows exist: a lone
        row keeps its id when a same-day sibling from someone else appears. Punctuation, capitals and spacing are ignored,
        and a payer renamed within the same company (ACME LTD -> Acme Limited) keeps the id. Rows identical in every field
        stay two payments (index 1, 2); the same row in two exports is flagged (one real payment or two?)."""
        groups: dict[str, list] = {}
        for rec in self.pending:
            groups.setdefault(rec["key"], []).append(rec)
        cross: set[str] = set()
        for key, recs in groups.items():
            for k, rec in enumerate(recs, 1):
                rec["payment"].id = "h-" + hashlib.sha256(f"{key}#{k}".encode()).hexdigest()[:12]
            if len({rec["file"] for rec in recs}) > 1:
                cross.update(rec["payment"].id for rec in recs)
        self._cross = cross


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
                  f"Check the dates, or pass --date-format dmy|mdy|ymd.", "DATE_AMBIGUOUS")


# ---------------------------------------------------------------- amounts and currency

_CURRENCY = r"(?:[A-Za-z]{3}|[€£$¥₹])"
_AMOUNT = re.compile(
    r"^(?P<s1>[+-])?\s*(?:" + _CURRENCY + r"\s*)?(?P<s2>[+-])?\s*(?P<num>[\d.,'   ’]*\d[\d.,'   ’]*|[.,]\d+)"
    r"(?:\s+" + _CURRENCY + r")?\s*$")
_GROUP_CHARS = "   '’"
_MAX_AMOUNT = Decimal("1e12")


def parse_amount(s: str, decimal_comma: bool | None = None) -> Decimal:
    """Strict amount grammar: optional sign or (parentheses), optional currency symbol/code at either
    end, then digits with ONE consistent thousands separator (comma, dot, space or apostrophe, always
    groups of three) and at most one decimal separator. Anything else is a ParseError, never a guess.

    decimal_comma: None reads only unambiguous forms (12.50, 12,50, 1,234.56, 1.234,56); "1,234" and
    "1.500" are refused because they could be 1234 or 1.234. True means 1.234,56 style, False 1,234.56 style.
    """
    raw = s
    t = (s or "").strip().replace("−", "-")
    if not t:
        raise ParseError("empty amount")
    hint = " (set decimal_comma in the config, or pass --decimal-comma)"
    neg = False
    m = re.fullmatch(r"\((.*)\)", t)
    if m:
        neg, t = True, m.group(1).strip()
    m = _AMOUNT.match(t) if len(t) <= 40 else None
    if not m:
        raise ParseError(f"unrecognised amount {raw!r}: expected digits like 1234.56 or 1,234.56 with an optional sign and currency")
    if m.group("s1") == "-" or m.group("s2") == "-":
        neg = True
    num = m.group("num").strip()
    chars = set(num) & {".", ","}
    spaces = set(num) & set(_GROUP_CHARS)
    if len(spaces) > 1:
        raise ParseError(f"unrecognised amount {raw!r}: mixed thousands separators")
    dec = grp = None
    if spaces:
        grp = next(iter(spaces))
    if len(chars) == 2:
        dec = "." if num.rfind(".") > num.rfind(",") else ","
        if grp:
            raise ParseError(f"unrecognised amount {raw!r}: mixed thousands separators")
        grp = "," if dec == "." else "."
        if (decimal_comma is True and dec != ",") or (decimal_comma is False and dec != "."):
            raise ParseError(f"amount {raw!r} contradicts the decimal_comma setting")
    elif len(chars) == 1:
        c = next(iter(chars))
        before, _, after = num.partition(c) if num.count(c) == 1 else ("", "", "")
        if num.count(c) > 1:
            if grp:
                raise ParseError(f"unrecognised amount {raw!r}: mixed thousands separators")
            grp = c
            if (decimal_comma is True and c != ".") or (decimal_comma is False and c != ","):
                raise ParseError(f"amount {raw!r} contradicts the decimal_comma setting")
        elif decimal_comma is not None:
            if (c == ",") == decimal_comma:
                dec = c
            elif grp:
                raise ParseError(f"unrecognised amount {raw!r}: mixed thousands separators")
            else:
                grp = c
        elif grp:
            dec = c  # "1 234,5": the space groups, so the lone , or . is the decimal mark
        else:
            before_d = before.replace(" ", "")
            if len(after) == 3 and 1 <= len(before_d) <= 3 and not before_d.startswith("0"):
                raise ParseError(f"ambiguous amount {raw!r}: could be {before_d}{after} or {before_d}.{after}" + hint)
            dec = c
    int_part, frac = (num.rsplit(dec, 1) if dec else (num, ""))
    if dec and not re.fullmatch(r"\d+", frac):
        raise ParseError(f"unrecognised amount {raw!r}")
    if grp:
        if not re.fullmatch(r"\d{1,3}(?:" + re.escape(grp) + r"\d{3})+", int_part):
            raise ParseError(f"unrecognised amount {raw!r}: thousands separators must group digits in threes" + hint)
        if int_part.startswith("0"):
            raise ParseError(f"unrecognised amount {raw!r}: a thousands group cannot start with 0" + hint)
        int_part = int_part.replace(grp, "")
    elif not re.fullmatch(r"\d*", int_part):
        raise ParseError(f"unrecognised amount {raw!r}")
    if int_part == "" and frac == "":
        raise ParseError(f"unrecognised amount {raw!r}")
    try:
        v = Decimal((int_part or "0") + ("." + frac if frac else ""))
    except InvalidOperation as e:
        raise ParseError(f"unrecognised amount {raw!r}") from e
    if v >= _MAX_AMOUNT:
        raise ParseError(f"implausibly large amount {raw!r}")
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
                  f"Those rows will not match anything.", "CURRENCY_UNKNOWN")
        return "$"
    letters = re.sub(r"[^A-Za-z]", "", s).upper()
    if len(letters) == 3:
        return letters
    opts.warn(f"{where}: currency {raw!r} is not an ISO code (like USD, EUR, GBP); rows with it will not match anything.", "CURRENCY_UNKNOWN")
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


def _bad_row(opts: ParseOptions, path, n: int, err: Exception, invoice: bool = False, ident: str = "") -> None:
    """A row that cannot be used. A dropped INVOICE is critical (it will not be matched, chased or shown), named by line and id;
    a dropped payment row is a warning (a payment may be missing)."""
    if opts.strict:
        raise ParseError(f"{path} line {n}: {err}") from err
    name = Path(path).name
    if invoice:
        opts.warn(f"{name} line {n}" + (f" (invoice id {ident!r})" if ident else "") + f": {err}. This invoice was DROPPED: it is not matched, "
                  f"not chased and not in the report. Fix the row and run again.", "INVOICE_ROW_DROPPED")
    else:
        opts.warn(f"{name} line {n}: {err}. This payment row was skipped, so a payment may be missing from the matching.", "PAYMENT_ROW_DROPPED")


# ---------------------------------------------------------------- invoices

def read_invoices(path: str | Path, opts: ParseOptions | None = None) -> list[Invoice]:
    opts = opts or ParseOptions()
    try:
        fns, rows = _open_rows(path)
    except ParseError as e:
        if "file is empty" in str(e):
            raise ParseError(f"no invoices found in {Path(path).name}: the file is empty") from e
        raise
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
    out, seen, seen_norm = [], set(), {}
    for n, r in enumerate(rows, start=2):
        if not any((v or "").strip() for v in r.values() if isinstance(v, str)):
            continue
        try:
            inv = Invoice(
                id=_get(r, c_id),
                client=_get(r, c_client),
                email=_get(r, c_email),
                amount=parse_amount(_get(r, c_amt), opts.decimal_comma),
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
            _bad_row(opts, path, n, e, invoice=True, ident=_get(r, c_id))
            continue
        norm_id = " ".join(inv.id.casefold().split())
        if norm_id in seen_norm:  # INV-6001 and inv-6001: never merge them silently, and never guess which one is meant
            opts.case_dup_ids.add(norm_id)
            opts.warn(f"{Path(path).name} row {n}: duplicate invoice id (case differs): {seen_norm[norm_id]!r} and {inv.id!r}. Both are held in "
                      f"needs review and no reminder is drafted for either. Next step: give each invoice its own id in the file, then run again.",
                      "DUPLICATE_INVOICE_ID")
        else:
            seen_norm[norm_id] = inv.id
        seen.add(inv.id)
        out.append(inv)
    return out


# ---------------------------------------------------------------- payments

_OK_STATUS = {"", "completed", "complete", "processed", "approved", "paid", "success", "successful", "received", "cleared"}


def _join(*parts: str) -> str:
    return " | ".join(p.strip() for p in parts if p and p.strip())  # a hard boundary: fields never glue into one id


def _finish(path, rows_seen: int, out: list[Payment], opts: ParseOptions, skipped_status: int = 0) -> list[Payment]:
    opts.finalize_ids()
    neg = [p for p in opts.ignored if p.source == Path(path).name]
    if neg:
        opts.warn(f"{Path(path).name}: {len(neg)} negative row(s) ignored (refunds, fees, withdrawals); they are NOT netted "
                  f"against invoices. Listed in review.html.", "NEGATIVE_ROWS")
    if skipped_status:
        opts.warn(f"{Path(path).name}: {skipped_status} row(s) skipped because their status is not completed (pending, failed...).", "ROWS_SKIPPED_STATUS")
    return out


class _Ids:
    """Builds the payments of ONE file. A real transaction id is kept as is; otherwise the id comes from
    ParseOptions.finalize_ids (content hash, never a row number)."""

    def __init__(self, opts: ParseOptions):
        self.opts = opts
        opts.files_parsed += 1
        self.file_no = opts.files_parsed

    def payment(self, source: str, real_id: str, d: date, amount: Decimal, currency: str, description: str, payer: str) -> Payment:
        pay = Payment(real_id or "h-pending", d, amount, currency, description, payer, source)
        pay.file_no = self.file_no
        if not real_id:
            clean = lambda t: " ".join(re.sub(r"[\W_]+", " ", (t or "").casefold()).split())
            ck = company_key(payer) if payer else None
            who = "" if ck is None else " ".join(ck[0]) + "/" + ",".join(sorted(ck[1]))
            key = "|".join([source, d.isoformat(), format(amount.normalize(), "f"), currency, clean(description), who])
            self.opts.pending.append({"payment": pay, "key": key, "file": self.file_no})
        return pay


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
    ids = _Ids(opts)
    for n, r in enumerate(rows, start=2):
        if not _get(r, c_amt):
            continue
        try:
            amt = parse_amount(_get(r, c_amt))  # Wise statements always use a decimal point
            d = parse_date(_get(r, c_date), order=opts.date_order)
            cur = norm_currency(_get(r, c_cur), opts, Path(path).name)
        except ParseError as e:
            _bad_row(opts, path, n, e)
            continue
        desc = _join(_get(r, c_desc), _get(r, c_ref), _get(r, c_note))
        if amt < 0:
            _ignored(opts, Path(path).name, _get(r, c_id) or "ignored", d, amt, cur, desc, _get(r, c_payer))
            continue
        if amt == 0:
            continue
        out.append(ids.payment("wise", _get(r, c_id), d, amt, cur, desc, _get(r, c_payer)))
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
    ids = _Ids(opts)
    for n, r in enumerate(rows, start=2):
        if not _get(r, c_amt):
            continue
        if _get(r, c_stat).lower() not in _OK_STATUS:
            skipped += 1
            continue
        try:
            amt = parse_amount(_get(r, c_amt), opts.decimal_comma)
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
            _ignored(opts, Path(path).name, _get(r, c_id) or "ignored", d, amt, cur, desc, payer)
            continue
        if amt == 0:
            continue
        out.append(ids.payment("payoneer", _get(r, c_id), d, amt, cur, desc, payer))
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
    ids = _Ids(opts)
    for n, r in enumerate(rows, start=2):
        if not _get(r, c_amt):
            continue
        try:
            amt = parse_amount(_get(r, c_amt), opts.decimal_comma)
            d = parse_date(_get(r, c_date), fmt, opts.date_order)
            cur = norm_currency(_get(r, c_cur), opts, Path(path).name) if c_cur and _get(r, c_cur) else default_cur
        except ParseError as e:
            _bad_row(opts, path, n, e)
            continue
        if amt < 0:
            _ignored(opts, Path(path).name, _get(r, c_id) or "ignored", d, amt, cur, _get(r, c_desc), _get(r, c_payer))
            continue
        if amt == 0:
            continue
        out.append(ids.payment("bank", _get(r, c_id), d, amt, cur, _get(r, c_desc), _get(r, c_payer)))
    return _finish(path, len(rows), out, opts)
