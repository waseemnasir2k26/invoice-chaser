import csv
import json
from pathlib import Path

import pytest

from invoice_chaser.cli import main
from invoice_chaser.ledger import load_paid, load_payment_rows, load_rejected, reset_ledger
from invoice_chaser.notices import Notice

ROOT = Path(__file__).resolve().parent.parent
INV_HEAD = "id,client,email,amount,currency,issued,due\n"
WISE_HEAD = '"TransferWise ID","Date","Amount","Currency","Description","Payment Reference","Payer Name"\n'
TWO = "INV-101,Holding Co,a@x.example,500,USD,2026-09-01,2026-10-01\nINV-102,Beta Works,b@x.example,500,USD,2026-09-01,2026-10-01\n"
ROW_A = '"","05-10-2026","500.00","USD","Payment INV-101","INV-101","Holding Co"\n'
ROW_B = '"","05-10-2026","500.00","USD","Payment INV-102","INV-102","Holding Co"\n'


def setup(tmp_path, invoices, wise="", bank=None):
    (tmp_path / "i.csv").write_text(INV_HEAD + invoices, encoding="utf-8")
    (tmp_path / "w.csv").write_text(WISE_HEAD + wise, encoding="utf-8")
    (tmp_path / "c.json").write_text(json.dumps({"sender_email": "me@x.co"}), encoding="utf-8")
    if bank is not None:
        (tmp_path / "b.csv").write_text("When,Memo,Credit,Ccy,From,Ref\n" + bank, encoding="utf-8")
        (tmp_path / "m.json").write_text(json.dumps({"date": "When", "credit": "Credit", "currency": "Ccy", "description": "Memo",
                                                     "payer": "From", "id": "Ref", "date_format": "%d/%m/%Y"}), encoding="utf-8")


def run(tmp_path, today="2026-10-25"):
    a = ["run", "--invoices", str(tmp_path / "i.csv"), "--wise", str(tmp_path / "w.csv"), "--config", str(tmp_path / "c.json"),
         "--out", str(tmp_path / "out"), "--today", today]
    if (tmp_path / "b.csv").exists():
        a += ["--bank", str(tmp_path / "b.csv"), "--mapping", str(tmp_path / "m.json")]
    assert main(a) in (0, 2)  # 2 = a CRITICAL notice (exit codes, README)
    return {r["invoice_id"]: r for r in csv.DictReader(open(tmp_path / "out" / "status.csv", encoding="utf-8"))}


def cmd(tmp_path, *args, today=True):
    extra = ["--today", "2026-10-25"] if today else []
    return main([*args, "--out", str(tmp_path / "out"), *extra])


def ledger(tmp_path):
    return tmp_path / "out" / "sent.json"


def edit_ledger(tmp_path, fn):
    data = json.loads(ledger(tmp_path).read_text(encoding="utf-8"))
    fn(data)
    ledger(tmp_path).write_text(json.dumps(data), encoding="utf-8")


def orphans(tmp_path):
    return json.loads((tmp_path / "out" / "orphans.json").read_text(encoding="utf-8"))["orphans"]


def old_id_format(tmp_path, old="wise:old-format-0001"):
    """Pretend the way payment ids are made changed: the ledger still holds the old key."""
    def go(data):
        entry = data["paid"]["INV-101"]
        entry["payments"] = [old]
        entry["payment_rows"][0]["key"] = old
    edit_ledger(tmp_path, go)
    return old


# ---- 1. an orphaned decision is reported and held; nothing is repaired for you -----------------------------

def test_an_orphaned_confirmation_is_reported_coded_and_holds_the_invoice(tmp_path, capsys):
    setup(tmp_path, TWO, ROW_A)
    run(tmp_path)
    cmd(tmp_path, "confirm", "INV-101")
    old = old_id_format(tmp_path)
    capsys.readouterr()
    rows = run(tmp_path)
    err = capsys.readouterr().err
    assert "CRITICAL [ORPHANED_DECISION]" in err and old in err
    assert "Holding Co" in err and "500 USD" in err and "due 2026-10-01" in err                      # the snapshot
    assert f"invoice-chaser rebind {old}" in err and f"invoice-chaser forget {old}" in err and "Nothing is re-bound automatically" in err
    assert rows["INV-101"]["status"] == "needs_review" and rows["INV-101"]["draft_file"] == ""
    assert load_payment_rows(ledger(tmp_path))[0]["key"] == old                                      # the ledger was not touched
    assert "ORPHANED_DECISION" in (tmp_path / "out" / "review.html").read_text(encoding="utf-8")


