"""1.1.21: the PLAUSIBLE_PAYMENT gate's name and amount signals count only from the invoice's issue date on.

A repeat client's old payment (for an earlier invoice) must not hold every new invoice forever. An invoice-number signal still holds at
any date: a payment that quotes INV-1103 before it was issued is still that invoice's payment until you say otherwise."""
import csv
import json
from datetime import date
from decimal import Decimal

from invoice_chaser.cli import main
from invoice_chaser.matching import payment_window_start, plausible_payment_refs
from invoice_chaser.models import Invoice, Payment

INV_HEAD = "id,client,email,amount,currency,issued,due,status,last_reminder\n"
PAY_HEAD = "Transaction Date,Description,Amount,Currency,Status,Transaction ID\n"
TODAY = "2026-10-26"
OLD_INV = "INV-1001,Bluebird Media,pat@bluebird.example,800,USD,2026-07-15,2026-08-14,,\n"
NEW_INV = "INV-1103,Bluebird Media,pat@bluebird.example,800,USD,2026-09-01,2026-09-30,,\n"
OLD_PAY = '"2026-08-01","Payment from Bluebird Media","800","USD","Completed","PAYO-10"\n'


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


def test_repeat_client_old_payment_does_not_hold_the_new_invoice(tmp_path):
    _setup(tmp_path, OLD_INV + NEW_INV, OLD_PAY)
    _run(tmp_path)
    assert main(["confirm", "INV-1001", "--payment", "PAYO-10", "--out", str(tmp_path / "out"), "--today", TODAY]) == 0  # 08-01 is INV-1001's
    rows = _run(tmp_path)
    assert rows["INV-1001"]["status"] == "paid"
    assert rows["INV-1103"]["status"] == "overdue"
    assert [n for n in _roots(tmp_path) if n.startswith("INV-1103")] == ["INV-1103-final.eml"]


def test_a_payment_dated_before_issue_that_quotes_the_new_invoice_still_holds_it(tmp_path):
    _setup(tmp_path, NEW_INV, '"2026-08-20","Payment from Bluebird Media INV-1103","800","USD","Completed","PAYO-11"\n')
    rows = _run(tmp_path)
    assert rows["INV-1103"]["status"] != "overdue"
    assert not [n for n in _roots(tmp_path) if n.startswith("INV-1103")]


def test_window_start_is_issue_date_or_due_less_60_days():
    inv = Invoice("INV-1", "A", "a@x", Decimal("1"), "USD", date(2026, 9, 1), date(2026, 9, 30))
    assert payment_window_start(inv) == date(2026, 9, 1)
    no_issue = Invoice("INV-2", "A", "a@x", Decimal("1"), "USD", None, date(2026, 9, 30))
    assert payment_window_start(no_issue) == date(2026, 8, 1)


def test_gate_name_signal_ignores_pre_issue_payment_but_id_signal_keeps_it():
    inv = Invoice("INV-1103", "Bluebird Media", "p@b.example", Decimal("800"), "USD", date(2026, 9, 1), date(2026, 9, 30))
    old = Payment("OLD", date(2026, 8, 1), Decimal("800"), "USD", "Payment from Bluebird Media", "", "payoneer")
    quoted = Payment("Q", date(2026, 8, 20), Decimal("800"), "USD", "INV-1103 from Bluebird Media", "", "payoneer")
    assert plausible_payment_refs(inv, [old], Decimal("800")) == []
    assert [p.id for p, _ in plausible_payment_refs(inv, [quoted], Decimal("800"))] == ["Q"]
