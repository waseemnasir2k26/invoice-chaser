from datetime import date
from decimal import Decimal

import pytest

from invoice_chaser.parsers import (
    ParseError,
    parse_amount,
    parse_date,
    parse_generic,
    parse_payoneer,
    parse_wise,
    read_invoices,
)


def test_wise_sample(examples):
    ps = parse_wise(examples / "wise-statement.csv")
    assert [p.id for p in ps] == ["TRANSFER-1100000001", "TRANSFER-1100000002", "TRANSFER-1100000004"]  # card spend skipped
    p = ps[0]
    assert p.date == date(2026, 10, 3) and p.amount == Decimal("1200.00") and p.currency == "USD"
    assert "INV-1001" in p.description and p.payer == "Acme Studio Ltd"


def test_payoneer_sample_skips_pending_and_withdrawals(examples):
    ps = parse_payoneer(examples / "payoneer-transactions.csv")
    assert len(ps) == 1
    assert ps[0].amount == Decimal("786.00") and ps[0].payer == "Bluebird Media"


def test_payoneer_tolerant_headers(tmp_path):
    f = tmp_path / "p.csv"
    f.write_text("TRANSACTION_DATE,description,AMOUNT,CURRENCY\n2026-10-01,Payment from X Corp,10.5,eur\n", encoding="utf-8")
    ps = parse_payoneer(f)
    assert ps[0].currency == "EUR" and ps[0].amount == Decimal("10.5")


def test_generic_with_mapping(examples):
    import json
    mapping = json.loads((examples / "bank-mapping.json").read_text())
    ps = parse_generic(examples / "bank-export.csv", mapping)
    assert len(ps) == 1  # debit-only row has no credit
    assert ps[0].date == date(2026, 10, 20) and ps[0].payer == "M. Okafor"


def test_generic_default_currency_and_signed_amount(tmp_path):
    f = tmp_path / "b.csv"
    f.write_text("When;Amt;Memo\n03.10.2026;1.234,50;INV-9\n04.10.2026;-20,00;fee\n", encoding="utf-8")
    ps = parse_generic(f, {"date": "When", "amount": "Amt", "description": "Memo", "default_currency": "pkr", "date_format": "%d.%m.%Y"})
    assert len(ps) == 1 and ps[0].amount == Decimal("1234.50") and ps[0].currency == "PKR"


def test_generic_missing_mapped_column_is_clear(tmp_path):
    f = tmp_path / "b.csv"
    f.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(ParseError, match="not found"):
        parse_generic(f, {"date": "Nope", "amount": "b", "default_currency": "USD"})


def test_wise_missing_column_lists_headers(tmp_path):
    f = tmp_path / "w.csv"
    f.write_text("foo,bar\n1,2\n", encoding="utf-8")
    with pytest.raises(ParseError, match="found headers"):
        parse_wise(f)


@pytest.mark.parametrize("s,v", [("1,200.00", "1200.00"), ("$1,200", "1200"), ("1.234,56", "1234.56"),
                                 ("12,50", "12.50"), ("(100.00)", "-100.00"), ("-5", "-5")])
def test_parse_amount(s, v):
    assert parse_amount(s) == Decimal(v)


@pytest.mark.parametrize("s,d", [("2026-10-03", date(2026, 10, 3)), ("03-10-2026", date(2026, 10, 3)),
                                 ("13/10/2026", date(2026, 10, 13)), ("2026-10-03 14:22:01", date(2026, 10, 3)),
                                 ("3 Oct 2026", date(2026, 10, 3))])
def test_parse_date(s, d):
    assert parse_date(s) == d


def test_invoices_sample_and_errors(examples, tmp_path):
    invs = read_invoices(examples / "invoices.csv")
    assert len(invs) == 9 and invs[8].status == "paid" and invs[0].contact == "Sarah Malik"
    dup = tmp_path / "d.csv"
    dup.write_text("id,client,email,amount,currency,issued,due\nA,x,,1,USD,2026-01-01,2026-02-01\nA,y,,1,USD,2026-01-01,2026-02-01\n")
    with pytest.raises(ParseError, match="duplicate"):
        read_invoices(dup)
    bad = tmp_path / "b.csv"
    bad.write_text("id,client,email,amount,currency,issued,due\nA,x,,abc,USD,2026-01-01,2026-02-01\n")
    with pytest.raises(ParseError, match="row 2"):
        read_invoices(bad)