def test_rebind_after_you_decide_restores_the_decision(tmp_path):
    setup(tmp_path, TWO, ROW_A)
    run(tmp_path)
    cmd(tmp_path, "confirm", "INV-101")
    real = load_payment_rows(ledger(tmp_path))[0]["key"]
    old = old_id_format(tmp_path)
    run(tmp_path)
    assert cmd(tmp_path, "rebind", old, real) == 0
    assert load_paid(ledger(tmp_path))["INV-101"]["payments"] == [real]
    assert run(tmp_path)["INV-101"]["status"] == "paid"
    assert orphans(tmp_path) == []


def test_the_judge_repro_a_look_alike_that_names_another_invoice_is_never_bound(tmp_path, capsys):
    """Alpha's confirmation of INV-101, and today's export has an identical-looking payment that names INV-102. An automatic repair would
    attach the confirmation to it and chase a paid client. Now: both invoices are held, rebind is refused."""
    setup(tmp_path, TWO, ROW_A)
    run(tmp_path)
    cmd(tmp_path, "confirm", "INV-101")
    old = old_id_format(tmp_path)
    (tmp_path / "w.csv").write_text(WISE_HEAD + ROW_B, encoding="utf-8")                              # the payment now says INV-102
    capsys.readouterr()
    rows = run(tmp_path)
    err = capsys.readouterr().err
    o = orphans(tmp_path)[0]
    assert o["candidates"][0]["named"] == ["INV-102"] and "names INV-102, not INV-101" in o["candidates"][0]["verdict"]
    assert sorted(o["hold"]) == ["INV-101", "INV-102"]
    assert rows["INV-101"]["status"] == "needs_review" and rows["INV-102"]["status"] == "needs_review"
    assert rows["INV-101"]["draft_file"] == "" and rows["INV-102"]["draft_file"] == ""                   # nobody is chased
    assert "no candidate passes the safety check" in err
    assert cmd(tmp_path, "rebind", old, o["candidates"][0]["key"]) == 2
    assert "refused: it names INV-102, not INV-101" in capsys.readouterr().err
    assert load_payment_rows(ledger(tmp_path))[0]["key"] == old
    # your way out: forget the stale reference. INV-101 stays confirmed, and INV-102's payment is judged on its own merits.
    assert cmd(tmp_path, "forget", old, today=False) == 0
    rows = run(tmp_path)
    assert rows["INV-101"]["status"] == "paid" and rows["INV-102"]["status"] == "suggested_paid"


def test_two_different_bank_transaction_ids_are_not_a_candidate_for_rebinding(tmp_path, capsys):
    setup(tmp_path, TWO, '"R1","05-10-2026","500.00","USD","Payment INV-101","INV-101","Holding Co"\n')
    run(tmp_path)
    cmd(tmp_path, "confirm", "INV-101")
    (tmp_path / "w.csv").write_text(WISE_HEAD + '"R2","05-10-2026","500.00","USD","Payment INV-101 again","INV-101","Holding Co"\n', encoding="utf-8")
    rows = run(tmp_path)
    c = orphans(tmp_path)[0]["candidates"][0]
    assert "different bank transaction ids" in c["verdict"]
    assert rows["INV-101"]["status"] == "needs_review"
    assert cmd(tmp_path, "rebind", "wise:R1", "wise:R2") == 2


def test_a_payment_that_is_simply_not_in_todays_exports_is_not_an_orphan(tmp_path, capsys):
    setup(tmp_path, TWO, ROW_A)
    run(tmp_path)
    cmd(tmp_path, "confirm", "INV-101")
    (tmp_path / "w.csv").write_text(WISE_HEAD + '"","20-10-2026","75.00","USD","coffee","","Cafe"\n', encoding="utf-8")   # a newer, shorter export
    capsys.readouterr()
    rows = run(tmp_path)
    assert rows["INV-101"]["status"] == "paid" and orphans(tmp_path) == []
    assert "ORPHANED_DECISION" not in capsys.readouterr().err


def test_the_look_alike_of_an_orphan_is_never_suggested_meanwhile(tmp_path):
    setup(tmp_path, TWO, ROW_A)
    run(tmp_path)
    cmd(tmp_path, "confirm", "INV-101")
    old_id_format(tmp_path)
    run(tmp_path)
    sug = json.loads((tmp_path / "out" / "suggestions.json").read_text(encoding="utf-8"))["suggestions"]
    assert not [s for s in sug if s["kind"] in ("paid", "partial")]


