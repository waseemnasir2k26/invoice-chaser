"""1.1.15: (1) every Unicode dash, space-like and invisible character is normalised before ANY invoice-id matching, and invoice ids are
stored in that form (INV–1103 names INV-1103). (2) SAFETY NET: a payment carrying an active invoice's number as a whole token,
where the strict id rule does not name that invoice, holds that invoice (needs_review, never a suggestion, no client reminder).
(3) a blank or e-mail contact greets "there"; a missing sender is CRITICAL NO_SENDER on every run and drafts are still written."""
import csv
import unicodedata
from pathlib import Path

import pytest

from invoice_chaser.cli import main
from invoice_chaser.decision import IdIndex, canon_id, normalise
from invoice_chaser.models import Invoice, InvoiceResult
from invoice_chaser.parsers import read_invoices
from invoice_chaser.templating import first_name
from test_v1110 import _client_eml, _cmd, _run, _write
from test_v118 import body_of, seed_first_seen

GAMMA = "INV-1103,Gamma Ltd,c@c.example,300,USD,2026-06-01,2026-07-01\n"


def _status(base: Path) -> dict[str, dict]:
    with open(base / "out" / "status.csv", newline="", encoding="utf-8") as fh:
        return {r["invoice_id"]: r for r in csv.DictReader(fh)}


def _eml_text(base: Path, name: str) -> str:
    return (base / "out" / "drafts" / name).read_text(encoding="utf-8")


# ---- (1) Unicode dashes, spaces and invisible characters -----------------------------------------------------------------------

DASHES = sorted({chr(c) for c in range(0x110000) if unicodedata.category(chr(c)) == "Pd"}
                | set("−﹘﹣－") | {chr(c) for c in range(0x2010, 0x2016)})
SPACES = sorted({chr(c) for c in range(0x110000) if unicodedata.category(chr(c)) == "Zs"})
INVISIBLE = sorted({chr(c) for c in range(0x10000) if unicodedata.category(chr(c)) == "Cf"})


@pytest.mark.parametrize("dash", DASHES, ids=[f"U+{ord(d):04X}" for d in DASHES])
def test_every_unicode_dash_names_the_hyphen_invoice(dash):
    assert IdIndex(["INV-1103"]).find(f"Payment INV{dash}1103") == {"INV-1103"}


@pytest.mark.parametrize("space", SPACES, ids=[f"U+{ord(s):04X}" for s in SPACES])
def test_every_space_like_character_is_a_space(space):
    assert IdIndex(["INV-1103"]).find(f"Payment INV{space}-{space}1103") == {"INV-1103"}
    assert normalise(f"a{space}b") == "a b"


@pytest.mark.parametrize("ch", INVISIBLE, ids=[f"U+{ord(c):04X}" for c in INVISIBLE])
def test_every_invisible_character_is_stripped(ch):
    assert IdIndex(["INV-1103"]).find(f"INV{ch}-1103") == {"INV-1103"}
    assert normalise(f"a{ch}b") == "ab"


def test_the_two_cases_from_the_report_name_the_invoice():
    ix = IdIndex(["INV-1103", "INV-1104"])
    assert ix.find("Payment INV–1103") == {"INV-1103"}      # en dash
    assert ix.find("INV‑1104") == {"INV-1104"}              # non-breaking hyphen


def test_nfkc_fullwidth_and_casefold_are_applied_before_matching():
    ix = IdIndex(["INV-1103"])
    assert ix.find("ＩＮＶ－１１０３") == {"INV-1103"}          # fullwidth letters, digits and hyphen
    assert ix.find("inv–1103") == {"INV-1103"}              # lower case + en dash
    assert normalise("INV–1103") == "inv-1103"


def test_invoice_ids_with_unicode_dashes_match_ascii_payments():
    ix = IdIndex(["INV–1103"])                              # the id itself carries an en dash
    assert ix.find("Payment INV-1103") == {"INV–1103"}


