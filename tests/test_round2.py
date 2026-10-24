import csv
import json
from datetime import date
from pathlib import Path

import pytest

from conftest import pay
from invoice_chaser.cli import main
from invoice_chaser.config import load_config
from invoice_chaser.dedupe import dedupe_payments
from invoice_chaser.drafts import write_drafts
from invoice_chaser.ledger import load_ledger, load_paid
from invoice_chaser.parsers import ParseError, ParseOptions, norm_currency, parse_date, parse_generic, parse_wise, read_invoices

INV_HEAD = "id,client,email,amount,currency,issued,due\n"
WISE_HEAD = ('"TransferWise ID","Date","Amount","Currency","Description","Payment Reference","Payer Name"\n')


def run(tmp_path, invoices, wise=None, extra=(), config=None):
    (tmp_path / "i.csv").write_text(invoices, encoding="utf-8")
    argv = ["run", "--invoices", str(tmp_path / "i.csv"), "--out", str(tmp_path / "out"), "--today", "2026-10-25"]
    if wise is not None:
        (tmp_path / "w.csv").write_text(wise, encoding="utf-8")
        argv += ["--wise", str(tmp_path / "w.csv")]
    if config is not None:
        (tmp_path / "c.json").write_text(json.dumps(config), encoding="utf-8")
        argv += ["--config", str(tmp_path / "c.json")]
    code = main(argv + list(extra))
    rows = {r["invoice_id"]: r for r in csv.DictReader(open(tmp_path / "out" / "status.csv", encoding="utf-8"))} if code == 0 else {}
    return code, rows


# ---- M1 duplicates -----------------------------------------------------------------

def test_same_transfer_id_twice_is_dropped_with_warning():
    w = []
    out = dedupe_payments([pay("T1"), pay("T1"), pay("T2", amount="5")], w.append)
    assert [p.id for p in out] == ["T1", "T2"] and "duplicate payment row dropped" in w[0]


def test_same_id_different_payment_is_kept_but_suspect():
    w = []
    out = dedupe_payments([pay("T1", amount="10"), pay("T1", amount="20")], w.append)
    assert len(out) == 2 and all(p.suspect for p in out) and w


def test_cross_source_duplicate_flagged_within_one_day():
    w = []
    a = pay("W1", payer="Acme Studio", source="wise")
    b = pay("B1", d=date(2026, 10, 6), payer="Acme Studio", source="bank")
    c = pay("B2", d=date(2026, 10, 9), payer="Acme Studio", source="bank")      # 4 days apart: a real second payment
    dedupe_payments([a, b, c], w.append)
    assert a.suspect and b.suspect and not c.suspect and "possible duplicate across sources" in w[0]


def test_two_real_payments_same_day_same_source_not_flagged():
    a, b = pay("T1", payer="Acme"), pay("T2", payer="Acme")
    dedupe_payments([a, b], lambda m: None)
    assert not a.suspect and not b.suspect


def test_duplicate_wise_and_bank_row_never_marked_paid_end_to_end(tmp_path):
    wise = WISE_HEAD + '"W1","05-10-2026","1000.00","USD","Received money from Acme Studio","","Acme Studio"\n'
    bank = tmp_path / "b.csv"
    bank.write_text("When,Memo,Credit,Ccy,From\n06/10/2026,Acme Studio,1000.00,USD,Acme Studio\n", encoding="utf-8")
    mapping = tmp_path / "m.json"
    mapping.write_text(json.dumps({"date": "When", "credit": "Credit", "currency": "Ccy", "description": "Memo", "payer": "From"}))
    code, rows = run(tmp_path, INV_HEAD + "INV-1,Acme Studio,a@b.co,1000,USD,2026-09-01,2026-10-01\n", wise,
                     ["--bank", str(bank), "--mapping", str(mapping)])
    assert code == 0 and rows["INV-1"]["status"] == "needs_review"
    assert "may be the same payment" in rows["INV-1"]["why_not_paid"]
    assert "possible duplicate across sources" in (tmp_path / "out" / "review.html").read_text(encoding="utf-8")


# ---- M2 dates ----------------------------------------------------------------------

def test_date_order_option_changes_reading():
    assert parse_date("03/04/2026") == date(2026, 4, 3)                 # auto: day-first
    assert parse_date("03/04/2026", order="mdy") == date(2026, 3, 4)
    assert parse_date("2026-04-03", order="mdy") == date(2026, 4, 3)    # ISO is always safe
    with pytest.raises(ParseError):
        parse_date("03/04/2026", order="ymd")