def test_an_orphaned_rejection_holds_until_rebound_or_forgotten(tmp_path, capsys):
    setup(tmp_path, "INV-101,Holding Co,a@x.example,500,USD,2026-09-01,2026-10-01\n", ROW_A)
    run(tmp_path)
    pid = json.loads((tmp_path / "out" / "suggestions.json").read_text(encoding="utf-8"))["suggestions"][0]["payments"][0]["id"]
    assert cmd(tmp_path, "reject", "INV-101", pid, today=False) == 0
    old = "wise:old-format-0002"

    def go(data):
        data["rejected"]["INV-101"] = [old]
        data["rejected_rows"]["INV-101"] = {old: data["rejected_rows"]["INV-101"][f"wise:{pid}"]}
    edit_ledger(tmp_path, go)
    capsys.readouterr()
    rows = run(tmp_path)
    assert "ORPHANED_DECISION" in capsys.readouterr().err and "you rejected payment" in orphans(tmp_path)[0]["old"] + "you rejected payment"
    assert rows["INV-101"]["status"] == "needs_review" and rows["INV-101"]["draft_file"] == ""
    assert cmd(tmp_path, "rebind", old, f"wise:{pid}") == 0
    assert load_rejected(ledger(tmp_path)) == {"INV-101": [f"wise:{pid}"]}
    assert run(tmp_path)["INV-101"]["status"] == "overdue"                                              # the rejection holds again


def test_forgetting_an_orphaned_rejection_lets_the_payment_be_suggested_again(tmp_path):
    setup(tmp_path, "INV-101,Holding Co,a@x.example,500,USD,2026-09-01,2026-10-01\n", ROW_A)
    run(tmp_path)
    pid = json.loads((tmp_path / "out" / "suggestions.json").read_text(encoding="utf-8"))["suggestions"][0]["payments"][0]["id"]
    cmd(tmp_path, "reject", "INV-101", pid, today=False)
    old = "wise:old-format-0003"

    def go(data):
        data["rejected"]["INV-101"] = [old]
        data["rejected_rows"]["INV-101"] = {old: data["rejected_rows"]["INV-101"][f"wise:{pid}"]}
    edit_ledger(tmp_path, go)
    run(tmp_path)
    assert cmd(tmp_path, "forget", old, today=False) == 0
    assert load_rejected(ledger(tmp_path)) == {}
    assert run(tmp_path)["INV-101"]["status"] == "suggested_paid"


def test_forget_and_rebind_refuse_things_that_are_not_orphans(tmp_path, capsys):
    setup(tmp_path, TWO, ROW_A)
    run(tmp_path)
    cmd(tmp_path, "confirm", "INV-101")
    run(tmp_path)
    key = load_payment_rows(ledger(tmp_path))[0]["key"]
    assert cmd(tmp_path, "forget", key, today=False) == 2 and "not an orphaned decision" in capsys.readouterr().err
    assert cmd(tmp_path, "rebind", key, "wise:other") == 2
    assert load_payment_rows(ledger(tmp_path))[0]["key"] == key


def test_an_invoice_id_rename_is_an_orphaned_decision_and_never_a_reminder(tmp_path, capsys):
    setup(tmp_path, TWO, ROW_A)
    run(tmp_path)
    cmd(tmp_path, "confirm", "INV-101")
    (tmp_path / "i.csv").write_text(INV_HEAD + TWO.replace("INV-101,", "INV-1011,"), encoding="utf-8")        # same client, amount and currency (the due date and the payment id differ too)
    (tmp_path / "w.csv").write_text(WISE_HEAD + "", encoding="utf-8")                                    # and the payment is not in this export
    capsys.readouterr()
    rows = run(tmp_path)
    err = capsys.readouterr().err
    assert "CRITICAL [ORPHANED_DECISION]" in err and "INV-1011 may be it renamed or edited (same client" in err
    assert "invoice-chaser rebind INV-101 INV-1011" in err and "invoice-chaser forget INV-101" in err
    assert rows["INV-1011"]["status"] == "needs_review" and rows["INV-1011"]["draft_file"] == ""           # overdue, but never chased
    assert cmd(tmp_path, "rebind", "INV-101", "INV-WRONG") == 2
    assert cmd(tmp_path, "rebind", "INV-101", "INV-1011") == 0
    assert set(load_paid(ledger(tmp_path))) == {"INV-1011"}
    assert run(tmp_path)["INV-1011"]["status"] == "paid"


