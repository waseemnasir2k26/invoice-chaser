"""The evidence table: how strongly does a payment point at an invoice?

The tool NEVER sets an invoice to "paid" on its own (since 1.1.0). This table only ranks evidence:
a payment that passes it becomes a SUGGESTION ("suggested-paid", shown with its reason and the
exact payment row), and only `invoice-chaser confirm` writes "paid" to the ledger. Everything that
does not pass is "needs review". While any suggestion exists, no reminder is drafted for the invoice.

STRONG evidence (a suggestion) comes from exactly two paths.

PATH A: invoice id
    (a) the amount is within the tolerance, in the invoice currency (or via a configured fx rate);
        a lower amount makes it a PARTIAL suggestion, a higher one needs review
    (b) EXACTLY ONE invoice id appears in the payment, as a whole token (see IdIndex). The
        description and the payer are searched separately, never glued together. Ids inside e-mail
        addresses and URLs do not count. An id must look like an id: at least 2 letters and 2 digits
        (INV-12, AB1234), or start with a configured prefix (ref_prefixes, default INV and INVOICE),
        or be 4+ digits behind a configured prefix. "A7" and "B2" never count.
    (c) no refund, reversal, failure or "not received" wording, in many languages (see below)
    (d) it names no other invoice (a paid invoice's id counts too)

PATH B: name (only when the payment names NO invoice)
    the amount is exact, same currency, and the payer IS the client: same words (as a multiset, any
    order) and the same legal-suffix kind (Acme Ltd != Acme Inc != Acme). No other invoice of that
    client (settled ones included) has the same price, and the name is not shared by two different
    clients (different e-mail addresses). No refund/failure wording.

Never a suggestion, whatever else matches: a payment dated outside the invoice window or in the
future, a possible duplicate row.
"""
from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from functools import lru_cache

_PART = re.compile(r"[^\W\d_]+|\d+")
_WORD = re.compile(r"[^\W\d_]+")
_INNER_OK = re.compile(r"[ \t]{0,2}[-_/.#:]?[ \t]{0,2}")
_CONTINUES = set("-_/.#:")  # after an id, these glue on more id (INV-1/2)
_LEAD_GLUE = set("-_/.")  # before an id, these mean it is the tail of something longer (X-INV-1)
_EMAIL_URL = re.compile(r"(?:https?://|ftp://|www\.)\S+|[\w.+%-]+@[\w-]+(?:\.[\w-]+)+", re.I)

# Rules the tests switch off one at a time to prove each test really depends on its rule.
MIN_LETTERS = 2
MIN_DIGITS = 2
MASK_EMAIL_URL = True
SEPARATE_FIELDS = True
FOREIGN_IDS = True

# Word stems (case-folded, matched at the start of a word) that make a payment unusable as evidence.
REFUND_STEMS = (
    "refund", "reembols", "rückerstatt", "rueckerstatt", "rembours", "rimbors", "storn", "terugbetal", "zwrot",
    "возврат", "gutschrift", "estorn", "iade", "استرداد", "واپسی", "revers", "chargeback", "return", "cancel",
    "void", "disput", "avoir", "crédit", "kredit",
)
FAILURE_STEMS = ("fail", "pending", "declin", "reject", "bounc", "nsf", "unpaid", "insufficient", "overdraft")
# Scripts written without spaces are searched as substrings.
CJK_SUBSTRINGS = ("返金", "返品", "退款", "退費", "退货", "退貨", "払い戻し", "払戻", "退还")
# "credit" alone is everywhere in bank text; only these follow-ups make it a credit note.
_CREDIT_NOTE = {"note", "notes", "memo", "nota"}


def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def parts_of(s: str) -> tuple[str, ...]:
    """Runs of letters and runs of digits, case-folded: 'INV-1004' -> ('inv', '1004')."""
    return tuple(m.group().casefold() for m in _PART.finditer(_nfkc(s)))


