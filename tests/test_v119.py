"""1.1.9: a held invoice you confirmed paid gets NO client draft of any kind (the judge repro: confirm, edit the amount, run far
ahead); a held invoice shows its real days overdue on every path, with the hold reason in its own status.csv column."""
import csv
import json
from pathlib import Path

from test_v118 import (INV_HEAD, JUDGE_INV, ROW_A, TWO, WISE_HEAD, check_first_files, cmd, body_of, run, seed_first_seen, setup)


def _drafts(tmp_path) -> list[str]:
    return sorted(p.name for p in (tmp_path / "out" / "drafts").glob("*.eml"))


def test_judge_repro_confirmed_then_changed_gets_no_client_draft_anywhere(tmp_path):
    setup(tmp_path)                                                    # INV-Z 300 USD, paid by a 300.00 transfer on 2026-09-15
    run(tmp_path, today="2026-09-20")
    assert cmd(tmp_path, "confirm", "INV-Z", "--payment", "h-a20a2d0c80f9", day="2026-09-20") == 0
    (tmp_path / "i.csv").write_text(INV_HEAD + JUDGE_INV.replace(",300,", ",310,"), encoding="utf-8")   # edited after you confirmed
    seed_first_seen(tmp_path, "2026-08-01", "INV-Z")
    _, rows = run(tmp_path, today="2026-12-30")
    assert rows["INV-Z"]["status"] == "needs_review"
    assert rows["INV-Z"]["days_overdue"] == "182"
    assert check_first_files(tmp_path) == []                           # no [CHECK FIRST] client reminder
    assert _drafts(tmp_path) == ["INV-Z-check.eml"]                    # the only file is the internal note to you
    subject, body = body_of(tmp_path / "out" / "drafts" / "INV-Z-check.eml")
    assert "You confirmed this paid on 2026-09-20, but the invoice changed: re-confirm or unmark." in body
    assert "re-confirm or unmark" in body and "not sent to the client" in body
    assert "CHECK FIRST" not in subject
    entries = json.loads((tmp_path / "out" / "drafts.json").read_text(encoding="utf-8"))
    assert all(e.get("internal") for e in entries)                     # nothing addressed to the client
    assert "no client draft" in rows["INV-Z"]["draft_status"] and "confirmed" in rows["INV-Z"]["draft_status"]


def test_a_confirmed_double_bound_invoice_gets_no_client_draft_and_shows_real_days(tmp_path, capsys):
    (tmp_path / "i.csv").write_text(INV_HEAD + TWO, encoding="utf-8")
    (tmp_path / "w.csv").write_text(WISE_HEAD + ROW_A, encoding="utf-8")
    (tmp_path / "c.json").write_text(json.dumps({"sender_email": "me@x.co"}), encoding="utf-8")
    run(tmp_path, today="2026-10-25")
    assert cmd(tmp_path, "confirm", "INV-101", day="2026-10-25") == 0
    from invoice_chaser.ledger import load_payment_rows
    key = load_payment_rows(tmp_path / "out" / "sent.json")[0]["key"]
    raw = json.loads((tmp_path / "out" / "sent.json").read_text(encoding="utf-8"))
    raw["paid"]["INV-102"] = {"date": "2026-10-25", "note": "", "source": "confirm", "payments": [key],
                              "snapshot": {"client": "Beta Works", "amount": "500", "currency": "USD", "due": "2026-10-01"}, "payment_rows": []}
    (tmp_path / "out" / "sent.json").write_text(json.dumps(raw), encoding="utf-8")
    seed_first_seen(tmp_path, "2026-09-01", "INV-101", "INV-102")
    _, rows = run(tmp_path, today="2026-12-30")
    for inv in ("INV-101", "INV-102"):
        assert rows[inv]["status"] == "needs_review"
        assert rows[inv]["days_overdue"] == "90"                       # 2026-10-01 due, 90 days overdue at 2026-12-30, not 0
        assert "PAYMENT_DOUBLE_BOUND" in rows[inv]["hold_reason"]
    assert check_first_files(tmp_path) == []
    assert all(name.endswith("-check.eml") for name in _drafts(tmp_path))


def test_a_held_invoice_without_a_ledger_entry_keeps_its_hold_reason_in_its_own_column(tmp_path):
    setup(tmp_path)
    seed_first_seen(tmp_path, "2026-09-01", "INV-Z")
    _, rows = run(tmp_path)
    assert rows["INV-Z"]["status"] == "needs_review"
    assert rows["INV-Z"]["days_overdue"] == "182"
    assert rows["INV-Z"]["hold_reason"].startswith("h-a20a2d0c80f9:")
    assert rows["INV-Z"]["days_overdue"] == "182" and rows["INV-Z"]["hold_reason"] != rows["INV-Z"]["days_overdue"]
    header = next(csv.reader(open(tmp_path / "out" / "status.csv", encoding="utf-8")))
    assert header[-1] == "hold_reason"