def test_canon_id_stores_one_form_for_ids():
    assert canon_id("INV–1103") == "INV-1103"
    assert canon_id("  INV  1103 ") == "INV 1103"
    assert canon_id("INV​-1103") == "INV-1103"


def test_ingest_stores_the_canonical_id(tmp_path):
    f = tmp_path / "i.csv"
    f.write_text("id,client,email,amount,currency,issued,due\nINV–1103,Gamma Ltd,c@c.example,300,USD,2026-06-01,2026-07-01\n",
                 encoding="utf-8")
    assert [i.id for i in read_invoices(f)] == ["INV-1103"]


def test_en_dash_payment_for_a_partial_amount_holds_the_invoice_and_no_reminder_goes_out(tmp_path, capsys):
    # the report's case: a payment naming INV–1103 (en dash) for part of the amount used to leave the invoice overdue and chased
    _write(tmp_path, GAMMA, ['"TRANSFER-1","15-07-2026","150.00","USD","Payment INV–1103","INV–1103","Other Payer Co"\n'])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    capsys.readouterr()
    assert _status(tmp_path)["INV-1103"]["status"] in ("partial_suggested", "needs_review")
    assert not any("INV-1103" in e for e in _client_eml(tmp_path))


def test_non_breaking_hyphen_payment_is_suggested_not_chased(tmp_path, capsys):
    _write(tmp_path, GAMMA, ['"TRANSFER-1","15-07-2026","300.00","USD","Payment INV‑1103","INV‑1103","Other Payer Co"\n'])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    capsys.readouterr()
    assert _status(tmp_path)["INV-1103"]["status"] == "suggested_paid"
    assert not any("INV-1103" in e for e in _client_eml(tmp_path))


def test_mark_paid_accepts_the_id_typed_with_an_en_dash(tmp_path, capsys):
    _write(tmp_path, GAMMA, [])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    capsys.readouterr()
    assert _cmd(tmp_path, "mark-paid", "INV–1103", day="2026-08-21") == 0


# ---- (2) the safety net ---------------------------------------------------------------------------------------------------

NET_INVOICES = "INV-1107,Delta Ltd,d@d.example,500,USD,2026-06-01,2026-07-01\n"


@pytest.mark.parametrize("desc", ["INVOICE#1107", "inv no. 1107", "#1107", "Ref 1107 paid"])
def test_a_number_mentioned_without_the_strict_rule_holds_the_invoice(tmp_path, capsys, desc):
    # payer is exactly the client and the amount is exact: without the net this would be a suggestion
    _write(tmp_path, NET_INVOICES, [f'"T-1","15-07-2026","500.00","USD","{desc}","","Delta Ltd"\n'])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    capsys.readouterr()
    row = _status(tmp_path)["INV-1107"]
    assert row["status"] == "needs_review"
    assert "a payment mentions 1107: check it" in row["why_not_paid"]
    assert not any(e.startswith("drafts/") for e in _client_eml(tmp_path)), "never a reminder in drafts/"


def test_invoice_hash_1107_is_held_and_never_suggested_paid(tmp_path, capsys):
    _write(tmp_path, NET_INVOICES, ['"T-1","15-07-2026","500.00","USD","INVOICE#1107","","Delta Ltd"\n'])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    capsys.readouterr()
    assert _status(tmp_path)["INV-1107"]["status"] == "needs_review"
    assert not any("suggested" in r["status"] for r in _status(tmp_path).values() if r["invoice_id"] == "INV-1107")


def test_the_net_is_a_whole_token_not_a_substring(tmp_path, capsys):
    _write(tmp_path, NET_INVOICES, ['"T-1","15-07-2026","500.00","USD","Payment 110711","","Delta Ltd"\n'])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    capsys.readouterr()
    assert "a payment mentions" not in _status(tmp_path)["INV-1107"]["why_not_paid"]