def id_variants(invoice_id: str, prefixes) -> list[tuple[str, ...]]:
    """The part-sequences that count as a mention of this invoice id. Empty = the id does not
    look like an id and can never be matched by reference."""
    parts = parts_of(invoice_id)
    if not parts:
        return []
    prefs = {p.casefold() for p in (prefixes or ()) if p}
    letters = sum(len(p) for p in parts if p[0].isalpha())
    digits = sum(len(p) for p in parts if p[0].isdigit())
    if letters >= MIN_LETTERS and digits >= MIN_DIGITS:
        return [parts]
    if parts[0][0].isalpha() and parts[0] in prefs and digits >= 1:
        return [parts]  # INV-1 with INV configured
    if letters == 0 and digits >= 4:
        return [pp + parts for pp in (parts_of(p) for p in prefs) if pp]  # 2026001 as "INV 2026001"
    return []


class IdIndex:
    """Finds invoice ids in free text, strictly.

    The text is split into letter runs and digit runs. An id matches when its parts appear in a
    row, separated only by a space and/or one of - _ / . # :, and it is a COMPLETE token:
      * "INV-1/2", "INV-1.5", "INV-1-2", "INV-1A", "XINV-1", "INV-10" do NOT match INV-1;
      * "INV 1 2" does not match INV-1 (a spaced id followed by another number is ambiguous);
      * "INV-1", "inv#1", "INV 1", "(INV-1)", "INV-1." and "INV-1 and INV-2" do match.
    E-mail addresses and URLs are blanked out first.
    """

    def __init__(self, invoice_ids, prefixes=()):
        self.by_parts: dict[tuple[str, ...], list[str]] = {}
        families = {p.casefold() for p in (prefixes or ()) if p}
        for iid in invoice_ids:
            for v in id_variants(iid, prefixes):
                if iid not in self.by_parts.setdefault(v, []):
                    self.by_parts[v].append(iid)
                if v[0][0].isalpha():
                    families.add(v[0])
        self.lengths = sorted({len(v) for v in self.by_parts})
        # id-like tokens of the same family (INV-1005) that are not in the file at all
        self._idlike = (re.compile(r"(?<![^\W_])(?:" + "|".join(sorted(map(re.escape, families), key=len, reverse=True))
                                   + r")[ 	]{0,2}[-_/#:.]?[ 	]{0,2}\d+(?:[-/_.]\d+)*(?![^\W_])", re.I)
                        if families else None)

    def find(self, text: str) -> frozenset:
        return self.analyse(text)[0]

    def analyse(self, text: str, limit: int | None = None) -> tuple[frozenset, frozenset]:
        """(known invoice ids named in the text, id-like tokens of the same family that are NOT in the file).

        limit: stop as soon as MORE than this many known ids are found (the caller only needs to know
        that a payment names "many" invoices, which keeps a payment naming every invoice cheap)."""
        if not text or not (self.by_parts or self._idlike):
            return frozenset(), frozenset()
        t = _nfkc(text)
        if MASK_EMAIL_URL:
            t = _EMAIL_URL.sub(lambda m: " " * len(m.group()), t)
        toks = [(m.group().casefold(), m.start(), m.end()) for m in _PART.finditer(t)]
        found: set[str] = set()
        spans: list[tuple[int, int]] = []
        for L in self.lengths:
            for i in range(len(toks) - L + 1):
                ids = self.by_parts.get(tuple(x[0] for x in toks[i:i + L]))
                if not ids:
                    continue
                seps = [t[toks[j][2]:toks[j + 1][1]] for j in range(i, i + L - 1)]
                if not all(_INNER_OK.fullmatch(s) for s in seps):
                    continue
                if i > 0:  # must not be the tail of a longer token: X-INV-1, 5INV-1
                    before = t[toks[i - 1][2]:toks[i][1]]
                    if before == "" or (len(before) == 1 and before in _LEAD_GLUE):
                        continue
                matched = [x[0] for x in toks[i:i + L]]
                if any(" " in x or "	" in x for x in seps) and any(p.isalpha() and len(p) < 3 for p in matched):
                    continue  # spaced ids need a real prefix like INV
                last = i + L - 1
                if last + 1 < len(toks):
                    after = t[toks[last][2]:toks[last + 1][1]]
                    if after == "" or (len(after) == 1 and after in _CONTINUES):
                        continue  # INV-1A, INV-1/2, INV-1.5, INV-1-2
                    spaced = any(" " in s or "	" in s for s in seps)
                    if spaced and after.strip() == "" and toks[last + 1][0].isdigit():
                        continue  # "INV 1 2"
                found.update(ids)
                if limit is not None and len(found) > limit:
                    return frozenset(found), frozenset()
                spans.append((toks[i][1], toks[last][2]))
        foreign: set[str] = set()
        if self._idlike is not None and FOREIGN_IDS:
            spans.sort()
            starts = [sp[0] for sp in spans]
            for m in self._idlike.finditer(t):
                k = bisect_right(starts, m.start())
                if (k > 0 and spans[k - 1][1] > m.start()) or (k < len(spans) and spans[k][0] < m.end()):
                    continue  # overlaps a known id
                foreign.add(" ".join(parts_of(m.group())).upper())
        return frozenset(found), frozenset(foreign)


