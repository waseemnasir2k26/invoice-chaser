"""1.1.7: the ledger invariants (one payment backs one invoice), write verification, rebind/forget against today's exports,
refusals that never overwrite a decision, held invoices shown as held, and exit code 2 on a CRITICAL notice."""
import csv
import json
import re
from datetime import date
from pathlib import Path

import pytest

import invoice_chaser.ledger as ledger_mod
from invoice_chaser.cli import main
from invoice_chaser.ledger import (
    load_paid,
    load_payment_rows,
    load_rejected,
    rebind_keys,
    record_sent,
    rename_invoice,
)

WISE_HEAD = '"TransferWise ID","Date","Amount","Currency","Description","Payment Reference","Payer Name"\n'
INV_HEAD = "id,client,email,amount,currency,issued,due\n"
TWO = "INV-101,Holding Co,a@x.example,500,USD,2026-09-01,2026-10-01\nINV-102,Beta Works,b@x.example,500,USD,2026-09-01,2026-10-01\n"
ROW_A = '"","05-10-2026","500.00","USD","Payment INV-101","INV-101","Holding Co"\n'
SNAP_102 = {"client": "Beta Works", "amount": "500", "currency": "USD", "due": "2026-10-01"}


def setup(tmp_path, invoices=TWO, wise=ROW_A):
    (tmp_path / "i.csv").write_text(INV_HEAD + invoices, encoding="utf-8")
    (tmp_path / "w.csv").write_text(WISE_HEAD + wise, encoding="utf-8")
    (tmp_path / "c.json").write_text(json.dumps({"sender_email": "me@x.co"}), encoding="utf-8")


def run(tmp_path, capsys=None):
    code = main(["run", "--invoices", str(tmp_path / "i.csv"), "--wise", str(tmp_path / "w.csv"), "--config", str(tmp_path / "c.json"),
                 "--out", str(tmp_path / "out"), "--today", "2026-10-25"])
    rows = {r["invoice_id"]: r for r in csv.DictReader(open(tmp_path / "out" / "status.csv", encoding="utf-8"))}
    return code, rows


def cmd(tmp_path, *args, today=True):
    extra = ["--today", "2026-10-25"] if today else []
    return main([*args, "--out", str(tmp_path / "out"), *extra])


def ledger(tmp_path) -> Path:
    return tmp_path / "out" / "sent.json"


def edit_ledger(tmp_path, fn):
    data = json.loads(ledger(tmp_path).read_text(encoding="utf-8"))
    fn(data)
    ledger(tmp_path).write_text(json.dumps(data), encoding="utf-8")


def confirmed_101(tmp_path):
    setup(tmp_path)
    run(tmp_path)
    assert cmd(tmp_path, "confirm", "INV-101") == 0
    return load_payment_rows(ledger(tmp_path))[0]["key"]


def bind_102_to_the_same_payment(tmp_path):
    """The ledger now says one payment backs INV-101 AND INV-102 (what a bad edit or an old build could leave behind)."""
    def go(data):
        data["paid"]["INV-102"] = {"date": "2026-10-25", "note": "", "source": "confirm", "payments": list(data["paid"]["INV-101"]["payments"]),
                                   "snapshot": SNAP_102, "payment_rows": []}
    edit_ledger(tmp_path, go)


# ---- I1: one payment key backs at most one invoice --------------------------------------------------------------

def test_i1_run_flags_a_payment_backing_two_invoices_critical_and_holds_both(tmp_path, capsys):
    key = confirmed_101(tmp_path)
    bind_102_to_the_same_payment(tmp_path)
    capsys.readouterr()
    code, rows = run(tmp_path)
    err = capsys.readouterr().err
    assert code == 2
    assert "CRITICAL [PAYMENT_DOUBLE_BOUND]" in err and "INV-101" in err and "INV-102" in err and key in err
    assert f"invoice-chaser unbind INV-101 {key}" in err and f"invoice-chaser unbind INV-102 {key}" in err
    for inv in ("INV-101", "INV-102"):
        assert rows[inv]["status"] == "needs_review" and rows[inv]["draft_file"] == ""      # held: no reminder, not counted paid


