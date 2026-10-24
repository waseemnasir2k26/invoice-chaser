"""Property-style attack on the evidence table: generate near-miss ids, names, wording and dates and
assert that NOTHING is ever suggested as paid (let alone "paid": the engine never sets that). Positive
controls prove the rules are not simply rejecting everything.

run_all() EXECUTES every scenario and counts the iterations; the README states that count
(test_readme_states_adversarial_count), so the number is measured, not computed from a formula.
"""
import re
from collections import Counter
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import pytest

from conftest import inv, pay
from invoice_chaser.config import Config
from invoice_chaser.dedupe import dedupe_payments
from invoice_chaser.matching import match_all
from test_false_paid import CASES as HAND_CASES

TODAY = date(2026, 10, 25)
ISSUED, DUE, PAID_ON = date(2026, 9, 1), date(2026, 9, 15), date(2026, 9, 10)
STRONG = ("suggested_paid", "partial_suggested", "paid", "partial")
ID_BASES = ["INV-1", "INV-12", "INV-1004", "INV-2026-001", "INV/77", "AB1234", "INVOICE-55"]
SHORT_IDS = ["A7", "B2", "C3", "X1", "PO-5", "AB1"]


def _run(invoices, payments, cfg=None, today=TODAY):
    payments = dedupe_payments(payments, lambda m: None)
    results, _ = match_all(invoices, payments, today, cfg or Config())
    return {r.invoice.id: r.status for r in results}


def _one(invoice_id, client, payer, text, amount="1000", cfg=None, paid_on=PAID_ON, email="a@b.co"):
    i = inv(invoice_id, client, amount=amount, issued=ISSUED, due=DUE, email=email)
    return _run([i], [pay("P1", d=paid_on, amount=amount, desc=text, payer=payer)], cfg)[invoice_id]


# ---------------------------------------------------------------- generators

REFUND_WORDS = ["refund of", "refund for", "Refunded:", "reembolso", "Rückerstattung", "Rueckerstattung", "remboursement",
                "rimborso", "storno", "terugbetaling", "zwrot", "возврат", "返金", "退款", "Gutschrift", "avoir", "estorno",
                "iade", "استرداد", "واپسی", "reversal of", "reversed", "chargeback", "credit note", "credit memo", "return of",
                "returned", "cancelled", "canceled", "cancellation", "void", "disputed"]
FAILURE_WORDS = ["payment failed", "failed", "pending", "declined", "rejected", "bounced", "NSF", "unpaid", "not received",
                 "not yet received", "insufficient funds", "PENDING APPROVAL", "Failed transfer"]


def id_near_misses():
    """(invoice id, payment text): the text must NOT produce a suggestion for that invoice."""
    out = []
    for x in ID_BASES:
        parts = re.findall(r"[A-Za-z]+|\d+", x)
        spaced = " ".join(parts)
        last = parts[-1]
        for glue in ["/2", ".5", "-2", "_3", "A", "0", "9", "#2", ":2", "x", "-A", "/A"]:
            out.append((x, f"payment {x}{glue}"))
        out += [(x, f"payment X{x}"), (x, f"payment 5{x}"), (x, f"Z-{x}"), (x, f"A/{x}"), (x, f"1.{x}"),
                (x, f"{spaced} 2"), (x, f"{spaced} 99"),
                (x, f"payment {parts[0]}{int(last) * 10 if last.isdigit() else last + last}")]
        for neg in REFUND_WORDS + FAILURE_WORDS:
            out += [(x, f"{neg} {x}"), (x, f"{x} {neg}"), (x, f"{neg}: {x} thanks")]
        out += [(x, f"mail {x.lower()}@example.com"), (x, f"billing.{x}@corp.example"), (x, f"https://pay.example/{x}"),
                (x, f"see www.example.com/invoices/{x}"), (x, f"ftp://files.example/{x}.pdf")]
        out += [(x, f"{x} and INV-9999"), (x, f"INV-9999 {x}"), (x, f"{x} INVOICE-31337")]   # also names an id not in the file
    for x in SHORT_IDS:
        out += [(x, t) for t in [x, f"paid {x}", f"Vitamin {x}", f"Vitamin {x} order", f"{x} deposit", f"ref {x}", f"({x})"]]
    for x, texts in {"5": ["Deposit for 5 hours", "order 5 widgets", "5", "payment 5", "hours: 5", "INV 5", "INV-5"],
                     "1": ["order 1 widgets", "1", "item 1", "INV-1"],
                     "2024": ["Payment 2024 rent", "year 2024", "Q3 2024", "2024 deposit"],
                     "123": ["INV-123", "INV 123", "invoice 123"],
                     "20240001": ["Payment 20240001", "20240001", "Order 20240001"],
                     "2026-001": ["2026-001", "payment 2026/001"]}.items():
        out += [(x, t) for t in texts]
    return out