def test_ambiguous_dates_warn_only_when_every_row_is_ambiguous(tmp_path):
    f = tmp_path / "w.csv"
    f.write_text(WISE_HEAD + '"A","03/04/2026","10","USD","x","",""\n"B","05/06/2026","10","USD","x","",""\n', encoding="utf-8")
    o = ParseOptions()
    parse_wise(f, o)
    assert any("day-first or month-first" in w for w in o.warnings)
    f.write_text(WISE_HEAD + '"A","03/04/2026","10","USD","x","",""\n"B","25/06/2026","10","USD","x","",""\n', encoding="utf-8")
    o = ParseOptions()
    parse_wise(f, o)
    assert not o.warnings
    o = ParseOptions(date_order="dmy")
    f.write_text(WISE_HEAD + '"A","03/04/2026","10","USD","x","",""\n', encoding="utf-8")
    parse_wise(f, o)
    assert not o.warnings


def test_early_payment_listed_and_invoice_held(tmp_path):
    # invoice issued 3 April; the payment row 03/04/2026 read month-first is 4 March: before issue
    inv = INV_HEAD + "INV-1,Acme Studio,a@b.co,1000,USD,2026-04-03,2026-05-03\n"
    wise = WISE_HEAD + '"W1","03/04/2026","1000.00","USD","Acme Studio INV-1","INV-1","Acme Studio"\n'
    code, rows = run(tmp_path, inv, wise)
    assert rows["INV-1"]["status"] == "suggested_paid"                    # day-first default: fine
    code, rows = run(tmp_path, inv, wise, ["--date-format", "mdy"])
    assert rows["INV-1"]["status"] == "needs_review" and rows["INV-1"]["draft_file"] == ""
    html = (tmp_path / "out" / "review.html").read_text(encoding="utf-8")
    assert "check date format" in html and "before the invoice was issued" in html and "W1" in html


# ---- M3 currency -------------------------------------------------------------------

def test_currency_normalisation():
    o = ParseOptions()
    assert norm_currency("usd ", o, "f") == "USD" and norm_currency("€", o, "f") == "EUR" and norm_currency("£", o, "f") == "GBP"
    assert not o.warnings
    assert norm_currency("$", o, "f.csv") == "$" and "ambiguous" in o.warnings[0]
    assert norm_currency("$", ParseOptions(default_currency="USD"), "f") == "USD"
    o2 = ParseOptions()
    assert norm_currency("DOLLARS1", o2, "f.csv") == "DOLLARS1" and "not an ISO code" in o2.warnings[0]


def test_dollar_sign_with_default_currency_matches(tmp_path):
    inv = INV_HEAD + 'INV-1,Acme Studio,a@b.co,1000,$,2026-09-01,2026-10-01\n'
    wise = WISE_HEAD + '"W1","05-10-2026","1000.00","USD","Acme Studio","INV-1","Acme Studio"\n'
    code, rows = run(tmp_path, inv, wise)
    assert rows["INV-1"]["status"] == "overdue"                           # "$" alone never silently becomes USD
    assert "ambiguous" in (tmp_path / "out" / "review.html").read_text(encoding="utf-8")
    code, rows = run(tmp_path, inv, wise, config={"default_currency": "USD", "sender_email": "me@x.co"})
    assert rows["INV-1"]["status"] == "suggested_paid"


def test_default_currency_validated(tmp_path):
    (tmp_path / "c.json").write_text('{"default_currency": "dollars"}')
    with pytest.raises(ValueError):
        load_config(tmp_path / "c.json")


# ---- refunds, encodings ------------------------------------------------------------

def test_negative_rows_warned_and_listed(tmp_path, capsys):
    wise = WISE_HEAD + '"R1","05-10-2026","-250.00","USD","Refund to Acme","",""\n'
    code, rows = run(tmp_path, INV_HEAD + "INV-1,Acme,a@b.co,1000,USD,2026-09-01,2026-10-01\n", wise)
    assert code == 0
    assert "1 negative row(s) ignored" in capsys.readouterr().err
    html = (tmp_path / "out" / "review.html").read_text(encoding="utf-8")
    assert "Negative rows ignored (1)" in html and "Refund to Acme" in html


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-16-le"])
def test_bom_and_utf16_files(tmp_path, encoding):
    f = tmp_path / "w.csv"
    text = WISE_HEAD + '"W1","05-10-2026","10.00","EUR","Zoë Müller","",""\n'
    raw = text.encode(encoding)
    if encoding == "utf-16-le":
        pass  # BOM-less UTF-16
    f.write_bytes(raw)
    (p,) = parse_wise(f)
    assert p.amount == 10 and p.currency == "EUR" and "Zoë" in p.description