def test_i1_the_double_binding_is_shown_as_held_not_as_days_overdue(tmp_path, capsys):
    confirmed_101(tmp_path)
    bind_102_to_the_same_payment(tmp_path)
    capsys.readouterr()
    run(tmp_path)
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if l.startswith("  INV-102"))
    assert "held: PAYMENT_DOUBLE_BOUND" in line and not re.search(r"\s\d+d\s", line)   # no "0d overdue" for a held invoice


def test_i1_every_write_refuses_to_add_a_second_binding_and_writes_nothing(tmp_path):
    key = confirmed_101(tmp_path)
    before = ledger(tmp_path).read_text(encoding="utf-8")
    data = json.loads(before)
    data["received"]["INV-102"] = {"date": "2026-10-25", "amount": "1.00", "payments": [key], "snapshot": SNAP_102, "payment_rows": []}
    with pytest.raises(ValueError, match="back payment .* with two invoices"):
        ledger_mod._write(ledger(tmp_path), data)
    assert ledger(tmp_path).read_text(encoding="utf-8") == before


def test_i1_confirm_refuses_a_payment_key_already_bound_to_any_invoice(tmp_path, capsys):
    key = confirmed_101(tmp_path)
    run(tmp_path)
    before = ledger(tmp_path).read_text(encoding="utf-8")
    # a suggestion (from today's run) that would put the same payment under INV-102: the confirm must refuse it
    sug = json.loads((tmp_path / "out" / "suggestions.json").read_text(encoding="utf-8"))
    sug["suggestions"] = [{"invoice_id": "INV-102", "kind": "paid", "client": "Beta Works", "amount": "500", "currency": "USD",
                           "due": "2026-10-01", "snapshot": SNAP_102, "tolerance": "0", "previous_received": "0", "previous_payments": [],
                           "confidence": 1.0, "reason": "x", "ledger_state": "",
                           "payments": [{"key": key, "id": key.split(":", 1)[1], "source": "wise", "date": "2026-10-05", "amount": "500.00",
                                         "currency": "USD", "payer": "Holding Co", "description": "", "allocated": "500.00", "named": []}]}]
    (tmp_path / "out" / "suggestions.json").write_text(json.dumps(sug), encoding="utf-8")
    assert cmd(tmp_path, "confirm", "INV-102") == 2
    assert "already backs INV-101" in capsys.readouterr().err
    assert ledger(tmp_path).read_text(encoding="utf-8") == before


def test_i1_rebind_refuses_a_target_key_bound_to_any_invoice(tmp_path):
    key = confirmed_101(tmp_path)
    other = "wise:someone-else"

    def go(data):
        data["paid"]["INV-102"] = {"date": "2026-10-25", "note": "", "source": "confirm", "payments": [other], "snapshot": SNAP_102,
                                   "payment_rows": []}
    edit_ledger(tmp_path, go)
    with pytest.raises(ValueError, match="already has a decision"):
        rebind_keys(ledger(tmp_path), {key: other})
    assert load_paid(ledger(tmp_path))["INV-101"]["payments"] == [key]                          # nothing overwritten


def test_i1_unbind_removes_one_link_and_the_other_invoice_keeps_its_own(tmp_path, capsys):
    key = confirmed_101(tmp_path)
    bind_102_to_the_same_payment(tmp_path)
    assert cmd(tmp_path, "unbind", "INV-102", key, today=False) == 0
    assert "INV-102" not in load_paid(ledger(tmp_path)) and load_paid(ledger(tmp_path))["INV-101"]["payments"] == [key]
    code, rows = run(tmp_path)
    assert code == 0 and rows["INV-101"]["status"] == "paid" and rows["INV-102"]["status"] != "needs_review"


def test_i1_unbind_of_a_payment_the_invoice_does_not_have_changes_nothing(tmp_path, capsys):
    confirmed_101(tmp_path)
    before = ledger(tmp_path).read_text(encoding="utf-8")
    assert cmd(tmp_path, "unbind", "INV-101", "wise:nope", today=False) == 1
    assert "nothing changed" in capsys.readouterr().out
    assert ledger(tmp_path).read_text(encoding="utf-8") == before


# ---- I2: every ledger write is re-read and must show the change; no-op commands say so and exit 1 -----------

