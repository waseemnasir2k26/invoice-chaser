"""1.1.20: PLAUSIBLE_PAYMENT and REFUND_REPAYMENT, the last gate before a client reminder.

A row that could be an invoice's payment (its number in the text, the client's name near the payer or description, an amount within
the NEAR_AMOUNT window) holds that invoice as needs_review, whatever happened to the row elsewhere (consumed by another invoice, any
currency). A refund for about the amount, dated from issue to the run date, holds a suggested-paid invoice. Only a payment you
confirmed, or rejected for this invoice, is left out."""
import csv
import json
from datetime import date
from decimal import Decimal

from invoice_chaser.cli import main
from invoice_chaser.ledger import load_paid
from invoice_chaser.matching import plausible_payment_refs, refund_rows
from invoice_chaser.models import Invoice, Payment

INV_HEAD = "id,client,email,amount,currency,issued,due,status,last_reminder\n"
PAY_HEAD = "Transaction Date,Description,Amount,Currency,Status,Transaction ID\n"
TODAY = "2026-10-26"


def _setup(tmp_path, invoices, payments):
    (tmp_path / "i.csv").write_text(INV_HEAD + invoices, encoding="utf-8")
    (tmp_path / "p.csv").write_text(PAY_HEAD + payments, encoding="utf-8")
    (tmp_path / "c.json").write_text(json.dumps({"sender_email": "me@x.co"}), encoding="utf-8")


def _run(tmp_path):
    main(["run", "--invoices", str(tmp_path / "i.csv"), "--payoneer", str(tmp_path / "p.csv"), "--config", str(tmp_path / "c.json"),
          "--out", str(tmp_path / "out"), "--today", TODAY])
    return {r["invoice_id"]: r for r in csv.DictReader(open(tmp_path / "out" / "status.csv", encoding="utf-8"))}


def _roots(tmp_path):
    return sorted(p.name for p in (tmp_path / "out" / "drafts").glob("*.eml"))


def _row(desc, amount, cur="USD", date="2026-10-20", pid="PAYO-1"):
    return f'"{date}","{desc}","{amount}","{cur}","Completed","{pid}"\n'


MEDICAL = "INV-1200,Bluebird Medical,ops@bluebirdmed.example,800,USD,2026-09-01,2026-09-30,,\n"
MEDIA = "INV-1103,Bluebird Media,pat@bluebird.example,800,USD,2026-09-01,2026-09-30,,\n"
N2_PAY = _row("Payment from Bluebird Media", "800", pid="PAYO-32")


def test_n2_the_similar_client_gets_no_root_draft_and_the_payer_stays_a_suggestion(tmp_path):
    _setup(tmp_path, MEDICAL + MEDIA, N2_PAY)
    rows = _run(tmp_path)
    assert _roots(tmp_path) == []  # neither invoice gets a root reminder
    assert rows["INV-1103"]["status"] == "suggested_paid"
    assert rows["INV-1200"]["status"] == "needs_review"
    why = rows["INV-1200"]["why_not_paid"]
    assert why.startswith("PLAUSIBLE_PAYMENT:") and "PAYO-32" in why
    assert "similarity 0.9" in why and "amount 800" in why  # the reasons are listed
    assert "invoice-chaser confirm INV-1200 --payment PAYO-32" in why
    assert "invoice-chaser reject INV-1200 PAYO-32" in why


def test_a_payment_you_confirmed_to_the_other_invoice_lets_the_similar_client_be_reminded(tmp_path):
    _setup(tmp_path, MEDICAL + MEDIA, N2_PAY)
    _run(tmp_path)
    assert main(["confirm", "INV-1103", "--payment", "PAYO-32", "--out", str(tmp_path / "out"), "--today", TODAY]) == 0
    assert "INV-1103" in load_paid(tmp_path / "out" / "sent.json")
    rows = _run(tmp_path)
    assert rows["INV-1103"]["status"] == "paid"
    assert rows["INV-1200"]["status"] == "overdue"
    assert _roots(tmp_path) == ["INV-1200-final.eml"]


def test_rejecting_the_payment_for_the_similar_client_releases_its_reminder(tmp_path):
    _setup(tmp_path, MEDICAL + MEDIA, N2_PAY)
    _run(tmp_path)
    assert main(["reject", "INV-1200", "PAYO-32", "--out", str(tmp_path / "out")]) == 0
    rows = _run(tmp_path)
    assert rows["INV-1200"]["status"] == "overdue"
    assert "INV-1200-final.eml" in _roots(tmp_path)


def test_a_stranger_payment_of_another_amount_still_lets_the_reminder_through(tmp_path):
    _setup(tmp_path, MEDIA, _row("Stranger Ltd thanks for the work", "400", pid="PAYO-5"))
    rows = _run(tmp_path)
    assert rows["INV-1103"]["status"] == "overdue"
    assert _roots(tmp_path) == ["INV-1103-final.eml"]


def test_refund_then_repay_holds_the_suggested_invoice(tmp_path):
    _setup(tmp_path, MEDIA, _row("Payment from Bluebird Media", "800", pid="PAYO-24")
           + _row("Refund to Bluebird Media", "-800", date="2026-10-21", pid="PAYO-25")
           + _row("Payment from Bluebird Media", "800", date="2026-10-22", pid="PAYO-26"))
    rows = _run(tmp_path)
    assert rows["INV-1103"]["status"] == "needs_review"
    assert "refund seen on 2026-10-21 for 800.00 USD: check the repayment" in rows["INV-1103"]["why_not_paid"]
    assert _roots(tmp_path) == []
    sug = json.loads((tmp_path / "out" / "suggestions.json").read_text(encoding="utf-8"))["suggestions"]
    assert [s["kind"] for s in sug] == ["review"] and {p["id"] for p in sug[0]["payments"]} == {"PAYO-24"}


def test_a_refund_before_the_invoice_was_issued_does_not_hold_it(tmp_path):
    _setup(tmp_path, MEDIA, _row("Payment from Bluebird Media", "800", pid="PAYO-24")
           + _row("Refund to Bluebird Media", "-800", date="2026-08-01", pid="PAYO-25"))
    rows = _run(tmp_path)
    assert rows["INV-1103"]["status"] == "suggested_paid"


def test_the_gate_reads_every_row_and_skips_only_the_decided_ones():
    inv = Invoice("INV-1200", "Bluebird Medical", "o@b.example", Decimal("800"), "USD", date(2026, 9, 1), date(2026, 9, 30))
    pay = Payment("PAYO-32", date(2026, 10, 20), Decimal("800"), "USD", "Payment from Bluebird Media", "", "payoneer")
    stranger = Payment("S-1", date(2026, 10, 20), Decimal("400"), "USD", "Stranger Ltd", "", "payoneer")
    hits = plausible_payment_refs(inv, [pay, stranger], Decimal("800"))
    assert [p.id for p, _ in hits] == ["PAYO-32"]
    assert plausible_payment_refs(inv, [pay], Decimal("800"), skip={"payoneer:PAYO-32"}) == []
    assert plausible_payment_refs(inv, [stranger], Decimal("800")) == []
    refund = Payment("R-1", date(2026, 10, 21), Decimal("-800"), "USD", "Refund to Bluebird Medical", "", "payoneer")
    assert [p.id for p in refund_rows(inv, [refund], date(2026, 10, 26))] == ["R-1"]
    assert refund_rows(inv, [refund], date(2026, 8, 30)) == []  # before issue
