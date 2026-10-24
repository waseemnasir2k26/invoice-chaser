"""parse_amount must never silently corrupt a number: it reads it exactly or refuses with a reason."""
import csv
from decimal import Decimal

import pytest

from invoice_chaser.cli import main
from invoice_chaser.parsers import ParseError, ParseOptions, parse_amount, read_invoices

D = Decimal

# (text, decimal_comma hint, expected value or None for "must be refused")
CASES = [
    # plain and dotted
    ("1200", None, "1200"), ("1200.00", None, "1200.00"), ("12.5", None, "12.5"), ("0.5", None, "0.5"),
    (".5", None, "0.5"), ("1234.500", None, "1234.500"), ("0.500", None, "0.500"),
    # thousands separators
    ("1,200.00", None, "1200.00"), ("1,234,567", None, "1234567"), ("1.234,56", None, "1234.56"),
    ("1.234.567", None, "1234567"), ("1 234,50", None, "1234.50"), ("1'234.50", None, "1234.50"),
    ("1 234,50", None, "1234.50"), ("1 234.50", None, "1234.50"),
    # decimal comma, unambiguous
    ("12,50", None, "12.50"), ("1,5", None, "1.5"), ("0,5", None, "0.5"),
    # ambiguous without a hint: refused
    ("1,234", None, None), ("1.500", None, None), ("$1,200", None, None), ("12,500", None, None),
    # with a hint
    ("1,234", False, "1234"), ("1.500", False, "1.500"), ("1,234", True, "1.234"), ("1.500", True, "1500"),
    ("1.234,56", True, "1234.56"), ("1,234.56", False, "1234.56"), ("1,5", True, "1.5"),
    ("1,5", False, None), ("0,5", False, None), ("1.234,56", False, None), ("1,234.56", True, None),
    ("1200.00", True, None),
    # sign, currency, parentheses
    ("-5", None, "-5"), ("+5", None, "5"), ("(100.00)", None, "-100.00"), ("−5", None, "-5"),
    ("$1,200.00", None, "1200.00"), ("USD 1,200.00", None, "1200.00"), ("1,200.00 EUR", None, "1200.00"),
    ("€ 12,50", None, "12.50"), ("-$5.00", None, "-5.00"), ("GBP-5", None, "-5"),
    # the corruptions this parser used to produce
    ("1e400", None, None), ("1e3", None, None), ("1000 (2 of 3)", None, None), ("1000 USD (2 of 3)", None, None),
    ("12abc", None, None), ("abc", None, None), ("", None, None), ("-", None, None), ("$", None, None),
    ("1,23,456.00", None, None), ("12.5.3", None, None), ("1,234.56.7", None, None), ("1 23", None, None),
    ("1,2,3", None, None), ("1..5", None, None), ("5-3", None, None), ("NaN", None, None), ("Infinity", None, None),
    ("0,500", False, None), ("0.500", True, None), ("99999999999999", None, None), ("1 234 56", None, None),
    ("1,234 567", None, None), ("1 234,567.8", None, None),
]


@pytest.mark.parametrize("text,hint,expected", CASES, ids=[f"{t!r}-{h}" for t, h, _ in CASES])
def test_parse_amount_table(text, hint, expected):
    if expected is None:
        with pytest.raises(ParseError):
            parse_amount(text, hint)
    else:
        assert parse_amount(text, hint) == D(expected)


def test_ambiguous_message_says_how_to_fix():
    with pytest.raises(ParseError, match="decimal_comma"):
        parse_amount("1,234")


def test_bad_amount_row_is_skipped_with_the_reason(tmp_path):
    f = tmp_path / "i.csv"
    f.write_text("id,client,email,amount,currency,issued,due\n"
                 "A,x,a@b.co,1e400,USD,2026-01-01,2026-02-01\n"
                 "B,x,a@b.co,1000 (2 of 3),USD,2026-01-01,2026-02-01\n"
                 "C,x,a@b.co,\"1,5\",USD,2026-01-01,2026-02-01\n"
                 "D,x,a@b.co,\"1,234\",USD,2026-01-01,2026-02-01\n"
                 "E,x,a@b.co,500,USD,2026-01-01,2026-02-01\n", encoding="utf-8")
    o = ParseOptions()
    invs = read_invoices(f, o)
    assert [(i.id, i.amount) for i in invs] == [("C", D("1.5")), ("E", D("500"))]
    text = " ".join(o.warnings)
    assert "row 2" in text and "row 3" in text and "row 5" in text and "ambiguous amount" in text and "row skipped" in text
    assert {i.id: i.amount for i in read_invoices(f, ParseOptions(decimal_comma=False)) if i.id == "D"} == {"D": D("1234")}
    with pytest.raises(ParseError):
        read_invoices(f, ParseOptions(strict=True))


def test_decimal_comma_flag_end_to_end(tmp_path):
    (tmp_path / "i.csv").write_text("id,client,email,amount,currency,issued,due\n"
                                    "INV-7,Acme,a@b.co,\"1.500\",EUR,2026-09-01,2026-10-01\n", encoding="utf-8")
    (tmp_path / "w.csv").write_text('"TransferWise ID","Date","Amount","Currency","Description","Payment Reference","Payer Name"\n'
                                    '"W1","05-10-2026","1500.00","EUR","INV-7","INV-7","Acme"\n', encoding="utf-8")
    base = ["run", "--invoices", str(tmp_path / "i.csv"), "--wise", str(tmp_path / "w.csv"), "--today", "2026-10-25"]
    assert main(base + ["--out", str(tmp_path / "a")]) == 2              # refused as ambiguous: no invoice could be read
    assert not (tmp_path / "a" / "status.csv").exists()
    assert main(base + ["--out", str(tmp_path / "b"), "--decimal-comma"]) == 0
    rows = list(csv.DictReader(open(tmp_path / "b" / "status.csv", encoding="utf-8")))
    assert rows[0]["status"] == "suggested_paid" and rows[0]["amount"] == "1500"