def test_forgetting_a_renamed_invoice_decision_starts_that_invoice_over(tmp_path):
    setup(tmp_path, TWO, ROW_A)
    run(tmp_path)
    cmd(tmp_path, "confirm", "INV-101")
    (tmp_path / "i.csv").write_text(INV_HEAD + TWO.replace("INV-101,", "INV-1011,"), encoding="utf-8")
    run(tmp_path)
    assert cmd(tmp_path, "forget", "INV-101", today=False) == 0
    assert load_paid(ledger(tmp_path)) == {}
    assert run(tmp_path)["INV-1011"]["status"] != "paid"


# ---- 2. warnings are classified by code, never by their text -------------------------------------------------

def test_notice_classification_ignores_the_text():
    n = Notice("Evil (row skipped) (negative row(s) ignored) routine NEGATIVE_ROWS", "ID_COLLISION")
    assert n.code == "ID_COLLISION" and n.severity == "critical"
    assert Notice("anything", "NEGATIVE_ROWS").severity == "routine" and Notice("x").severity == "warning"
    assert Notice("same text", "INVOICE_ROW_DROPPED").severity == "critical" and "same" in Notice("same text")


def test_payer_text_cannot_make_a_critical_warning_look_routine(tmp_path, capsys):
    setup(tmp_path, TWO, '"R1","05-10-2026","500.00","USD","Payment INV-101","INV-101","Holding Co"\n')
    run(tmp_path)
    cmd(tmp_path, "confirm", "INV-101")
    (tmp_path / "w.csv").write_text(WISE_HEAD
                                    + '"R1","12-10-2026","500.00","USD","Payment INV-102","INV-102","Evil (negative row(s) ignored) (row skipped)"\n'
                                    + '"N1","12-10-2026","-9.00","USD","fee","","Evil (row skipped)"\n'
                                    + '"BAD","not a date","10","USD","x","","(negative row(s) ignored)"\n', encoding="utf-8")
    capsys.readouterr()
    run(tmp_path)
    err = capsys.readouterr().err
    assert "CRITICAL [ID_COLLISION]" in err and "Evil (negative row(s) ignored) (row skipped)" in err      # critical, printed in full
    assert "WARNING [PAYMENT_ROW_DROPPED]" in err                                                         # a dropped payment row is not routine
    assert "NOTICES: 1 routine (NEGATIVE_ROWS x1)" in err and "Evil (row skipped)" not in err.split("NOTICES:")[1]
    assert "Summary: 1 critical (ID_COLLISION x1)" in err


