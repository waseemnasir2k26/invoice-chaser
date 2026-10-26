"""Mutation-backed tests: for each class of false-paid attack, the scenario is NOT suggested under the real
rules, and IS suggested when that one rule is switched off. A test that passes without its rule would prove
nothing; these fail if the rule is removed or loosened.
"""
import csv
import json
from datetime import date, timedelta

import pytest

from conftest import inv, pay
from invoice_chaser import cli, decision, matching
from invoice_chaser.cli import main
from invoice_chaser.config import Config
from invoice_chaser.matching import MAX_NAMED, match_all

TODAY = date(2026, 10, 25)
ISSUED, DUE, PAID_ON = date(2026, 9, 1), date(2026, 9, 15), date(2026, 9, 10)
SUGGESTED = "suggested_paid"


def status(invoices, payments, cfg=None, today=TODAY):
    results, _ = match_all(invoices, payments, today, cfg or Config())
    return {r.invoice.id: r.status for r in results}


def one(invoice_id="INV-1004", client="Client Co", payer="Zed", text="", paid_on=PAID_ON, cfg=None):
    return status([inv(invoice_id, client, issued=ISSUED, due=DUE)],
                  [pay("P1", d=paid_on, desc=text, payer=payer)], cfg)[invoice_id]


def flips(monkeypatch, scenario, *patches):
    """True when the scenario is refused normally and suggested once the rule is switched off."""
    assert scenario() != SUGGESTED, "the real rules must refuse this scenario"
    for module, name, value in patches:
        monkeypatch.setattr(module, name, value)
    return scenario() == SUGGESTED


NO_WORDS = [(decision, "NEGATIVE_WORDS", frozenset()), (decision, "LONG_STEMS", ()), (decision, "SUBSTRINGS", ()), (decision, "PHRASES", ())]

REFUND = ["refund", "reembolso", "Rückerstattung", "Rueckerstattung", "Erstattung", "Rückzahlung", "Rücklastschrift", "remboursement",
          "rimborso", "storno", "terugbetaling", "zwrot", "возврат", "Gutschrift", "avoir", "estorno", "iade", "استرداد", "واپسی",
          "reversal", "chargeback", "returned", "cancelled", "voided", "disputed", "devolución", "devolução", "retour", "restitution",
          "annulé", "clawback", "money back", "repayment", "reimbursement", "pengembalian", "återbetalning", "tilbagebetaling",
          "tilbakebetaling", "rambursare", "visszatérítés", "palautus", "vrácení", "بازپرداخت", "החזר"]
SUBSTRING_WORDS = ["返金", "退款", "返品", "환불", "คืนเงิน", "वापसी", "ফেরত", "επιστροφή", "hoàn tiền"]
FAILURE = ["failed", "payment failed", "pending", "declined", "rejected", "bounced", "NSF", "unpaid", "insufficient funds", "denied",
           "blocked", "on hold", "not paid", "not completed", "unsuccessful", "wasn't received", "didn't go through", "not received",
           "not yet received", "was not received", "did not go through"]


@pytest.mark.parametrize("word", REFUND)
def test_class_multilingual_refund_words(monkeypatch, word):
    assert flips(monkeypatch, lambda: one(text=f"{word} INV-1004"), *NO_WORDS)


@pytest.mark.parametrize("word", SUBSTRING_WORDS)
def test_class_non_latin_refund_words(monkeypatch, word):
    assert flips(monkeypatch, lambda: one(text=f"{word}済み INV-1004"), *NO_WORDS)


@pytest.mark.parametrize("word", FAILURE)
def test_class_failure_words(monkeypatch, word):
    assert flips(monkeypatch, lambda: one(text=f"INV-1004 {word}"), *NO_WORDS)


@pytest.mark.parametrize("text", ["credit note INV-1004", "credit memo INV-1004", "INV-1004 credit nota"])
def test_class_credit_notes(monkeypatch, text):
    assert flips(monkeypatch, lambda: one(text=text), (decision, "_CREDIT_NOTE", set()))


def test_credit_alone_is_not_a_refund():
    assert one(text="Credit transfer INV-1004 thanks") == SUGGESTED


@pytest.mark.parametrize("name", ["Stornoway Ltd", "Returnly", "Voidstar", "Kreditech", "Refundly Inc", "Cancelot", "Failsafe Co",
                                  "Blockchain Labs", "Denise Holdings", "Retourist", "Re", "Un", "Ac", "Pend"])
