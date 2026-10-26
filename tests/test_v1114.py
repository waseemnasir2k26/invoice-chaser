"""1.1.14: (1) a payment in ANOTHER currency that names a known invoice id holds that invoice (needs_review), whatever its currency or
amount: no reminder in drafts/, OVERDUE_HELD past 30 days. (2) a CHECK FIRST draft for a partial suggestion names the payment that
was found and says it waits for your check."""
from test_v1110 import _client_eml, _run, _write
from test_v118 import INV_HEAD, WISE_HEAD, body_of, seed_first_seen


def _status_text(base) -> str:
    return (base / "out" / "status.csv").read_text(encoding="utf-8")


def _overdue_lines(err: str) -> list[str]:
    return [ln for ln in err.splitlines() if "CRITICAL [OVERDUE_HELD]" in ln]


# ---- (1) a payment in another currency that names the invoice id holds the invoice ----------------------------------

CEE_INV = "INV-C1,Cee Co,c@c.example,500,USD,2026-06-01,2026-07-01\n"
CEE_WISE = ['"","15-07-2026","300.00","EUR","Payment INV-C1","INV-C1","Cee Co"\n']


def test_repro_eur_payment_naming_usd_invoice_holds_it_and_no_reminder_is_drafted(tmp_path, capsys):
    _write(tmp_path, CEE_INV, CEE_WISE)
    _run(tmp_path, "2026-08-20")
    capsys.readouterr()
    assert _client_eml(tmp_path) == [], "no client reminder may be drafted while a payment that names INV-C1 waits for you"
    status = _status_text(tmp_path)
    assert "needs_review" in status
    assert "a payment in EUR names this invoice: check the exchange/amount" in status


def test_eur_payment_naming_usd_invoice_past_30_days_is_overdue_held_and_exits_2(tmp_path, capsys):
    _write(tmp_path, CEE_INV, CEE_WISE)
    capsys.readouterr()
    assert _run(tmp_path, "2026-10-20") == 2
    lines = _overdue_lines(capsys.readouterr().err)
    assert len(lines) == 1 and "INV-C1 (Cee Co)" in lines[0]
    assert _client_eml(tmp_path) == []


def test_eur_payment_naming_invoice_is_held_after_the_stuck_window_only_as_check_first(tmp_path, capsys):
    _write(tmp_path, CEE_INV, CEE_WISE)
    seed_first_seen(tmp_path, "2026-08-01", "INV-C1")
    _run(tmp_path, "2026-08-20")
    capsys.readouterr()
    eml = _client_eml(tmp_path)
    assert eml and all(e.startswith("check-first/") for e in eml), eml


def test_eur_payment_without_an_invoice_id_does_not_hold_the_usd_invoice(tmp_path, capsys):
    _write(tmp_path, CEE_INV, ['"","15-07-2026","300.00","EUR","Transfer","","Other Ltd"\n'])
    capsys.readouterr()
    _run(tmp_path, "2026-08-20")
    capsys.readouterr()
    assert any("INV-C1" in e for e in _client_eml(tmp_path)), "an unnamed payment in another currency must not hold the invoice"


# ---- (2) CHECK FIRST for a partial suggestion names the payment found ---------------------------------------------------

def test_check_first_partial_suggestion_names_the_payment_that_was_found(tmp_path, capsys):
    _write(tmp_path, "INV-K1,Kay Co,k@k.example,500,USD,2026-06-01,2026-07-01\n",
           ['"","05-07-2026","300.00","USD","Payment INV-K1","INV-K1","Kay Co"\n'])
    seed_first_seen(tmp_path, "2026-08-01", "INV-K1")
    _run(tmp_path, "2026-09-30")
    capsys.readouterr()
    drafts = sorted((tmp_path / "out" / "drafts" / "check-first").glob("INV-K1-*.eml"))
    assert drafts, "a CHECK FIRST draft is expected for a partial suggestion held past the stuck window"
    text = body_of(drafts[0])[1]
    assert ("Invoice INV-K1 for 500.00 USD (a payment of 300.00 USD was found and is waiting for your check)") in text
