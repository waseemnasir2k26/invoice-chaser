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

# ---- wording that makes a payment unusable as evidence: refunds, reversals, failures, non-receipt ----
# Four kinds of entry, so that a client called Stornoway, Returnly, Voidstar, Kreditech or Pending Ventures is
# not mistaken for a refund:
#   NEGATIVE_WORDS  whole words only (the word must equal the entry, case-folded)
#   LONG_STEMS      long, distinctive stems (7+ letters) found anywhere inside a word, to cover inflections
#   SUBSTRINGS      scripts written without spaces or with attached particles (CJK, Thai, Korean, Hindi, Arabic...)
#   PHRASES         word sequences
# A client whose own name contains one of these (Chargebacks911, Pending Ventures, Refund Ltd) is never
# matched automatically: the matcher reports "client name contains a refund word: please check".
NEGATIVE_WORDS = frozenset("""
refund refunds refunded refunding reversal reversals reverse reverses reversed reversing chargeback chargebacks
return returns returned returning cancel cancels cancelled canceled cancelling canceling cancellation cancellations
void voided dispute disputes disputed clawback repayment repaid reimbursement reimbursed reimburse restitution
denied blocked rejected reject rejects declined decline declines bounced bounce failed fail fails failure failures
pending nsf unpaid unsuccessful overdraft refusal refused
storno storniert stornierung stornierungen stornieren rückbuchung rueckbuchung rückgabe rueckgabe retour retours
retourné retournée annulé annulée annulation rejeté rejetée échoué echoué impayé impayée avoir
reembolso reembolsos reembolsado devolución devolucion devoluciones devuelto anulado anulación anulacion rechazado
fallido impago pendiente
estorno estornado devolução devolucao devolvido cancelado recusado pendente falhou
rimborso rimborsi rimborsato storno annullato respinto insoluto restituzione
terugbetaling terugbetaald teruggestort restitutie creditnota geannuleerd afgewezen mislukt
zwrot zwrotu zwroty zwrotem zwrócono anulowane odrzucone
vrácení vráceno storna zrušeno zamítnuto
возврат возврата возвраты возвращен возвращён отмена отменен отменён отклонен отклонён
iade iadesi iptal reddedildi başarısız
استرداد ارتجاع مرتجع مرفوض ملغي فشل
واپسی منسوخ ناکام
بازپرداخت برگشت
החזר ביטול
gagal ditolak dibatalkan
refund
""".split())
LONG_STEMS = (
    "rückerstattung", "rueckerstattung", "erstattung", "rückzahlung", "rueckzahlung", "rücklastschrift", "rucklastschrift",
    "gutschrift", "stornierung", "remboursement", "annulation", "restitution", "reembolso", "reembolsad",
    "devolución", "devolução", "rimborso", "rimborsat", "terugbetaling", "terugbetaal", "creditnota", "återbetalning",
    "tilbagebetaling", "tilbakebetaling", "pengembalian", "pembatalan", "rambursare", "visszatérítés", "palautus",
    "chargeback", "reimburse", "refunded",
)
SUBSTRINGS = (
    "返金", "返品", "払い戻し", "払戻", "退款", "退费", "退費", "退货", "退貨", "退还", "환불", "반품", "คืนเงิน", "वापसी",
    "ফেরত", "επιστροφή", "החזר", "استرداد", "بازپرداخت", "واپسی", "возврат", "hoàn tiền", "hoan tien",
)
PHRASES = (
    ("money", "back"), ("on", "hold"), ("not", "paid"), ("not", "completed"), ("not", "received"), ("not", "yet", "received"),
    ("was", "not", "received"), ("did", "not", "go", "through"), ("didn", "t", "go", "through"), ("wasn", "t", "received"),
    ("could", "not", "be", "processed"), ("payment", "failed"), ("insufficient", "funds"), ("credit", "reversal"),
)
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