def test_a_strict_id_match_is_still_a_suggestion(tmp_path, capsys):
    _write(tmp_path, NET_INVOICES, ['"T-1","15-07-2026","500.00","USD","Payment INV-1107","INV-1107","Other Co"\n'])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    capsys.readouterr()
    assert _status(tmp_path)["INV-1107"]["status"] == "suggested_paid"


def test_the_net_holds_past_the_stuck_window_as_check_first_only(tmp_path, capsys):
    _write(tmp_path, NET_INVOICES, ['"T-1","15-07-2026","500.00","USD","INVOICE#1107","","Delta Ltd"\n'])
    seed_first_seen(tmp_path, "2026-08-01", "INV-1107")
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    capsys.readouterr()
    eml = _client_eml(tmp_path)
    assert eml and all(e.startswith("check-first/") for e in eml), eml


def test_the_net_past_30_days_overdue_is_overdue_held_critical(tmp_path, capsys):
    _write(tmp_path, NET_INVOICES, ['"T-1","15-07-2026","500.00","USD","INVOICE#1107","","Delta Ltd"\n'])
    capsys.readouterr()
    _run(tmp_path, "2026-10-20")
    err = capsys.readouterr().err
    assert "CRITICAL [OVERDUE_HELD]" in err and "INV-1107" in err


def test_a_payment_the_user_rejected_for_the_invoice_does_not_hold_it(tmp_path, capsys):
    _write(tmp_path, NET_INVOICES, ['"T-1","15-07-2026","500.00","USD","#1107","","Other Co"\n'])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    capsys.readouterr()
    assert _status(tmp_path)["INV-1107"]["status"] == "needs_review"
    assert main(["reject", "INV-1107", "T-1", "--out", str(tmp_path / "out")]) in (0, 2)
    capsys.readouterr()
    _run(tmp_path, "2026-08-22")
    capsys.readouterr()
    assert _status(tmp_path)["INV-1107"]["status"] == "overdue"


# ---- (3) greeting and sender -----------------------------------------------------------------------------------------------

def test_a_blank_contact_greets_there_never_the_email(tmp_path, capsys):
    _write(tmp_path, "INV-2201,Epsilon Ltd,eps@e.example,300,USD,2026-06-01,2026-07-01\n", [])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    capsys.readouterr()
    (eml,) = [e for e in _client_eml(tmp_path) if "INV-2201" in e]
    _subject, body = body_of(tmp_path / "out" / eml)
    assert body.startswith("Hi there,")
    assert "eps@e.example" not in body


def test_an_email_in_the_contact_column_is_never_used_as_a_name(tmp_path, capsys):
    from datetime import date
    from decimal import Decimal
    inv = Invoice("INV-9", "Zeta Ltd", "z@z.example", Decimal("10"), "USD", date(2026, 6, 1), date(2026, 7, 1), "", "z@z.example")
    assert first_name(InvoiceResult(inv, "overdue", Decimal("0"), Decimal("10"), 5, None, 0.0)) == ""


def test_missing_sender_is_critical_on_every_run_and_drafts_are_still_written(tmp_path, capsys):
    (tmp_path / "i.csv").write_text("id,client,email,amount,currency,issued,due\nINV-5,Kappa Co,k@k.example,100,USD,2026-06-01,2026-07-01\n",
                                    encoding="utf-8")
    cmd = ["run", "--invoices", str(tmp_path / "i.csv"), "--out", str(tmp_path / "out"), "--today", "2026-08-20"]
    for _ in range(2):
        capsys.readouterr()
        assert main(cmd) == 2
        err = capsys.readouterr().err
        assert "CRITICAL [NO_SENDER]" in err
    drafts = list((tmp_path / "out" / "drafts").glob("*.eml"))
    assert drafts, "drafts are still written without a sender"
    assert "set-sender-email@example.invalid" in drafts[0].read_text(encoding="utf-8")
    assert "NO_SENDER" in (tmp_path / "out" / "review.html").read_text(encoding="utf-8")