def multi_id_cases():
    ids = [("INV-1", "INV-2"), ("INV-1004", "INV-1005"), ("AB1234", "AB1235"), ("INV-77", "INV-78")]
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
        out.append((c, f"{base} Ltd") if not has_suffix else (c, base))
        for latin, lookalike in [("a", "а"), ("e", "е"), ("o", "о"), ("A", "А")]:
            if latin in c:
                out.append((c, c.replace(latin, lookalike, 1)))
    out += [("Zoë Park", "Zoe Park"), ("Müller GmbH", "Muller GmbH"), ("Zoe Park", "Zoë Park"), ("Alpha", "Alpha Alpha"),
            ("Alpha Alpha", "Alpha"), ("Tom Lee", "Tom Leeds"), ("Tom Leeds", "Tom Lee"), ("Alpha Studio", "Alpha Labs"),
            ("Acme", "Acme Holdings"), ("Smith & Sons", "Smith Sons"), ("Co", "Ltd"), ("The Co", "Inc")]

    def squash(x):  # a variant that is just the same name with other spacing is not a near miss
        return re.sub(r"[\s&.]", "", x.casefold())
    return [(c, p) for c, p in dict.fromkeys(out) if squash(c) != squash(p)]


def name_with_failure_words():
    return [(c, w) for c in ("Acme Studio", "Zoë Park", "Beta Works") for w in REFUND_WORDS + FAILURE_WORDS]


# ---------------------------------------------------------------- one pass over everything

LOG = []

@lru_cache(maxsize=1)
def run_all():
    """Executes every scenario. Returns (executed, strong_suggestions_that_should_not_exist, status_counter)."""
    executed, bad, seen = 0, [], Counter()
    LOG.clear()

    def check(label, statuses, must_not=None):
        nonlocal executed
        executed += 1
        LOG.append(label)   # every executed scenario leaves a trace: the count below is measured, not computed
        seen.update(statuses.values())
        hit = [i for i, s in statuses.items() if s in STRONG and (must_not is None or i in must_not)]
        if hit:
            bad.append((label, hit))

    for x, text in id_near_misses():
        for payer in ("Zed Corp", ""):
            check(("id", x, text, payer), {x: _one(x, "Client Co", payer, text)})
    for a, b, text in multi_id_cases():
        invoices = [inv(a, "Zed", amount="1000", issued=ISSUED, due=DUE), inv(b, "Zed", amount="1000", issued=ISSUED, due=DUE)]
        for amount in ("1000", "2000"):
            check(("multi", a, b, text, amount), _run(invoices, [pay("P1", d=PAID_ON, amount=amount, desc=text, payer="Zed")]))
    for n in (4, 5, 8):  # one payment naming many invoices at once
        invoices = [inv(f"INV-{100 + k}", "Zed", amount="500", issued=ISSUED, due=DUE) for k in range(n)]
        text = " ".join(i.id for i in invoices)
        check(("names-many", n), _run(invoices, [pay("P1", d=PAID_ON, amount="500", desc=text, payer="Zed")]))
    for client, payer in name_near_misses():
        check(("payer", client, payer), {"INV-9": _one("INV-9", client, payer, "")})
        check(("description", client, payer), {"INV-9": _one("INV-9", client, "", f"Received from {payer}")})
    for client, word in name_with_failure_words():
        check(("name+negative", client, word), {"INV-9": _one("INV-9", client, client, word)})
    for delta in (1, 2, 30, 400):  # future-dated payments, by id and by name
        future = TODAY + timedelta(days=delta)
        check(("future-id", delta), {"INV-1004": _one("INV-1004", "Client Co", "Zed", "INV-1004", paid_on=future)})
        check(("future-name", delta), {"INV-9": _one("INV-9", "Acme Studio", "Acme Studio", "", paid_on=future)})
    for d1, d2 in [("payment INV-", "1004"), ("INV", "-1004"), ("INV-10", "04"), ("paid INV-1", "004 Co"), ("INV 1", "004")]:
        i = inv("INV-1004", "Client Co", amount="1000", issued=ISSUED, due=DUE)
        check(("glued", d1, d2), _run([i], [pay("P1", d=PAID_ON, amount="1000", desc=d1, payer=d2)]))
    for kind, invoices in {
        "settled-peer": [inv("INV-1", "Acme Studio", amount="1000", status="paid", issued=ISSUED, due=DUE),
                         inv("INV-2", "Acme Studio", amount="1000", issued=ISSUED, due=DUE)],
        "void-peer": [inv("INV-1", "Acme Studio", amount="1000", status="void", issued=ISSUED, due=DUE),
                      inv("INV-2", "Acme Studio", amount="1000", issued=ISSUED, due=DUE)],
        "same-name-two-clients": [inv("INV-1", "Acme Studio", amount="1000", issued=ISSUED, due=DUE, email="a@one.example"),
                                  inv("INV-2", "Acme Studio", amount="700", issued=ISSUED, due=DUE, email="b@two.example")],
    }.items():
        got = _run(invoices, [pay("P1", d=PAID_ON, amount="1000", payer="Acme Studio")])
        if kind != "same-name-two-clients":
            got.pop("INV-1")  # that one is settled or void in the invoice list itself
        check((kind,), got)
    ok_statuses = {"suggested_paid", "partial_suggested", "needs_review", "overdue", "open", "paid", "void"}
    assert set(seen) <= ok_statuses, set(seen) - ok_statuses
    return executed + len(HAND_CASES), bad, seen