def test_client_names_that_only_look_like_a_refund_word_are_not_blocked(name):
    assert one(client=name, payer=name, text="") == SUGGESTED                       # path B: the client itself
    assert one(client=name, payer=name, text=f"Received from {name} INV-1004") == SUGGESTED   # path A, name in the text
    assert one(client=name, payer=name, text=f"refund {name} INV-1004") != SUGGESTED          # but a real refund word still blocks


@pytest.mark.parametrize("name", ["Refund Ltd", "Pending Ventures", "Chargebacks911", "Failed Banks Inc", "Money Back Guarantee"])
def test_a_client_whose_own_name_contains_a_refund_word_is_always_a_human_check(monkeypatch, name):
    why = lambda: match_all([inv("INV-1004", name, issued=ISSUED, due=DUE)],      # noqa: E731
                            [pay("P1", d=PAID_ON, payer=name, desc=f"Received from {name} INV-1004")], TODAY, Config())[0][0]
    assert why().status == "needs_review" and "client name contains a refund word: please check" in why().why_not_paid
    monkeypatch.setattr(matching, "has_negative_context", lambda t: False)       # the check is what holds it back
    assert why().status == SUGGESTED


@pytest.mark.parametrize("name,text", [("Re", "unpaid INV-1004"), ("Un", "unpaid INV-1004"), ("Ac", "chargeback INV-1004"),
                                       ("Re", "refund INV-1004"), ("Refund Ltd", "INV-1004 refund"), ("Un", "Un unpaid")])
def test_the_clients_name_is_never_cut_out_of_the_text(name, text):
    assert one(client=name, payer=name, text=text) != SUGGESTED


def test_class_description_and_payer_are_never_glued(monkeypatch):
    scenario = lambda: one(text="payment INV-", payer="1004")      # noqa: E731
    assert flips(monkeypatch, scenario, (decision, "SEPARATE_FIELDS", False))
    monkeypatch.undo()
    assert one(text="INV", payer="-1004") != SUGGESTED


def test_class_future_dated_payment(monkeypatch):
    scenario = lambda: one(text="INV-1004", paid_on=TODAY + timedelta(days=3))      # noqa: E731
    assert flips(monkeypatch, scenario, (matching, "FUTURE_CHECK", False))
    monkeypatch.undo()
    r, _ = match_all([inv("INV-1004", "Client Co", issued=ISSUED, due=DUE)],
                     [pay("P1", d=TODAY + timedelta(days=3), desc="INV-1004", payer="Zed")], TODAY, Config())
    assert "future" in r[0].why_not_paid


def test_class_short_ids_like_B2(monkeypatch):
    scenario = lambda: one(invoice_id="B2", text="Vitamin B2 payment")      # noqa: E731
    assert flips(monkeypatch, scenario, (decision, "MIN_LETTERS", 1), (decision, "MIN_DIGITS", 1))


def test_class_ids_inside_emails_and_urls(monkeypatch):
    for text in ("mail inv-1004@example.com", "see https://shop.example?ref=INV-1004", "www.example.com?x=INV-1004"):
        assert flips(monkeypatch, lambda: one(text=text), (decision, "MASK_EMAIL_URL", False)), text
        monkeypatch.undo()


def test_class_an_id_that_is_not_in_the_file(monkeypatch):
    scenario = lambda: one(text="INV-1004 and INV-9999")      # noqa: E731
    assert flips(monkeypatch, scenario, (decision, "FOREIGN_IDS", False))


def test_class_settled_invoice_of_same_client_and_price(monkeypatch):
    invoices = [inv("INV-1", "Acme Studio", status="paid", issued=ISSUED, due=DUE),
                inv("INV-2", "Acme Studio", issued=ISSUED, due=DUE)]
    scenario = lambda: status(invoices, [pay("P1", d=PAID_ON, payer="Acme Studio")])["INV-2"]      # noqa: E731
    assert flips(monkeypatch, scenario, (matching, "COUNT_SETTLED_PEERS", False))


def test_class_two_different_clients_with_the_same_name(monkeypatch):
    invoices = [inv("INV-1", "Acme Studio", email="a@one.example", issued=ISSUED, due=DUE),
                inv("INV-2", "Acme Studio", amount="700", email="b@two.example", issued=ISSUED, due=DUE)]
    scenario = lambda: status(invoices, [pay("P1", d=PAID_ON, payer="Acme Studio")])["INV-1"]      # noqa: E731
    assert flips(monkeypatch, scenario, (matching, "CHECK_NAME_IDENTITY", False))