def test_i2_a_write_that_does_not_land_is_reported_not_silently_accepted(tmp_path, monkeypatch):
    setup(tmp_path)
    run(tmp_path)
    ledger_path = tmp_path / "out" / "sent.json"
    monkeypatch.setattr(ledger_mod, "atomic_write_text", lambda *a, **k: None)     # the disk accepts the call and writes nothing
    with pytest.raises(ValueError, match="reading it back does not show the change"):
        record_sent(ledger_path, "INV-101", "nudge", date(2026, 10, 20))


def test_i2_a_repeated_mark_sent_says_nothing_changed_and_exits_1(tmp_path, capsys):
    setup(tmp_path)
    run(tmp_path)
    assert cmd(tmp_path, "mark-sent", "INV-101", "--stage", "nudge", "--date", "2026-10-20") == 0
    capsys.readouterr()
    assert cmd(tmp_path, "mark-sent", "INV-101", "--stage", "nudge", "--date", "2026-10-20") == 1
    assert "nothing changed" in capsys.readouterr().out


def test_i2_rejecting_the_same_payment_twice_says_nothing_changed(tmp_path, capsys):
    setup(tmp_path, invoices="INV-101,Holding Co,a@x.example,500,USD,2026-09-01,2026-10-01\n")
    run(tmp_path)
    pid = json.loads((tmp_path / "out" / "suggestions.json").read_text(encoding="utf-8"))["suggestions"][0]["payments"][0]["id"]
    assert cmd(tmp_path, "reject", "INV-101", pid, today=False) == 0
    capsys.readouterr()
    assert cmd(tmp_path, "reject", "INV-101", pid, today=False) == 1
    assert "nothing changed" in capsys.readouterr().out


def test_i2_forgetting_something_not_in_the_ledger_says_nothing_changed(tmp_path, capsys):
    setup(tmp_path)
    run(tmp_path)
    assert cmd(tmp_path, "forget", "INV-404", today=False) == 2          # not an orphan: refused, nothing written
    assert cmd(tmp_path, "forget", "INV-404", "--force", today=False) == 1
    assert "nothing changed" in capsys.readouterr().out


# ---- I3: rebind and forget use the exports as they are now; identical invoices renamed X1 / X2 -------------------

def test_i3_identical_invoices_renamed_x1_and_x2_are_both_candidates_and_rebind_takes_one(tmp_path, capsys):
    same = "INV-101,Holding Co,a@x.example,500,USD,2026-09-01,2026-10-01\n"
    setup(tmp_path, invoices=same)
    run(tmp_path)
    assert cmd(tmp_path, "confirm", "INV-101") == 0
    # INV-101 is renamed: two identical invoices now stand where it was (same client, amount, due date)
    (tmp_path / "i.csv").write_text(INV_HEAD + "X1,Holding Co,a@x.example,500,USD,2026-09-01,2026-10-01\n"
                                    "X2,Holding Co,a@x.example,500,USD,2026-09-01,2026-10-01\n", encoding="utf-8")
    code, rows = run(tmp_path)
    assert code == 2 and rows["X1"]["status"] == "needs_review" and rows["X2"]["status"] == "needs_review"
    orphans = json.loads((tmp_path / "out" / "orphans.json").read_text(encoding="utf-8"))["orphans"]
    assert sorted(o["new"] for o in orphans if o["kind"] == "invoice") == ["X1", "X2"]    # keyed by kind + old + new
    capsys.readouterr()
    assert cmd(tmp_path, "orphans", today=False) == 0
    out = capsys.readouterr().out
    assert "invoice-chaser rebind INV-101 X1" in out and "invoice-chaser rebind INV-101 X2" in out
    assert cmd(tmp_path, "rebind", "INV-101", "X1", today=False) == 0
    assert load_paid(ledger(tmp_path)).keys() == {"X1"}                                    # the other candidate row is gone with it
    assert cmd(tmp_path, "orphans", today=False) == 0 and "no pending orphaned decisions" in capsys.readouterr().out
    code, rows = run(tmp_path)
    assert rows["X1"]["status"] == "paid" and rows["X2"]["status"] != "paid"