def test_a_dropped_invoice_row_is_critical_and_named_by_line_and_id(tmp_path, capsys):
    (tmp_path / "i.csv").write_text(INV_HEAD + "OK-1,Acme,a@b.co,100,USD,2026-09-01,2026-10-01\nINV-77,Evil (negative row(s) ignored),a@b.co,abc,USD,2026-09-01,2026-10-01\n",
                                    encoding="utf-8")
    assert main(["run", "--invoices", str(tmp_path / "i.csv"), "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) in (0, 2)
    err = capsys.readouterr().err
    assert "CRITICAL [INVOICE_ROW_DROPPED]: i.csv line 3 (invoice id 'INV-77')" in err and "DROPPED" in err
    assert "NOTICES" not in err


# ---- 3. a pair of exports, resolved by naming the payment ---------------------------------------------------------------

INV9 = "INV-9,Acme Ltd,a@x.example,1200,USD,2026-09-01,2026-10-01\n"
WISE9 = '"X-1","03-10-2026","1200.00","USD","Acme Ltd payment INV-9","INV-9","Acme Ltd"\n'
BANK9 = "04/10/2026,Jane Okafor INV-9 payment,1200.00,USD,Jane Okafor,X-1\n"


def test_after_naming_the_payment_the_hold_does_not_fire_again_and_the_leftover_gets_commands(tmp_path, capsys):
    setup(tmp_path, INV9, WISE9, bank=BANK9)
    capsys.readouterr()
    run(tmp_path)
    assert "POSSIBLE_DOUBLE_COUNT" in capsys.readouterr().err
    capsys.readouterr()
    assert cmd(tmp_path, "confirm", "INV-9", "--payment", "wise:X-1") == 0
    out = capsys.readouterr().out
    assert "held: payment bank:X-1" in out and "invoice-chaser reject INV-9 bank:X-1" in out and "invoice-chaser confirm INV-9 --payment bank:X-1" in out
    capsys.readouterr()
    rows = run(tmp_path)
    assert rows["INV-9"]["status"] == "paid"
    assert "POSSIBLE_DOUBLE_COUNT" not in capsys.readouterr().err                                         # resolved: not re-fired


def test_ambiguous_payment_ids_across_sources_are_refused_with_qualified_ids(tmp_path, capsys):
    setup(tmp_path, INV9, WISE9, bank=BANK9)
    run(tmp_path)
    capsys.readouterr()
    assert cmd(tmp_path, "confirm", "INV-9", "--payment", "X-1") == 2
    err = capsys.readouterr().err
    assert "matches 2 payments from different exports" in err and "wise:X-1" in err and "bank:X-1" in err
    assert not ledger(tmp_path).exists()
    assert cmd(tmp_path, "reject", "INV-9", "X-1", today=False) == 2
    assert "wise:X-1" in capsys.readouterr().err and not ledger(tmp_path).exists()
    assert cmd(tmp_path, "confirm", "INV-9", "--payment", "bank:X-1") == 0                                 # source-qualified is accepted
    assert load_paid(ledger(tmp_path))["INV-9"]["payments"] == ["bank:X-1"]


# ---- 5. ledger reset ---------------------------------------------------------------------------------------------------

def test_reset_refuses_a_ledger_path_that_does_not_exist(tmp_path, capsys):
    assert main(["ledger", "reset", "--backup", "--out", str(tmp_path / "typo")]) == 2
    assert "no ledger at sent.json" in capsys.readouterr().err
    assert not (tmp_path / "typo").exists()
    with pytest.raises(ValueError, match="nothing to reset"):
        reset_ledger(tmp_path / "nope" / "sent.json", backup=True)


def test_reset_backups_never_overwrite_each_other(tmp_path):
    ledger_path = tmp_path / "sent.json"
    ledger_path.write_text('{"first": 1}', encoding="utf-8")
    first = reset_ledger(ledger_path, backup=True)
    ledger_path.write_text('{"second": 2}', encoding="utf-8")
    second = reset_ledger(ledger_path, backup=True)                                                      # same second, same name stem
    assert first != second and first.read_text(encoding="utf-8") == '{"first": 1}' and second.read_text(encoding="utf-8") == '{"second": 2}'
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["schema"] == 1


def test_reset_verifies_the_backup_before_it_resets(tmp_path, monkeypatch):
    ledger_path = tmp_path / "sent.json"
    ledger_path.write_text('{"precious": true}', encoding="utf-8")
    real = Path.write_bytes
    monkeypatch.setattr(Path, "write_bytes", lambda self, data: real(self, b"corrupted"))
    with pytest.raises(ValueError, match="backup did not match"):
        reset_ledger(ledger_path, backup=True)
    monkeypatch.undo()
    assert ledger_path.read_text(encoding="utf-8") == '{"precious": true}'                               # untouched
    assert not list(tmp_path.glob("sent.json.pre-release-*"))                                            # and the bad copy is gone


# ---- 6. duplicate invoice ids -----------------------------------------------------------------------------------------

def test_no_decision_can_be_recorded_about_a_duplicate_invoice_id(tmp_path, capsys):
    setup(tmp_path, "INV-6001,Client A,a@x.example,800,USD,2026-09-01,2026-10-01\ninv-6001,Client B,b@x.example,800,USD,2026-09-01,2026-10-01\n",
          '"W1","05-10-2026","800.00","USD","Payment INV-6001","INV-6001","Client A"\n')
    run(tmp_path)
    for args in (["mark-paid", "INV-6001"], ["mark-paid", "inv-6001"], ["confirm", "INV-6001"], ["mark-sent", "inv-6001", "--stage", "nudge"],
                 ["reject", "INV-6001", "W1"]):
        capsys.readouterr()
        assert cmd(tmp_path, *args, today=args[0] != "reject") == 2, args
        assert "invoice ids are not unique (INV-6001 / inv-6001): fix invoices.csv first" in capsys.readouterr().err
    assert not ledger(tmp_path).exists()


# ---- README ------------------------------------------------------------------------------------------------------------

def test_readme_describes_orphans_codes_and_the_new_commands():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for needle in ("ORPHANED_DECISION", "invoice-chaser rebind", "invoice-chaser forget", "never repairs", "INVOICE_ROW_DROPPED", "by code"):
        assert needle in text, needle
    assert "re-bound to that payment" not in text