# Letters that look like Latin ones (Cyrillic and Greek homoglyphs) and invisible characters are used to hide a word
# from a plain search. The check therefore looks at the text several ways.
_CONFUSABLES = str.maketrans({
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t", "у": "y", "х": "x",
    "і": "i", "ї": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ɡ": "g", "ո": "n", "ս": "u",
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "χ": "x", "γ": "y",
})
_SPACED = re.compile(r"(?<![^\W\d_])(?:[^\W\d_][ \u00a0.\-_]+){2,}[^\W\d_](?![^\W\d_])")
_SPACER = re.compile(r"[ \u00a0.\-_]+")
_INVISIBLE_NAMES = {"\u200b": "ZWSP", "\u200c": "ZWNJ", "\u200d": "ZWJ", "\u2060": "WJ", "\u00ad": "SHY", "\ufeff": "BOM",
                    "\u200e": "LRM", "\u200f": "RLM", "\u2061": "FA", "\u2062": "IT", "\u2063": "IS", "\u2064": "IP"}


def _strip_invisible(t: str) -> str:
    return "".join(ch for ch in t if unicodedata.category(ch) != "Cf")


def visible(text: str) -> str:
    """Show invisible characters visibly, e.g. a zero-width space as <ZWSP>, so a payer or description cannot hide them."""
    out = []
    for ch in text or "":
        if unicodedata.category(ch) == "Cf":
            out.append("\u27e8" + _INVISIBLE_NAMES.get(ch, f"U+{ord(ch):04X}") + "\u27e9")
        else:
            out.append(ch)
    return "".join(out)


def _fold(t: str) -> str:
    """Letters without diacritics, Cyrillic/Greek look-alikes as Latin."""
    d = unicodedata.normalize("NFKD", t.translate(_CONFUSABLES))
    return "".join(ch for ch in d if not unicodedata.combining(ch)).translate(_CONFUSABLES)


def _collapse_spaced(t: str) -> str:
    """"r e f u n d" becomes "refund"."""
    def join(m):
        letters = _SPACER.sub("", m.group())
        return letters if len(letters) >= 3 else m.group()
    return _SPACED.sub(join, t)


def has_negative_context(text: str) -> bool:
    """Refund, reversal, failure or non-receipt wording (English plus the common payment languages)."""
    # cached per text; the key holds the identity of the word lists so that swapping a list (tests do) never reuses a stale answer
    return _negative(text, id(NEGATIVE_WORDS), id(LONG_STEMS), id(SUBSTRINGS), id(PHRASES), id(_CREDIT_NOTE))


@lru_cache(maxsize=16)
def _folded_lists(*_ids):
    return (frozenset(_fold(w) for w in NEGATIVE_WORDS), tuple(_fold(w) for w in LONG_STEMS),
            tuple(_fold(w) for w in SUBSTRINGS), tuple(tuple(_fold(x) for x in ph) for ph in PHRASES))


@lru_cache(maxsize=131072)
def _negative(text: str, *_lists) -> bool:
    base = _strip_invisible(_nfkc(text)).casefold().replace("\u2019", "'")
    if not base:
        return False
    spaced = _collapse_spaced(base)
    variants = {base, _fold(base), spaced, _fold(spaced)}
    folded_lists = _folded_lists(*_lists)
    for v in variants:
        if _check(v, NEGATIVE_WORDS, LONG_STEMS, SUBSTRINGS, PHRASES) or _check(v, *folded_lists):
            return True
    return False


def _check(t: str, words, stems, substrings, phrases) -> bool:
    if any(sub in t for sub in substrings):
        return True
    t = t.replace("n't", " not")
    ws = [m.group() for m in _WORD.finditer(t)]
    wordset = set(ws)
    if wordset & words:
        return True
    if any(stem in t for stem in stems):
        return True
    for phrase in phrases:
        if phrase[0] not in wordset:
            continue
        n = len(phrase)
        if any(tuple(ws[i:i + n]) == phrase for i in range(len(ws) - n + 1)):
            return True
    return any(w == "credit" and i + 1 < len(ws) and ws[i + 1] in _CREDIT_NOTE for i, w in enumerate(ws))


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