def field_texts(description: str, payer: str) -> list[str]:
    """The texts to search for ids and refund wording. The fields are never glued together, so a
    description ending in "INV-" and a payer starting with "1" cannot forge "INV-1"."""
    if SEPARATE_FIELDS:
        return [description or "", payer or ""]
    return [f"{description or ''} {payer or ''}"]


def has_negative_context(text: str) -> bool:
    """Refund, reversal, failure or not-received wording (English plus the common payment languages)."""
    t = _nfkc(text).casefold()
    if not t:
        return False
    if any(s in t for s in CJK_SUBSTRINGS):
        return True
    words = [m.group() for m in _WORD.finditer(t)]
    stems = tuple(REFUND_STEMS) + tuple(FAILURE_STEMS)
    for i, w in enumerate(words):
        if stems and w.startswith(stems):
            return True
        if w == "credit" and i + 1 < len(words) and words[i + 1] in _CREDIT_NOTE:
            return True
        if w == "not" and "received" in words[i + 1:i + 3]:
            return True
    return False


# ---------------------------------------------------------------- company names

SUFFIX_CLASS = {
    "ltd": "ltd", "limited": "ltd", "inc": "inc", "incorporated": "inc", "llc": "llc", "pllc": "pllc",
    "llp": "llp", "lp": "lp", "plc": "plc", "gmbh": "gmbh", "ag": "ag", "pvt": "pvt", "private": "pvt",
    "pty": "pty", "sa": "sa", "bv": "bv", "nv": "nv", "srl": "srl", "spa": "spa", "ab": "ab", "oy": "oy",
    "sdn": "sdn", "bhd": "bhd", "corp": "corp", "corporation": "corp", "co": "co", "company": "co",
}

_WORD_OR_DIGIT = re.compile(r"[^\W_]+")


@lru_cache(maxsize=None)
def company_key(name: str):
    """(sorted words, frozenset of legal-suffix classes), or None if the name has no usable words.
    Words are compared as a multiset ("Alpha Alpha" != "Alpha"), order-insensitively. Different
    suffix classes are different companies (Acme Ltd != Acme Inc), and so is a missing suffix."""
    s = _nfkc(name).casefold().replace("&", " and ")
    s = re.sub(r"[.'’]", "", s)  # L.L.C. -> llc, O'Brien -> obrien
    words = _WORD_OR_DIGIT.findall(s)
    if words and words[0] == "the" and len(words) > 1:
        words = words[1:]
    classes = set()
    while len(words) > 1 and words[-1] in SUFFIX_CLASS:
        classes.add(SUFFIX_CLASS[words.pop()])
    if not words:
        return None
    return tuple(sorted(words)), frozenset(classes)


def same_company(client: str, payer: str) -> bool:
    a = company_key(client)
    return a is not None and a == company_key(payer)
