"""1.1.12: a payment whose text names a DIFFERENT known invoice id is refused by `confirm INV --payment ID` and `rebind`, unless
--force; with --force it is recorded and a warning is printed. One rule (`cli._name_clash`) for both commands."""
import json

from test_v1110 import _cmd, _run, _write

from invoice_chaser.ledger import load_paid, load_payment_rows

INVOICES = ("INV-101,Alpha Co,a@a.example,200,USD,2026-06-01,2026-07-01\n"
            "INV-102,Gamma Ltd,c@c.example,300,USD,2026-06-01,2026-07-01\n")
NAMES_OTHER = '"TRANSFER-1","05-10-2026","300.00","USD","Payment for INV-101","INV-101","Gamma Ltd"\n'
NAMES_OWN = '"TRANSFER-2","05-10-2026","200.00","USD","Payment for INV-101","INV-101","Alpha Co"\n'


def test_confirm_refuses_a_payment_that_names_another_invoice(tmp_path, capsys):
    _write(tmp_path, INVOICES, [NAMES_OTHER])
    _run(tmp_path, "2026-10-20")
    capsys.readouterr()
    assert _cmd(tmp_path, "confirm", "INV-102", "--payment", "TRANSFER-1", day="2026-10-20") == 2
    assert "TRANSFER-1 names INV-101, not INV-102: pass --force if you're sure" in capsys.readouterr().err
    assert "INV-102" not in load_paid(tmp_path / "out" / "sent.json")  # nothing recorded


def test_confirm_force_records_it_and_warns(tmp_path, capsys):
    _write(tmp_path, INVOICES, [NAMES_OTHER])
    _run(tmp_path, "2026-10-20")
    capsys.readouterr()
    assert _cmd(tmp_path, "confirm", "INV-102", "--payment", "TRANSFER-1", "--force", day="2026-10-20") == 0
    assert "WARNING: TRANSFER-1 names INV-101, not INV-102: recorded because of --force" in capsys.readouterr().err
    assert "INV-102" in load_paid(tmp_path / "out" / "sent.json")


def test_confirm_of_the_payment_that_names_its_own_invoice_is_not_refused(tmp_path):
    _write(tmp_path, INVOICES, [NAMES_OWN])
    _run(tmp_path, "2026-10-20")
    assert _cmd(tmp_path, "confirm", "INV-101", "--payment", "TRANSFER-2", day="2026-10-20") == 0


def test_rebind_refuses_a_payment_that_names_another_invoice_until_force(tmp_path, capsys):
    # hash ids (no bank transaction id): the bank re-words the same payment, and the new wording names INV-101
    _write(tmp_path, INVOICES, ['"","05-10-2026","300.00","USD","Payment for INV-102","INV-102","Gamma Ltd"\n'])
    _run(tmp_path, "2026-10-20")
    suggested = json.loads((tmp_path / "out" / "suggestions.json").read_text(encoding="utf-8"))["suggestions"][0]["payments"][0]["key"]
    assert _cmd(tmp_path, "confirm", "INV-102", "--payment", suggested, day="2026-10-20") == 0
    old = load_payment_rows(tmp_path / "out" / "sent.json")[0]["key"]
    _write(tmp_path, INVOICES, ['"","05-10-2026","300.00","USD","Payment for INV-101","INV-101","Gamma Ltd"\n'])
    _run(tmp_path, "2026-10-21")
    new = json.loads((tmp_path / "out" / "orphans.json").read_text(encoding="utf-8"))["orphans"][0]["candidates"][0]["key"]
    capsys.readouterr()
    assert _cmd(tmp_path, "rebind", old, new, day="2026-10-21") == 2
    assert "it names INV-101, not INV-102" in capsys.readouterr().err
    assert _cmd(tmp_path, "rebind", old, new, "--force", day="2026-10-21") == 0
    assert "WARNING" in capsys.readouterr().err
