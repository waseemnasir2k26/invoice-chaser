"""1.1.13: (1) a held invoice more than stuck_overdue_days overdue gets a CRITICAL OVERDUE_HELD on every run, with its days overdue
and the exact resolve commands; run exits 2. (2) a CHECK FIRST draft states the real outstanding amount, never a suggested payment's
remainder. (3) mark-paid refuses when a confirmed payment of the same client and amount already backs another invoice, unless --force."""
import json
from pathlib import Path

from test_v1110 import _client_eml, _cmd, _run, _write
from test_v118 import body_of, seed_first_seen

from invoice_chaser.ledger import load_paid


def _stderr_lines(capsys) -> list[str]:
    return capsys.readouterr().err.splitlines()


def _overdue_lines(err_lines: list[str]) -> list[str]:
    return [ln for ln in err_lines if "CRITICAL [OVERDUE_HELD]" in ln]


# ---- (1) OVERDUE_HELD ----------------------------------------------------------------------------------------------------

REPRO_A_INVOICES = ("INV-101,Alpha Co,a@a.example,200,USD,2026-06-01,2026-07-01\n"
                    "INV-102,Gamma Ltd,c@c.example,300,USD,2026-06-01,2026-07-01\n")
REPRO_A_WISE = ['"TRANSFER-1","05-10-2026","300.00","USD","Payment for INV-101","INV-101","Gamma Ltd"\n']


def test_repro_a_held_invoice_past_30_days_is_critical_on_first_sight_and_exits_2(tmp_path, capsys):
    _write(tmp_path, REPRO_A_INVOICES, REPRO_A_WISE)
    capsys.readouterr()
    assert _run(tmp_path, "2026-10-20") == 2
    lines = [ln for ln in _overdue_lines(_stderr_lines(capsys)) if "INV-102 (" in ln]
    assert len(lines) == 1                                    # INV-101 is held too (also past 30 days): its own line, not checked here
    assert "days overdue" in lines[0]
    assert "invoice-chaser confirm INV-102" in lines[0] or "invoice-chaser mark-paid INV-102" in lines[0]
    assert "invoice-chaser reject INV-102" in lines[0] or "invoice-chaser mark-paid INV-102" in lines[0]


def test_repro_a_is_critical_on_every_run_not_only_the_first(tmp_path, capsys):
    _write(tmp_path, REPRO_A_INVOICES, REPRO_A_WISE)
    _run(tmp_path, "2026-10-20")
    capsys.readouterr()
    assert _run(tmp_path, "2026-10-21") == 2
    assert len([ln for ln in _overdue_lines(_stderr_lines(capsys)) if "INV-102 (" in ln]) == 1


def test_held_invoice_not_past_the_overdue_threshold_is_not_critical(tmp_path, capsys):
    _write(tmp_path, REPRO_A_INVOICES, REPRO_A_WISE)
    capsys.readouterr()
    assert _run(tmp_path, "2026-07-20") == 0                 # INV-102 is 19 days overdue: held, but under 30 days
    assert _overdue_lines(_stderr_lines(capsys)) == []


def test_repro_n1_both_shared_price_holds_get_overdue_held_and_exit_2(tmp_path, capsys):
    _write(tmp_path, "INV-P1,Pair Co,p@p.example,200,USD,2026-06-01,2026-07-01\n"
                     "INV-P2,Pair Co,p@p.example,200,USD,2026-06-01,2026-07-01\n",
           ['"","05-10-2026","200.00","USD","Transfer","",""\n'])
    capsys.readouterr()
    assert _run(tmp_path, "2026-10-20") == 2
    text = "\n".join(_overdue_lines(_stderr_lines(capsys)))
    assert "INV-P1" in text and "INV-P2" in text


# ---- (2) CHECK FIRST draft shows the real outstanding ---------------------------------------------------------------------

def test_check_first_draft_states_the_invoice_amount_not_zero_when_a_payment_is_only_suggested(tmp_path, capsys):
    _write(tmp_path, "INV-601,Suggest Co,s@s.example,400,USD,2026-06-01,2026-07-01\n",
           ['"","05-07-2026","400.00","USD","Payment INV-601","INV-601","Suggest Co"\n'])
    seed_first_seen(tmp_path, "2026-09-01", "INV-601")
    _run(tmp_path, "2026-09-30")
    capsys.readouterr()
    drafts = sorted((tmp_path / "out" / "drafts" / "check-first").glob("INV-601-*.eml"))
    assert drafts, "a CHECK FIRST draft is expected for a suggestion held past the stuck window"
    text = body_of(drafts[0])[1]
    assert "Invoice INV-601 for 400.00 USD" in text
    assert "for 0.00 USD" not in text


# ---- (3) mark-paid refuses a second payment of the same client and amount ------------------------------------------------

TWICE = ("INV-701,Twice Co,t@t.example,500,USD,2026-06-01,2026-07-01\n"
         "INV-702,Twice Co,t@t.example,500,USD,2026-06-01,2026-07-01\n")
TWICE_WISE = ['"","15-09-2026","500.00","USD","Payment INV-701","INV-701","Twice Co"\n']


def test_mark_paid_refuses_when_a_confirmed_payment_of_the_same_client_and_amount_backs_another_invoice(tmp_path, capsys):
    _write(tmp_path, TWICE, TWICE_WISE)
    assert _run(tmp_path, "2026-09-20") in (0, 2)
    assert _cmd(tmp_path, "confirm", "INV-701", day="2026-09-20") == 0
    capsys.readouterr()
    assert _cmd(tmp_path, "mark-paid", "INV-702", day="2026-09-20") == 2
    err = capsys.readouterr()
    assert "a payment of this client and amount already backs INV-701; pass --force if this is a second payment" in (err.err + err.out)
    assert "INV-702" not in load_paid(tmp_path / "out" / "sent.json")


def test_mark_paid_with_force_records_the_second_payment(tmp_path, capsys):
    _write(tmp_path, TWICE, TWICE_WISE)
    assert _run(tmp_path, "2026-09-20") in (0, 2)
    assert _cmd(tmp_path, "confirm", "INV-701", day="2026-09-20") == 0
    capsys.readouterr()
    assert _cmd(tmp_path, "mark-paid", "INV-702", "--force", day="2026-09-20") == 0
    assert "INV-702" in load_paid(tmp_path / "out" / "sent.json")


def test_mark_paid_for_another_client_is_not_refused(tmp_path, capsys):
    _write(tmp_path, "INV-701,Twice Co,t@t.example,500,USD,2026-06-01,2026-07-01\n"
                     "INV-703,Other Co,o@o.example,500,USD,2026-06-01,2026-07-01\n", TWICE_WISE)
    assert _run(tmp_path, "2026-09-20") in (0, 2)
    assert _cmd(tmp_path, "confirm", "INV-701", day="2026-09-20") == 0
    assert _cmd(tmp_path, "mark-paid", "INV-703", day="2026-09-20") == 0
