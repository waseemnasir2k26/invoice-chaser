"""The "may this payment be counted as paid?" decision, in one place.

A false "paid" is the worst failure this tool can have (the client is never chased), so the
rule is a table of conditions, not a score. A payment is AUTO-matched to an invoice only by
one of two paths. Anything else that looks plausible is "needs review", with a reason.

PATH A: invoice id
    (a) the amount is within the tolerance, in the invoice currency (or via a configured fx rate);
        a lower amount makes it a PARTIAL payment (the rest is still chased), a higher one needs review
    (b) EXACTLY ONE invoice id appears in the payment text, as a whole token (see IdIndex);
        the id must look like an id: a letter and a digit (INV-1004, A7), or 4+ digits with a
        configured prefix (ref_prefixes). Bare numbers never count.
    (c) no refund/reversal word anywhere in the text (refund, reversal, chargeback, credit note,
        return, cancel...)
    (d) that payment names no other invoice (the text must not contain two ids)

PATH B: name (only when the text names NO invoice)
    the amount is exact, in the same currency, and the payer IS the client: same words (as a
    multiset, order does not matter) and the same legal-suffix classes (Acme Ltd != Acme Inc,
    Acme != Acme Ltd); no refund word.

Never auto, whatever else matches: a payment dated outside the invoice window, a possible
duplicate row, a name-path payment that fits several invoices.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

_PART = re.compile(r"[^\W\d_]+|\d+")
_WORD = re.compile(r"[^\W\d_]+")
_INNER_OK = re.compile(r"[ \t]{0,2}[-_/.#:]?[ \t]{0,2}")
_CONTINUES = set("-_/.#:")  # after an id, these glue on more id (INV-1/2)
_LEAD_GLUE = set("-_/.")  # before an id, these mean it is the tail of something longer (X-INV-1)

NEGATIVE_WORDS = frozenset("""refund refunds refunded refunding reversal reversals reversed reverse reversing
chargeback chargebacks return returns returned cancel cancels cancelled canceled cancellation void voided
dispute disputed""".split())


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
    has_letter = any(p[0].isalpha() for p in parts)
    has_digit = any(p[0].isdigit() for p in parts)
    if has_letter and has_digit:
        return [parts]
    if has_digit and not has_letter and sum(len(p) for p in parts) >= 4:
        return [pp + parts for pp in (parts_of(p) for p in prefixes or ()) if pp]
    return []


class IdIndex:
    """Finds invoice ids in free text, strictly.

    The text is split into letter runs and digit runs. An id matches when its parts appear in a
    row, separated only by a space and/or one of - _ / . # :, and it is a COMPLETE token:
      * "INV-1/2", "INV-1.5", "INV-1-2", "INV-1A", "XINV-1", "INV-10" do NOT match INV-1;
      * "INV 1 2" does not match INV-1 (a spaced id followed by another number is ambiguous);
      * "INV-1", "inv#1", "INV 1", "(INV-1)", "INV-1." and "INV-1 and INV-2" do match.
    """

    def __init__(self, invoice_ids, prefixes=()):
        self.by_parts: dict[tuple[str, ...], list[str]] = {}
        for iid in invoice_ids:
            for v in id_variants(iid, prefixes):
                if iid not in self.by_parts.setdefault(v, []):
                    self.by_parts[v].append(iid)
        self.lengths = sorted({len(v) for v in self.by_parts})

    def find(self, text: str) -> frozenset:
        if not self.by_parts or not text:
            return frozenset()
        t = _nfkc(text)
        toks = [(m.group().casefold(), m.start(), m.end()) for m in _PART.finditer(t)]
        found: set[str] = set()
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
                    continue  # "A 7" is not a mention of invoice A7; spaced ids need a real prefix like INV
                last = i + L - 1
                if last + 1 < len(toks):
                    after = t[toks[last][2]:toks[last + 1][1]]
                    if after == "" or (len(after) == 1 and after in _CONTINUES):
                        continue  # INV-1A, INV-1/2, INV-1.5, INV-1-2
                    spaced = any(" " in s or "\t" in s for s in seps)
                    if spaced and after.strip() == "" and toks[last + 1][0].isdigit():
                        continue  # "INV 1 2"
                found.update(ids)
        return frozenset(found)


def has_negative_context(text: str) -> bool:
    words = [m.group().casefold() for m in _WORD.finditer(_nfkc(text))]
    if NEGATIVE_WORDS.intersection(words):
        return True
    return any(a == "credit" and b in ("note", "notes", "memo") for a, b in zip(words, words[1:]))


# ---------------------------------------------------------------- company names

SUFFIX_CLASS = {
    "ltd": "ltd", "limited": "ltd", "inc": "inc", "incorporated": "inc", "llc": "llc", "pllc": "pllc",
    "llp": "llp", "lp": "lp", "plc": "plc", "gmbh": "gmbh", "ag": "ag", "pvt": "pvt", "private": "pvt",
    "pty": "pty", "sa": "sa", "bv": "bv", "nv": "nv", "srl": "srl", "spa": "spa", "ab": "ab", "oy": "oy",
    "sdn": "sdn", "bhd": "bhd", "corp": "corp", "corporation": "corp", "co": "co", "company": "co",
}


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


_WORD_OR_DIGIT = re.compile(r"[^\W_]+")


def same_company(client: str, payer: str) -> bool:
    a = company_key(client)
    return a is not None and a == company_key(payer)