# ---------------------------------------------------------------- the properties

def test_nothing_near_a_match_is_ever_suggested_or_paid():
    executed, bad, seen = run_all()
    assert bad == [], f"{len(bad)} strong suggestions that must not exist: {bad[:8]}"
    assert executed > 1500


def test_the_engine_never_outputs_paid_without_a_ledger_confirm():
    _, _, seen = run_all()
    assert "paid" not in seen and "partial" not in seen                 # only confirm / mark-paid / invoices.csv can set these
    assert seen["suggested_paid"] == 0 and seen["needs_review"] > 500   # and nothing here deserved even a suggestion


def test_bare_numbers_and_short_ids_never_count():
    cfg = Config(ref_prefixes=["INV"])
    assert _one("20240001", "Client Co", "Zed", "INV-20240001", cfg=cfg) == "suggested_paid"
    assert _one("20240001", "Client Co", "Zed", "INV 20240001", cfg=cfg) == "suggested_paid"
    for text in ("Payment 20240001", "Order 20240001", "20240001", "INV-20240002", "XINV-20240001"):
        assert _one("20240001", "Client Co", "Zed", text, cfg=cfg) != "suggested_paid", text
    assert _one("5", "Client Co", "Zed", "Deposit for 5 hours", cfg=cfg) != "suggested_paid"
    assert _one("B2", "Client Co", "Zed", "Vitamin B2") != "suggested_paid"


@pytest.mark.parametrize("x,text", [("INV-1", "INV-1"), ("INV-1", "inv-1"), ("INV-1", "INV 1"), ("INV-1", "INV#1"),
                                    ("INV-1", "(INV-1)"), ("INV-1", "Payment for INV-1."), ("INV-1", "INV-1,"),
                                    ("INV-1", "INV-1 thanks"), ("INV-1", "INV1"), ("INV-1", "Invoice: INV-1 paid"),
                                    ("INV-1", "INV_1"), ("INV-1", "Ref INV-1 2026-09-10"), ("AB1234", "AB1234"),
                                    ("AB1234", "paid AB1234 today"), ("INV-2026-001", "INV-2026-001"),
                                    ("INV-2026-001", "INV 2026 001"), ("INV/77", "INV-77"), ("INV-12", "INV-12 INV-12"),
                                    ("INV-1", "Zahlung für INV-1, danke"), ("INV-1", "pago de INV-1 recibido")])
def test_real_references_are_still_recognised(x, text):
    assert _one(x, "Client Co", "Someone Else", text) == "suggested_paid"


@pytest.mark.parametrize("client,payer", [("Acme", "ACME"), ("Acme Ltd", "Acme Limited"), ("Alpha Beta", "Beta Alpha"),
                                          ("Harbor & Co", "Harbor and Co."), ("Zoë Park", "Zoë Park"),
                                          ("Zoë Park", "Zoë Park"), ("Müller GmbH", "MÜLLER GMBH"), ("山田", "山田"),
                                          ("Smith, John", "John Smith")])
def test_real_payers_are_still_recognised(client, payer):
    assert _one("INV-9", client, payer, "") == "suggested_paid"


def test_the_executed_count_is_measured_not_computed():
    executed = run_all()[0]
    assert executed == len(LOG) + len(HAND_CASES)
    assert len(LOG) > 3000 and len(HAND_CASES) >= 30
    kinds = Counter(label[0] for label in LOG)
    assert {"id", "multi", "payer", "description", "future-id", "glued", "settled-peer"} <= set(kinds)


def test_readme_states_adversarial_count():
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    found = re.findall(r"never suggests paid on a near match\D{0,60}?([\d,]+) adversarial cases", readme, re.I)
    assert len(found) >= 1, "README headline missing: 'never suggests paid on a near match: N adversarial cases in the test suite'"
    executed = run_all()[0]
    assert {int(n.replace(",", "")) for n in found} == {executed}, f"README must say {executed:,}"
