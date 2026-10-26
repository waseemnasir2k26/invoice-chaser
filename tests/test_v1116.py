"""1.1.16: (1) the one shared normaliser maps every Unicode decimal digit to ASCII, so INV-١١٠٣ is INV-1103 at ingest, in CLI arguments and in
payment text. (2) the safety net also reads the DIGIT SKELETON of a payment: "INV-1 103" and "INV-11 03" mention invoice 1103. (3) NEAR_AMOUNT:
an unidentified payment within max(3%, 5 units) of an open invoice, same currency, holds that invoice (needs_review, no reminder in drafts/)."""
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from invoice_chaser.config import Config
from invoice_chaser.decision import canon_id, normalise
from invoice_chaser.matching import match_all
from invoice_chaser.models import Invoice, Payment
from test_v1110 import _client_eml, _cmd, _run, _write
from test_v1115 import _status

GAMMA = "INV-1103,Gamma Ltd,c@c.example,800,USD,2026-06-01,2026-07-01\n"
ARABIC_1103 = "INV-١١٠٣"  # INV-١١٠٣


def _root_drafts_for(base: Path, invoice_id: str) -> list[str]:
    return [e for e in _client_eml(base) if e.startswith("drafts/") and e.split("/", 1)[1].startswith(invoice_id)]


def _row(pid: str, amount: str, desc: str, payer: str, ref: str = "", day: str = "15-07-2026", cur: str = "USD") -> str:
    return f'"{pid}","{day}","{amount}","{cur}","{desc}","{ref}","{payer}"\n'


# ---- (1) one normaliser for every Unicode decimal digit ----------------------------------------------------------------------

def test_arabic_indic_digits_are_ascii_in_every_entry_point():
    assert canon_id(ARABIC_1103) == "INV-1103"                       # ingest and CLI arguments
    assert normalise(f"paid {ARABIC_1103}") == "paid inv-1103"       # payment text
    assert normalise("۱۱۰۳") == "1103"           # extended Arabic-Indic (Persian) digits too


def test_arabic_indic_id_in_a_payment_names_the_ascii_invoice(tmp_path, capsys):
    # at 786 (inside the 2% tolerance) the id must count exactly as the ASCII id does
    _write(tmp_path, GAMMA, [_row("T-1", "786.00", f"Payment {ARABIC_1103}", "Other Co")])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    capsys.readouterr()
    assert _status(tmp_path)["INV-1103"]["status"] == "suggested_paid"


def test_arabic_indic_id_on_the_command_line_is_the_ascii_invoice(tmp_path, capsys):
    _write(tmp_path, GAMMA, [])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    capsys.readouterr()
    assert _cmd(tmp_path, "mark-paid", ARABIC_1103, day="2026-08-21") == 0


# ---- (2) digits split by spaces, dots or dashes ------------------------------------------------------------------------------

@pytest.mark.parametrize("desc", ["Payment INV-1 103", "Payment INV-11 03", "INV-11.03 thanks", "INV-11-03 thanks"])
def test_a_split_number_at_a_non_exact_amount_holds_the_invoice(tmp_path, capsys, desc):
    _write(tmp_path, GAMMA, [_row("T-1", "786.00", desc, "Other Co")])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    capsys.readouterr()
    row = _status(tmp_path)["INV-1103"]
    assert row["status"] == "needs_review", row
    assert "a payment mentions 1103: check it" in row["why_not_paid"]
    assert _root_drafts_for(tmp_path, "INV-1103") == []


def test_the_skeleton_is_a_whole_run_not_a_fragment():
    from invoice_chaser.matching import _DIGIT_SKELETON

    assert _DIGIT_SKELETON.findall("1 103 45") == ["1 103 45"]          # the whole run, never "1 103" inside it
    assert _DIGIT_SKELETON.findall("x 1103") == []                      # no separator: nothing to collapse


# ---- (3) NEAR_AMOUNT --------------------------------------------------------------------------------------------------------

def test_a_stranger_at_786_with_no_reference_holds_the_invoice(tmp_path, capsys):
    _write(tmp_path, GAMMA, [_row("T-1", "786.00", "thanks for the work", "Some Stranger")])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    err = capsys.readouterr().err
    row = _status(tmp_path)["INV-1103"]
    assert row["status"] == "needs_review"
    assert "NEAR_AMOUNT" in err and "invoice-chaser confirm INV-1103 --payment T-1" in err
    assert "invoice-chaser reject INV-1103 T-1" in row["why_not_paid"]
    assert _root_drafts_for(tmp_path, "INV-1103") == [], "never a normal reminder in drafts/"


