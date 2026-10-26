"""1.1.18 (judge R10 majors 2-3): a hold's reason states what WAS detected, from one builder and one client-name test. A refund row
is named by a REFUND_SEEN notice, never silently dropped. (The confirm refusal for review-kind suggestions is NOT in this file: it
changes shipped hold flows and is waiting for a decision.)"""
from test_v1110 import _run, _write
from test_v1115 import _status
from test_v1116 import _row

from invoice_chaser.matching import hold_reason

BLUEBIRD = "INV-1103,Bluebird Media,b@b.example,800,USD,2026-06-01,2026-07-01\n"


def test_refund_then_repay_far_back_names_the_client_match_not_its_absence(tmp_path, capsys):
    # the refund -800 and the +800 repayment from the same client; the repayment is outside the name window, so it is a NEAR hold
    _write(tmp_path, BLUEBIRD, [_row("T-0", "-800.00", "Refund to Bluebird Media", "Bluebird Media", day="15-07-2026"),
                                _row("T-1", "800.00", "Payment from Bluebird Media", "Bluebird Media", day="01-03-2026")])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    err = capsys.readouterr().err
    row = _status(tmp_path)["INV-1103"]
    assert row["status"] == "needs_review", row
    assert "client name matched" in row["why_not_paid"], row["why_not_paid"]
    assert "no client" not in row["why_not_paid"] and "no invoice id or client name" not in row["why_not_paid"]
    assert "REFUND_SEEN" in err and "Refund to Bluebird Media" in err and "-800.00" in err


def test_near_hold_for_a_client_payment_says_the_name_matched(tmp_path, capsys):
    _write(tmp_path, BLUEBIRD, [_row("T-1", "776.00", "Payment from Bluebird Media", "Bluebird Media", day="01-03-2026")])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    capsys.readouterr()
    row = _status(tmp_path)["INV-1103"]
    assert row["status"] == "needs_review", row
    assert "NEAR_AMOUNT: payment T-1" in row["why_not_paid"], row["why_not_paid"]
    assert "names the client and no invoice id" in row["why_not_paid"]
    assert "names no invoice and no client" not in row["why_not_paid"]


def test_near_hold_for_a_stranger_says_no_client_name_matched(tmp_path, capsys):
    _write(tmp_path, BLUEBIRD, [_row("T-1", "776.00", "thanks for the work", "Some Stranger", day="01-03-2026")])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    capsys.readouterr()
    row = _status(tmp_path)["INV-1103"]
    assert row["status"] == "needs_review", row
    assert "it names no invoice id and no client" in row["why_not_paid"]
    assert "client name matched" not in row["why_not_paid"]


def test_the_reason_builder_only_says_what_was_detected():
    assert hold_reason(True, near_amount=True) == "amount near the invoice; client name matched, no invoice id"
    assert hold_reason(False, near_amount=True) == "amount near the invoice; no invoice id, no client name matched"
    assert hold_reason(True, near_amount=False, other_currency=True) == "payment in another currency; client name matched, no invoice id"


def test_a_refund_row_is_listed_by_a_refund_seen_notice(tmp_path, capsys):
    _write(tmp_path, BLUEBIRD, [_row("R-9", "-120.50", "Refund to Bluebird Media", "Bluebird Media", day="15-07-2026")])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    err = capsys.readouterr().err
    assert "REFUND_SEEN" in err and "R-9" in err and "120.5" in err and "Refund to Bluebird Media" in err
    assert "NOT counted" in err