def test_i3_rebind_uses_todays_exports_not_a_saved_orphans_file(tmp_path, capsys):
    setup(tmp_path)
    key = confirmed_101(tmp_path)
    old = "wise:old-format-9001"

    def go(data):
        data["paid"]["INV-101"]["payments"] = [old]
        data["paid"]["INV-101"]["payment_rows"][0]["key"] = old
    edit_ledger(tmp_path, go)
    run(tmp_path)
    (tmp_path / "out" / "orphans.json").unlink()                                             # no saved candidates at all
    assert cmd(tmp_path, "rebind", old, key, today=False) == 0
    assert load_paid(ledger(tmp_path))["INV-101"]["payments"] == [key]


def test_i3_forget_finds_the_orphan_in_current_exports_without_a_saved_file(tmp_path):
    setup(tmp_path)
    key = confirmed_101(tmp_path)
    (tmp_path / "w.csv").write_text(WISE_HEAD + ROW_A.replace('"",', '"R78",'), encoding="utf-8")
    run(tmp_path)
    (tmp_path / "out" / "orphans.json").unlink()
    assert cmd(tmp_path, "forget", key, today=False) == 0
    assert load_payment_rows(ledger(tmp_path)) == []


def test_i3_orphans_command_prints_the_exact_commands(tmp_path, capsys):
    setup(tmp_path)
    key = confirmed_101(tmp_path)
    capsys.readouterr()
    assert cmd(tmp_path, "orphans", today=False) == 0
    assert "no pending orphaned decisions" in capsys.readouterr().out
    (tmp_path / "w.csv").write_text(WISE_HEAD + ROW_A.replace('"",', '"R77",'), encoding="utf-8")
    run(tmp_path)
    capsys.readouterr()
    assert cmd(tmp_path, "orphans", today=False) == 0
    out = capsys.readouterr().out
    assert f"invoice-chaser rebind {key} wise:R77" in out and f"invoice-chaser forget {key}" in out


# ---- I4: renaming or rebinding never overwrites an existing decision ------------------------------------------

def test_i4_rename_refuses_a_target_that_already_has_decisions(tmp_path):
    setup(tmp_path)
    confirmed_101(tmp_path)

    def go(data):
        data["rejected"]["INV-102"] = ["wise:zzz"]
        data["rejected_rows"]["INV-102"] = {"wise:zzz": {"date": "2026-10-05", "amount": "1", "currency": "USD", "payer": "", "text": ""}}
    edit_ledger(tmp_path, go)
    with pytest.raises(ValueError, match="already has decisions"):
        rename_invoice(ledger(tmp_path), "INV-101", "INV-102")
    assert "INV-101" in load_paid(ledger(tmp_path)) and load_rejected(ledger(tmp_path))["INV-102"] == ["wise:zzz"]


def test_i4_rebind_keys_refuses_a_target_key_that_is_already_rejected(tmp_path):
    key = confirmed_101(tmp_path)
    other = "wise:already-rejected"

    def go(data):
        data["rejected"]["INV-102"] = [other]
        data["rejected_rows"]["INV-102"] = {other: {"date": "2026-10-05", "amount": "500.00", "currency": "USD", "payer": "Holding Co", "text": ""}}
    edit_ledger(tmp_path, go)
    with pytest.raises(ValueError, match="already has a decision"):
        rebind_keys(ledger(tmp_path), {key: other})
    assert load_paid(ledger(tmp_path))["INV-101"]["payments"] == [key]


# ---- exit codes and the held display ------------------------------------------------------------------------

def test_a_clean_run_exits_0(tmp_path):
    setup(tmp_path)
    code, _ = run(tmp_path)
    assert code == 0


def test_a_critical_notice_exits_2(tmp_path):
    confirmed_101(tmp_path)
    (tmp_path / "w.csv").write_text(WISE_HEAD + '"R9","05-10-2026","500.00","USD","Payment INV-101","INV-101","Holding Co"\n', encoding="utf-8")
    code, _ = run(tmp_path)
    assert code == 2                                                                         # an orphaned confirmation is CRITICAL


def test_readme_documents_exit_code_2_and_the_new_commands():
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    for needle in ("## Exit codes", "exits with code **2**", "PAYMENT_DOUBLE_BOUND", "invoice-chaser unbind", "invoice-chaser orphans", "held:"):
        assert needle in readme, needle