def test_control_a_stranger_at_400_does_not_hold_an_800_invoice(tmp_path, capsys):
    _write(tmp_path, GAMMA, [_row("T-1", "400.00", "thanks for the work", "Some Stranger")])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    err = capsys.readouterr().err
    row = _status(tmp_path)["INV-1103"]
    assert row["status"] == "overdue", row
    assert "NEAR_AMOUNT" not in err
    assert _root_drafts_for(tmp_path, "INV-1103"), "the normal reminder still goes out (the control must be able to fail)"


def test_a_payment_confirmed_to_another_invoice_does_not_hold_this_one(tmp_path, capsys):
    two = GAMMA + "INV-2001,Other Ltd,o@o.example,790,USD,2026-06-01,2026-07-01\n"
    _write(tmp_path, two, [_row("T-1", "790.00", "Payment INV-2001", "Other Ltd")])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    capsys.readouterr()
    assert _status(tmp_path)["INV-2001"]["status"] == "suggested_paid"
    assert _cmd(tmp_path, "confirm", "INV-2001", "--payment", "T-1", "--yes", day="2026-08-20") == 0
    capsys.readouterr()
    _run(tmp_path, "2026-08-21")
    capsys.readouterr()
    assert _status(tmp_path)["INV-1103"]["status"] == "overdue"
    assert _root_drafts_for(tmp_path, "INV-1103")


def test_a_payment_already_suggested_for_another_invoice_does_not_hold_this_one():
    # engine level: the 790 payment is a strong suggestion for INV-2001 (exact id), so it is not an unidentified payment for INV-1103
    invs = [
        Invoice("INV-1103", "Gamma Ltd", "c@c.example", Decimal("800"), "USD", date(2026, 6, 1), date(2026, 7, 1)),
        Invoice("INV-2001", "Other Ltd", "o@o.example", Decimal("790"), "USD", date(2026, 6, 1), date(2026, 7, 1)),
    ]
    pays = [Payment("T-1", date(2026, 7, 15), Decimal("790"), "USD", "Payment INV-2001", "Other Ltd", "wise")]
    results, _ = match_all(invs, pays, date(2026, 8, 20), Config())
    by_id = {r.invoice.id: r for r in results}
    assert by_id["INV-2001"].status == "suggested_paid"
    assert by_id["INV-1103"].status == "overdue"


def test_near_window_is_the_larger_of_3_percent_and_5_units():
    def held(outstanding: str, amount: str) -> bool:
        inv = Invoice("INV-9", "Zed Ltd", "z@z.example", Decimal(outstanding), "USD", date(2026, 6, 1), date(2026, 7, 1))
        pay = Payment("P", date(2026, 7, 15), Decimal(amount), "USD", "thanks", "Nobody", "wise")
        (r,), _ = match_all([inv], [pay], date(2026, 8, 20), Config())
        return r.status == "needs_review"

    assert held("100", "95")          # 5 units below: 5% of 100 is 5 and 3% is 3, so the 5-unit window applies
    assert not held("100", "94")      # 6 units below: outside the window
    assert held("1000", "971")        # 3% of 1000 is 30: inside
    assert not held("1000", "969")    # 31 below: outside


def test_the_near_amount_currency_must_match():
    # 1.1.17 (R9): NEAR_AMOUNT stays same-currency, but an unidentified stranger in another currency whose number is near the
    # invoice now holds it as CURRENCY_MISMATCH (a hold, never a root reminder). Before 1.1.17 this was "overdue" with a root draft.
    inv = Invoice("INV-9", "Zed Ltd", "z@z.example", Decimal("800"), "USD", date(2026, 6, 1), date(2026, 7, 1))
    pay = Payment("P", date(2026, 7, 15), Decimal("786"), "EUR", "thanks", "Nobody", "wise")
    (r,), _ = match_all([inv], [pay], date(2026, 8, 20), Config())
    assert r.status == "needs_review"
    assert r.suggestions[0].mismatch and not r.suggestions[0].near
