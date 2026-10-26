"""1.1.17 (judge R9 blocker): a payment in ANOTHER currency with no invoice id never leaves an open invoice on a root reminder.
(1) a payment that names the client, in any currency, holds the invoice (CURRENCY_MISMATCH, needs_review, no draft in drafts/).
(2) an unidentified stranger payment whose amount is near the invoice amount (read as a number, no FX) holds it too.
(3) control: a stranger at a far-off amount does not hold it; the normal reminder still goes out.
(4) a client-name payment already confirmed to another invoice does not hold this one."""
from test_v1110 import _cmd, _run, _write
from test_v1115 import _status
from test_v1116 import _root_drafts_for, _row

from invoice_chaser.matching import NO_MATCH_NOTE

BLUEBIRD = "INV-1103,Bluebird Media,b@b.example,800,USD,2026-06-01,2026-07-01\n"


def test_eur_800_from_the_client_holds_the_usd_invoice_with_no_root_draft(tmp_path, capsys):
    _write(tmp_path, BLUEBIRD, [_row("T-1", "800.00", "Payment from Bluebird Media", "Bluebird Media", cur="EUR")])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    err = capsys.readouterr().err
    row = _status(tmp_path)["INV-1103"]
    assert row["status"] == "needs_review", row
    assert "CURRENCY_MISMATCH" in err
    assert "a payment from Bluebird Media in EUR 800.00 may be for this USD invoice: check it" in row["why_not_paid"]
    assert "invoice-chaser confirm INV-1103 --payment T-1" in row["why_not_paid"]
    assert "invoice-chaser reject INV-1103 T-1" in row["why_not_paid"]
    assert _root_drafts_for(tmp_path, "INV-1103") == [], "never a root reminder while the payment may be this invoice"


def test_stranger_eur_800_holds_the_usd_800_invoice(tmp_path, capsys):
    _write(tmp_path, BLUEBIRD, [_row("T-1", "800.00", "thanks for the work", "Some Stranger", cur="EUR")])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    err = capsys.readouterr().err
    row = _status(tmp_path)["INV-1103"]
    assert row["status"] == "needs_review", row
    assert "CURRENCY_MISMATCH" in err
    assert "invoice-chaser confirm INV-1103 --payment T-1" in row["why_not_paid"]
    assert _root_drafts_for(tmp_path, "INV-1103") == []


def test_control_stranger_eur_400_is_not_held_and_the_normal_reminder_goes_out(tmp_path, capsys):
    _write(tmp_path, BLUEBIRD, [_row("T-1", "400.00", "thanks for the work", "Some Stranger", cur="EUR")])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    err = capsys.readouterr().err
    row = _status(tmp_path)["INV-1103"]
    assert row["status"] == "overdue", row
    assert "CURRENCY_MISMATCH" not in err
    assert row["why_not_paid"] == NO_MATCH_NOTE
    assert _root_drafts_for(tmp_path, "INV-1103"), "the control must be able to fail: the reminder is written"


def test_client_eur_payment_already_confirmed_to_another_invoice_does_not_hold(tmp_path, capsys):
    two = BLUEBIRD + "INV-2001,Bluebird Media,b@b.example,800,EUR,2026-06-01,2026-07-01\n"
    _write(tmp_path, two, [_row("T-1", "800.00", "Payment from Bluebird Media", "Bluebird Media", cur="EUR")])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    capsys.readouterr()
    assert _status(tmp_path)["INV-2001"]["status"] == "suggested_paid"
    assert _cmd(tmp_path, "confirm", "INV-2001", "--payment", "T-1", "--yes", day="2026-08-20") == 0
    capsys.readouterr()
    _run(tmp_path, "2026-08-21")
    err = capsys.readouterr().err
    assert _status(tmp_path)["INV-1103"]["status"] == "overdue"
    assert "CURRENCY_MISMATCH" not in err
    assert _root_drafts_for(tmp_path, "INV-1103")
