"""Property-style attack on the "paid" decision: generate hundreds of near-miss ids, names and
texts and assert ZERO false paid. Positive controls prove the rules are not simply rejecting everything.

README states the total ("N adversarial cases"); test_readme_states_adversarial_count keeps it in sync.
"""
import itertools
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import inv, pay
from invoice_chaser.config import Config
from invoice_chaser.dedupe import dedupe_payments
from invoice_chaser.matching import match_all
from test_false_paid import CASES as HAND_CASES

TODAY = date(2026, 10, 25)
ISSUED, DUE, PAID_ON = date(2026, 9, 1), date(2026, 9, 15), date(2026, 9, 10)
ID_BASES = ["INV-1", "INV-12", "INV-1004", "A7", "INV-2026-001", "PO-5", "INV/77", "ORD_9"]


def _run(invoices, payments, cfg=None):
    payments = dedupe_payments(payments, lambda m: None)
    results, _ = match_all(invoices, payments, TODAY, cfg or Config())
    return {r.invoice.id: r.status for r in results}


def _one(invoice_id, client, payer, text, amount="1000", cfg=None):
    return _run([inv(invoice_id, client, amount=amount, issued=ISSUED, due=DUE)],
                [pay("P1", d=PAID_ON, amount=amount, desc=text, payer=payer)], cfg)[invoice_id]


# ---------------------------------------------------------------- generators

def id_near_misses():
    """(invoice id, payment text): the text must NOT settle that invoice."""
    out = []
    for x in ID_BASES:
        parts = re.findall(r"[A-Za-z]+|\d+", x)
        spaced = " ".join(parts)
        last = parts[-1]
        for glue in ["/2", ".5", "-2", "_3", "A", "0", "9", "#2", ":2", "x", "-A", "/A"]:
            out.append((x, f"payment {x}{glue}"))
        out += [(x, f"payment X{x}"), (x, f"payment 5{x}"), (x, f"Z-{x}"), (x, f"A/{x}"), (x, f"1.{x}"),
                (x, f"{spaced} 2"), (x, f"{spaced} 99"), (x, f"ref {parts[0]} {last}0"),
                (x, f"payment {parts[0]}{int(last) * 10 if last.isdigit() else last + last}")]
        for neg in ["refund of", "refund for", "Refunded:", "reversal of", "reversed", "chargeback", "credit note",
                    "credit memo", "return of", "returned", "cancelled", "canceled", "cancellation", "void", "disputed"]:
            out += [(x, f"{neg} {x}"), (x, f"{x} {neg}"), (x, f"{neg}: {x} thanks")]
    for x, texts in {"5": ["Deposit for 5 hours", "order 5 widgets", "5", "payment 5", "hours: 5", "INV 5", "INV-5"],
                     "1": ["order 1 widgets", "1", "item 1", "INV-1"],
                     "2024": ["Payment 2024 rent", "year 2024", "Q3 2024", "2024 deposit", "INV-2024", "Invoice 2024"],
                     "20240001": ["INV 20240001", "INV-20240001", "Payment 20240001", "20240001", "Order 20240001"],
                     "2026-001": ["2026-001", "payment 2026/001", "INV-2026-001"]}.items():
        out += [(x, t) for t in texts]
    return out


def multi_id_cases():
    ids = [("INV-1", "INV-2"), ("A7", "A8"), ("INV-1004", "INV-1005"), ("PO-5", "PO-6")]
    out = []
    for a, b in ids:
        for fmt in ["{a} and {b}", "{a}, {b}", "{b} {a}", "{a} / {b}", "{a};{b}", "invoice {a} + {b}", "{a} {b} thanks", "paid {a} & {b}"]:
            out.append((a, b, fmt.format(a=a, b=b)))
    return out


SUFFIX_CLASSES = {"ltd": "Ltd", "inc": "Inc", "llc": "LLC", "gmbh": "GmbH", "sa": "SA", "bv": "BV", "pvt": "Pvt Ltd"}


def name_near_misses():
    """(client, payer): different companies that a loose matcher would merge."""
    clients = ["Acme", "Acme Studio", "Alpha", "Tom Lee", "Harbor & Co", "Smith and Sons", "Zoë Park", "Beta Works",
               "Acme Ltd", "Müller GmbH", "Delta Inc", "Orchid LLC", "山田", "Alpha Beta"]
    out = []
    for c in clients:
        base = re.sub(r"\s+(Ltd|Inc|LLC|GmbH|SA|BV)$", "", c)
        has_suffix = base != c
        out += [(c, f"{base} Holdings"), (c, f"{base} Holdings Ltd"), (c, f"{base}s"), (c, f"{base}e"), (c, base[:-1]),
                (c, f"X{base}"), (c, f"{base} {base.split()[0]}"), (c, f"{base} 2"), (c, f"{base} Jr"),
                (c, f"{base}-{base.split()[0]}"), (c, base.replace(" ", "")), (c, f"{base} Berlin"),
                (c, f"The {base} Group"), (c, "Random Person"), (c, "")]
        if " " in base:
            out += [(c, base.split()[0]), (c, base.split()[-1]), (c, f"{base.split()[0]} {base.split()[0]}")]
        for suf in SUFFIX_CLASSES.values():
            if not (has_suffix and c.endswith(suf)):  # a DIFFERENT legal form, or a suffix the client lacks
                out.append((c, f"{base} {suf}"))
        if not has_suffix:
            out.append((c, f"{base} Ltd"))                  # Acme != Acme Ltd
        else:
            out.append((c, base))                           # Acme Ltd != Acme
        # unicode look-alikes: Cyrillic a/e/o, accents added or removed
        for latin, lookalike in [("a", "а"), ("e", "е"), ("o", "о"), ("A", "А")]:
            if latin in c:
                out.append((c, c.replace(latin, lookalike, 1)))
    out += [("Zoë Park", "Zoe Park"), ("Müller GmbH", "Muller GmbH"), ("Zoe Park", "Zoë Park"), ("Alpha", "Alpha Alpha"),
            ("Alpha Alpha", "Alpha"), ("Tom Lee", "Tom Leeds"), ("Tom Leeds", "Tom Lee"), ("Alpha Studio", "Alpha Labs"),
            ("Acme", "Acme Holdings"), ("Smith & Sons", "Smith Sons"), ("Co", "Ltd"), ("The Co", "Inc")]
    def squash(x):  # a variant that is just the same name with other spacing is not a near miss
        return re.sub(r"[\s&.]", "", x.casefold())
    return [(c, p) for c, p in dict.fromkeys(out) if squash(c) != squash(p)]