def test_class_stale_confirmation_after_the_invoice_changes(monkeypatch, tmp_path):
    (tmp_path / "i.csv").write_text("id,client,email,amount,currency,issued,due\nINV-1,Acme,a@b.co,1000,USD,2026-09-01,2026-10-01\n", encoding="utf-8")
    (tmp_path / "w.csv").write_text('"TransferWise ID","Date","Amount","Currency","Description","Payment Reference","Payer Name"\n'
                                    '"W1","05-10-2026","1000.00","USD","INV-1","INV-1","Acme"\n', encoding="utf-8")
    argv = ["run", "--invoices", str(tmp_path / "i.csv"), "--wise", str(tmp_path / "w.csv"), "--out", str(tmp_path / "o"), "--today", "2026-10-25"]

    def final_status():
        main(argv)
        return {r["invoice_id"]: r["status"] for r in csv.DictReader(open(tmp_path / "o" / "status.csv", encoding="utf-8"))}["INV-1"]

    main(argv)
    main(["confirm", "INV-1", "--out", str(tmp_path / "o"), "--today", "2026-10-25"])
    assert final_status() == "paid"
    (tmp_path / "i.csv").write_text((tmp_path / "i.csv").read_text(encoding="utf-8").replace(",1000,", ",1500,"), encoding="utf-8")
    assert final_status() == "needs_review"
    monkeypatch.setattr(cli, "CHECK_SNAPSHOT", False)
    assert final_status() == "paid", "without the snapshot check the stale confirmation would keep the changed invoice paid"


# ---- the id-shape thresholds: both sides of every boundary --------------------------------

@pytest.mark.parametrize("invoice_id,text,expected", [
    ("AB12", "AB12", True), ("AB1", "AB1", False), ("A12", "A12", False), ("A7", "A7", False), ("B2", "B2", False),
    ("ABC1234", "ABC1234", True), ("1234", "INV 1234", True), ("1234", "1234", False), ("123", "INV 123", False),
    ("123", "INV-123", False), ("12345", "Payment 12345", False), ("INV-1", "INV-1", True), ("INVOICE-7", "INVOICE-7", True),
    ("PO-5", "PO-5", False), ("PO-55", "PO-55", True), ("X-1", "X-1", False),
])
def test_id_shape_boundaries(invoice_id, text, expected):
    got = one(invoice_id=invoice_id, text=text) == SUGGESTED
    assert got is expected, (invoice_id, text)


def test_id_shape_rule_fails_if_loosened(monkeypatch):
    monkeypatch.setattr(decision, "MIN_LETTERS", 1)
    monkeypatch.setattr(decision, "MIN_DIGITS", 1)
    assert one(invoice_id="A7", text="A7") == SUGGESTED          # i.e. the boundary test above would have caught this
    assert one(invoice_id="AB1", text="AB1") == SUGGESTED


# ---- one payment naming many invoices is never paired -------------------------------------

def test_payment_naming_more_than_the_cap_goes_to_review_without_pairing():
    n = MAX_NAMED + 1
    invoices = [inv(f"INV-{100 + k}", "Zed", amount="500", issued=ISSUED, due=DUE) for k in range(n)]
    r, _ = match_all(invoices, [pay("P1", d=PAID_ON, amount="500", payer="Zed", desc=" ".join(i.id for i in invoices))], TODAY, Config())
    assert {x.status for x in r} == {"needs_review"} and f"names more than {MAX_NAMED} invoices at once" in r[0].why_not_paid


def test_naming_every_invoice_is_fast():
    import os
    import sys
    import time
    invoices = [inv(f"INV-{1000 + k}", f"Client {k}", amount="100", issued=ISSUED, due=DUE) for k in range(500)]
    text = " ".join(i.id for i in invoices)
    payments = [pay(f"P{j}", d=PAID_ON, amount="100", payer=f"Payer {j}", desc=text) for j in range(2000)]
    match_all(invoices, payments, TODAY, Config())          # warm-up: first-call imports and caches are not what we time
    t0 = time.perf_counter()
    r, _ = match_all(invoices, payments, TODAY, Config())
    budget = 3 * (2 if (sys.platform == "win32" or os.environ.get("CI")) else 1)   # slower shared runners get twice the budget
    assert time.perf_counter() - t0 < budget
    assert {x.status for x in r} == {"needs_review"}
    assert f"names more than {MAX_NAMED} invoices at once" in r[0].why_not_paid


def test_word_lists_in_the_readme_match_the_code():
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("urs", root / "scripts" / "update_readme_samples.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    block = mod.build_samples()["negative-words"]
    for entry in sorted(decision.NEGATIVE_WORDS) + list(decision.LONG_STEMS) + list(decision.SUBSTRINGS):
        assert entry in block, entry
    for phrase in decision.PHRASES:
        assert " ".join(phrase) in block, phrase
    for follow in decision._CREDIT_NOTE:
        assert follow in block
