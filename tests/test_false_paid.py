"""A false "paid" is the worst failure this tool can have: the client is never chased.

Each case lists invoices and payments and the exact set of invoice ids that may come out
"paid". Anything else must stay needs-review / overdue. The README states this case count and
test_readme_states_case_count keeps the two in sync.
"""
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import inv, pay
from invoice_chaser.config import Config
from invoice_chaser.dedupe import dedupe_payments
from invoice_chaser.matching import match_all, strong_name

TODAY = date(2026, 10, 25)
D = date(2026, 10, 5)

CASES = {
    # --- a different company with a similar name -------------------------------------
    "Acme vs Acme Holdings": dict(
        invoices=[inv("INV-1", "Acme")], payments=[pay(payer="Acme Holdings")], paid=set()),
    "Acme vs Acme Holdings Ltd": dict(
        invoices=[inv("INV-1", "Acme")], payments=[pay(payer="Acme Holdings Ltd")], paid=set()),
    "Tom Lee vs Tom Leeds": dict(
        invoices=[inv("INV-1", "Tom Lee")], payments=[pay(payer="Tom Leeds")], paid=set()),
    "Alpha Studio vs Alpha Labs": dict(
        invoices=[inv("INV-1", "Alpha Studio")], payments=[pay(payer="Alpha Labs")], paid=set()),
    "client is a substring of the payer word": dict(
        invoices=[inv("INV-1", "Acme")], payments=[pay(payer="Acmeville")], paid=set()),
    "payer has an extra distinctive word": dict(
        invoices=[inv("INV-1", "Acme Studio")], payments=[pay(payer="Acme Studio Berlin")], paid=set()),
    "payer is missing a client word": dict(
        invoices=[inv("INV-1", "Acme Studio")], payments=[pay(payer="Acme")], paid=set()),
    "same words, different person": dict(
        invoices=[inv("INV-1", "Sam Lee")], payments=[pay(payer="Lee Samantha")], paid=set()),
    # --- no payer / no reference -------------------------------------------------------
    "exact amount, no payer, no reference": dict(
        invoices=[inv("INV-1", "Acme")], payments=[pay(desc="transfer")], paid=set()),
    "exact amount, name only in the description": dict(
        invoices=[inv("INV-1", "Acme")], payments=[pay(desc="Received from Acme Holdings")], paid=set()),
    "id of a different invoice in the text": dict(
        invoices=[inv("INV-12", "Acme")], payments=[pay(payer="Other Co", desc="INV-120")], paid=set()),
    # --- several candidates ------------------------------------------------------------
    "same amount from two clients: only the right one is paid": dict(
        invoices=[inv("INV-1", "Acme Studio"), inv("INV-2", "Beta Works")],
        payments=[pay(payer="Beta Works")], paid={"INV-2"}),
    "same client, two same-amount invoices, one payment, no reference": dict(
        invoices=[inv("INV-1", "Acme Studio"), inv("INV-2", "Acme Studio")],
        payments=[pay(payer="Acme Studio")], paid=set()),
    "same client, two same-amount invoices, reference picks one": dict(
        invoices=[inv("INV-1", "Acme Studio"), inv("INV-2", "Acme Studio")],
        payments=[pay(payer="Acme Studio", desc="INV-2")], paid={"INV-2"}),
    # --- amount ------------------------------------------------------------------------
    "fee shortfall with a strong name but no reference": dict(
        invoices=[inv("INV-1", "Acme Studio", amount="800")],
        payments=[pay(payer="Acme Studio", amount="786")], paid=set()),
    "same number, other currency, no rate": dict(
        invoices=[inv("INV-1", "Acme Studio")], payments=[pay(payer="Acme Studio", cur="EUR")], paid=set()),
    # --- dates -------------------------------------------------------------------------
    "strong match dated before the invoice (date format mix-up)": dict(
        invoices=[inv("INV-1", "Acme Studio", issued=date(2026, 4, 3), due=date(2026, 5, 3))],
        payments=[pay(payer="Acme Studio", d=date(2026, 3, 4))], paid=set()),
    # --- duplicate rows ----------------------------------------------------------------
    "one transfer listed twice settles only one invoice": dict(
        invoices=[inv("INV-1", "Acme Studio"), inv("INV-2", "Acme Studio")],
        payments=[pay("T1", payer="Acme Studio", desc="INV-1"), pay("T1", payer="Acme Studio", desc="INV-1")],
        paid={"INV-1"}),
    "same payment in Wise and in the bank export": dict(
        invoices=[inv("INV-1", "Acme Studio")],
        payments=[pay("W1", payer="Acme Studio", source="wise"),
                  pay("B1", d=date(2026, 10, 6), payer="Acme Studio", source="bank")], paid=set()),
    # --- positive controls: real payments must still be recognised -----------------------
    "control: exact amount, payer is the client (suffix differs)": dict(
        invoices=[inv("INV-1", "Acme Studio Ltd")], payments=[pay(payer="ACME STUDIO LIMITED")], paid={"INV-1"}),
    "control: invoice id in the text": dict(
        invoices=[inv("INV-1", "Acme Studio")], payments=[pay(payer="Someone Else", desc="Invoice INV-1")], paid={"INV-1"}),
    "control: ampersand and Co": dict(
        invoices=[inv("INV-1", "Harbor & Co")], payments=[pay(payer="Harbor and Co.")], paid={"INV-1"}),
}


@pytest.mark.parametrize("name", list(CASES))
def test_false_paid(name):
    case = CASES[name]
    payments = dedupe_payments([p for p in case["payments"]], lambda m: None)
    results, _ = match_all(case["invoices"], payments, TODAY, Config())
    paid = {r.invoice.id for r in results if r.status == "paid"}
    assert paid == case["paid"], (name, {r.invoice.id: r.status for r in results})
    for r in results:
        if r.status == "paid":
            assert r.matches and all(m.blocker == "" for m in r.matches)
        else:
            assert r.paid == Decimal("0.00") or r.status == "partial"


def test_strong_name_rule():
    assert strong_name("Acme Studio Ltd", "ACME STUDIO LIMITED")
    assert strong_name("Harbor & Co", "Harbor and Co.")
    assert not strong_name("Acme", "Acme Holdings")
    assert not strong_name("Tom Lee", "Tom Leeds")
    assert not strong_name("Acme", "")
    assert not strong_name("", "Acme")


def test_not_paid_always_explains_itself():
    case = CASES["Acme vs Acme Holdings"]
    (r,), _ = match_all(case["invoices"], case["payments"], TODAY, Config())
    assert r.status == "needs_review" and r.stage is None
    assert "'Acme Holdings' is not exactly 'Acme'" in r.why_not_paid and "no invoice id" in r.why_not_paid


def test_readme_states_case_count():
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    m = re.search(r"false-paid tests: (\d+) cases", readme)
    assert m and int(m.group(1)) == len(CASES), f"README must say 'false-paid tests: {len(CASES)} cases'"