def test_cp1252_fallback(tmp_path):
    f = tmp_path / "w.csv"
    f.write_bytes((WISE_HEAD + '"W1","05-10-2026","10.00","EUR","Café","",""\n').encode("cp1252"))
    (p,) = parse_wise(f)
    assert "Caf" in p.description


def test_json_files_with_bom_are_read(tmp_path):
    (tmp_path / "c.json").write_bytes(b"\xef\xbb\xbf" + b'{"sender_email": "me@x.co"}')
    assert load_config(tmp_path / "c.json").sender_email == "me@x.co"
    (tmp_path / "m.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"date": "When", "credit": "Credit", "default_currency": "USD"}).encode())
    (tmp_path / "b.csv").write_text("When,Credit\n2026-10-01,5\n", encoding="utf-8")
    mapping = json.loads((tmp_path / "m.json").read_text(encoding="utf-8-sig"))
    assert len(parse_generic(tmp_path / "b.csv", mapping)) == 1
    inv = tmp_path / "i.csv"
    inv.write_text(INV_HEAD + "A,x,a@b.co,1,USD,2026-01-01,2026-02-01\n", encoding="utf-8")
    out = tmp_path / "o"
    assert main(["mark-paid", "A", "--out", str(out), "--force", "--ledger", str(tmp_path / "l.json")]) == 0
    (tmp_path / "l.json").write_bytes(b"\xef\xbb\xbf" + (tmp_path / "l.json").read_bytes())
    assert "A" in load_paid(tmp_path / "l.json")


# ---- bad rows ----------------------------------------------------------------------

def test_bad_invoice_row_is_skipped_with_warning_and_strict_aborts(tmp_path, capsys):
    inv = INV_HEAD + "A,Acme,a@b.co,abc,USD,2026-09-01,2026-10-01\nB,Beta,b@b.co,100,USD,2026-09-01,2026-10-01\n"
    code, rows = run(tmp_path, inv)
    assert code == 0 and list(rows) == ["B"]
    assert "row 2" in (tmp_path / "out" / "review.html").read_text(encoding="utf-8")
    assert "row skipped" in capsys.readouterr().err
    code, _ = run(tmp_path, inv, extra=["--strict"])
    assert code == 2 and "row 2" in capsys.readouterr().err


def test_bad_payment_row_skipped(tmp_path):
    wise = WISE_HEAD + '"W1","not a date","10","USD","x","",""\n"W2","05-10-2026","10","USD","x","",""\n'
    o = ParseOptions()
    f = tmp_path / "w.csv"
    f.write_text(wise, encoding="utf-8")
    assert [p.id for p in parse_wise(f, o)] == ["W2"] and any("row skipped" in w for w in o.warnings)
    with pytest.raises(ParseError):
        parse_wise(f, ParseOptions(strict=True))


# ---- drafts edited by the user -----------------------------------------------------

def _item(inv="A", body="b"):
    return dict(invoice_id=inv, stage="nudge", to="a@b.co", from_name="Me", from_email="me@b.co", subject="s", body=body)


def test_edited_draft_survives_rerun(tmp_path):
    write_drafts([_item(body="first")], tmp_path)
    f = tmp_path / "drafts" / "A-nudge.eml"
    f.write_text(f.read_text(encoding="utf-8") + "\nMy personal addition.\n", encoding="utf-8")
    edited = f.read_bytes()
    warns = []
    write_drafts([_item(body="second")], tmp_path, warns)
    assert f.read_bytes() == edited                                       # untouched
    new = tmp_path / "drafts" / "A-nudge.new.eml"
    assert new.exists() and "second" in new.read_text(encoding="utf-8")
    assert "kept" in warns[0] and "A-nudge.new.eml" in warns[0]
    # next run: the user's file is now unknown to us, so it is still never touched; the unedited .new is replaced
    write_drafts([_item(body="third")], tmp_path, [])
    assert f.read_bytes() == edited
    texts = [p.read_text(encoding="utf-8") for p in (tmp_path / "drafts").glob("*.eml") if p.read_bytes() != edited]
    assert len(texts) == 1 and "third" in texts[0]                        # fresh draft written beside it, nothing lost


def test_unedited_draft_is_replaced_not_duplicated(tmp_path):
    write_drafts([_item(body="first")], tmp_path)
    write_drafts([_item(body="second")], tmp_path)
    files = sorted(p.name for p in (tmp_path / "drafts").glob("*.eml"))
    assert files == ["A-nudge.eml"] and "second" in (tmp_path / "drafts" / "A-nudge.eml").read_text(encoding="utf-8")
    entry = json.loads((tmp_path / "drafts.json").read_text(encoding="utf-8"))[0]
    assert len(entry["sha256"]) == 64


def test_edit_survives_full_cli_rerun(tmp_path):
    out = tmp_path / "out"
    main(["demo", "--keep-ledger", "--out", str(out)])
    f = out / "drafts" / "INV-1004-nudge.eml"
    f.write_text("MY OWN WORDS", encoding="utf-8")
    main(["demo", "--keep-ledger", "--out", str(out)])
    assert f.read_text(encoding="utf-8") == "MY OWN WORDS"
    assert (out / "drafts" / "INV-1004-nudge.new.eml").exists()
    assert "was edited by you and was kept" in (out / "review.html").read_text(encoding="utf-8")


# ---- ledger commands ---------------------------------------------------------------

def test_mark_sent_rejects_future_dates_and_needs_status_or_force(tmp_path, capsys):
    out = tmp_path / "out"
    assert main(["mark-sent", "INV-1", "--stage", "nudge", "--out", str(out)]) == 2          # no status.csv yet
    assert "not found" in capsys.readouterr().err
    assert main(["mark-sent", "INV-1", "--stage", "nudge", "--out", str(out), "--force", "--date", "2026-10-01",
                 "--today", "2026-10-25"]) == 0
    main(["demo", "--keep-ledger", "--out", str(out)])
    assert main(["mark-sent", "INV-1004", "--stage", "nudge", "--out", str(out), "--date", "2026-10-26",
                 "--today", "2026-10-25"]) == 2
    assert "future" in capsys.readouterr().err


def test_mark_sent_ids_are_case_insensitive_and_unknown_refused(tmp_path, capsys):
    out = tmp_path / "out"
    main(["demo", "--keep-ledger", "--out", str(out)])
    assert main(["mark-sent", " inv-1004 ", "--stage", "nudge", "--out", str(out), "--date", "2026-10-20", "--today", "2026-10-25"]) == 0
    assert list(load_ledger(out / "sent.json")) == ["INV-1004"]           # stored under the real id
    assert main(["mark-sent", "INV-9999", "--stage", "nudge", "--out", str(out), "--today", "2026-10-25"]) == 2
    assert "unknown invoice id" in capsys.readouterr().err
    assert main(["mark-sent", "INV-9999", "--stage", "nudge", "--out", str(out), "--force", "--today", "2026-10-25"]) == 0
    assert "INV-9999" in load_ledger(out / "sent.json")


def test_ledger_ids_match_case_insensitively_on_run(tmp_path):
    out = tmp_path / "out"
    main(["demo", "--keep-ledger", "--out", str(out)])
    (out / "sent.json").write_text(json.dumps({"sent": {"inv-1004": [{"stage": "nudge", "date": "2026-10-25"}]}, "paid": {}}))
    main(["demo", "--keep-ledger", "--out", str(out)])
    assert not (out / "drafts" / "INV-1004-nudge.eml").exists()


def test_mark_paid_flow_and_why_not_paid_and_html_command(tmp_path):
    out = tmp_path / "out"
    main(["demo", "--keep-ledger", "--out", str(out)])
    rows = {r["invoice_id"]: r for r in csv.DictReader(open(out / "status.csv", encoding="utf-8"))}
    r = rows["INV-1006"]
    assert r["status"] == "needs_review" and "is not exactly 'Lumen Labs'" in r["why_not_paid"]
    assert rows["INV-1004"]["why_not_paid"] and "waiting for your confirm" in rows["INV-1001"]["why_not_paid"]
    html = (out / "review.html").read_text(encoding="utf-8")
    assert "invoice-chaser mark-paid INV-1006" in html
    assert main(["mark-paid", "inv-1006", "--out", str(out), "--note", "checked bank", "--today", "2026-10-25"]) == 0
    assert load_paid(out / "sent.json")["INV-1006"]["note"] == "checked bank"
    main(["demo", "--keep-ledger", "--out", str(out)])
    rows = {r["invoice_id"]: r for r in csv.DictReader(open(out / "status.csv", encoding="utf-8"))}
    assert rows["INV-1006"]["status"] == "paid" and rows["INV-1006"]["outstanding"] == "0.00"
    assert "confirmed by you" in rows["INV-1006"]["reason"]
    assert not list((out / "drafts").glob("INV-1006*"))


def test_mark_paid_command_is_escaped_in_html():
    from invoice_chaser.report import _markpaid
    html = _markpaid("A'<b>&", "my out")
    assert "<b>" not in html and "&lt;b&gt;" in html


def test_mark_paid_unknown_id_refused(tmp_path):
    out = tmp_path / "out"
    main(["demo", "--keep-ledger", "--out", str(out)])
    assert main(["mark-paid", "NOPE", "--out", str(out)]) == 2
    assert not Path(out / "sent.json").exists()