ID_CASES = id_near_misses()
MULTI_CASES = multi_id_cases()
NAME_CASES = name_near_misses()
# scenarios executed: hand-written + ids x (payer given / no payer) + multi-id x (1000 / 2000) + names x (payer field / description)
TOTAL = len(HAND_CASES) + len(ID_CASES) * 2 + len(MULTI_CASES) * 2 + len(NAME_CASES) * 2


# ---------------------------------------------------------------- the properties

def test_no_id_near_miss_is_ever_paid():
    bad = []
    for x, text in ID_CASES:
        for payer in ("Zed Corp", ""):
            if _one(x, "Client Co", payer, text) == "paid":
                bad.append((x, text, payer))
    assert bad == [], f"{len(bad)} FALSE PAID of {len(ID_CASES) * 2}: {bad[:10]}"


def test_no_multi_id_payment_is_ever_paid():
    bad = []
    for a, b, text in MULTI_CASES:
        invoices = [inv(a, "Zed", amount="1000", issued=ISSUED, due=DUE), inv(b, "Zed", amount="1000", issued=ISSUED, due=DUE)]
        for amount in ("1000", "2000"):
            for r, status in _run(invoices, [pay("P1", d=PAID_ON, amount=amount, desc=text, payer="Zed")]).items():
                if status == "paid":
                    bad.append((a, b, text, amount, r))
    assert bad == [], f"{len(bad)} FALSE PAID: {bad[:10]}"


def test_no_name_near_miss_is_ever_paid():
    bad = []
    for client, payer in NAME_CASES:
        if _one("INV-9", client, payer, "") == "paid":
            bad.append(("payer", client, payer))
        if _one("INV-9", client, "", f"Received from {payer}") == "paid":      # name only in the description
            bad.append(("description", client, payer))
    assert bad == [], f"{len(bad)} FALSE PAID of {len(NAME_CASES) * 2}: {bad[:10]}"


def test_bare_numbers_need_a_configured_prefix():
    cfg = Config(ref_prefixes=["INV"])
    assert _one("20240001", "Client Co", "Zed", "INV-20240001", cfg=cfg) == "paid"
    assert _one("20240001", "Client Co", "Zed", "INV 20240001", cfg=cfg) == "paid"
    for text in ("Payment 20240001", "Order 20240001", "20240001", "INV-20240002", "XINV-20240001"):
        assert _one("20240001", "Client Co", "Zed", text, cfg=cfg) != "paid", text
    assert _one("5", "Client Co", "Zed", "Deposit for 5 hours", cfg=cfg) != "paid"


@pytest.mark.parametrize("x,text", [("INV-1", "INV-1"), ("INV-1", "inv-1"), ("INV-1", "INV 1"), ("INV-1", "INV#1"),
                                    ("INV-1", "(INV-1)"), ("INV-1", "Payment for INV-1."), ("INV-1", "INV-1,"),
                                    ("INV-1", "INV-1 thanks"), ("INV-1", "INV1"), ("INV-1", "Invoice: INV-1 paid"),
                                    ("INV-1", "INV_1"), ("INV-1", "Ref INV-1 2026-09-10"), ("A7", "A7"), ("A7", "paid A7 today"),
                                    ("INV-2026-001", "INV-2026-001"), ("INV-2026-001", "INV 2026 001"),
                                    ("INV/77", "INV-77"), ("INV-12", "INV-12 INV-12")])
def test_real_references_are_still_recognised(x, text):
    assert _one(x, "Client Co", "Someone Else", text) == "paid"


@pytest.mark.parametrize("client,payer", [("Acme", "ACME"), ("Acme Ltd", "Acme Limited"), ("Alpha Beta", "Beta Alpha"),
                                          ("Harbor & Co", "Harbor and Co."), ("Zoë Park", "Zoë Park"),
                                          ("Zoë Park", "Zoë Park"), ("Müller GmbH", "MÜLLER GMBH"), ("山田", "山田"),
                                          ("Smith, John", "John Smith")])
def test_real_payers_are_still_recognised(client, payer):
    assert _one("INV-9", client, payer, "") == "paid"


def test_readme_states_adversarial_count():
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    found = re.findall(r"never auto-marks paid on a near match\D{0,40}?([\d,]+) adversarial cases", readme, re.I)
    assert len(found) >= 2, "README headline missing: 'never auto-marks paid on a near match: N adversarial cases in the test suite'"
    assert {int(n.replace(",", "")) for n in found} == {TOTAL}, f"README must say {TOTAL:,}"
