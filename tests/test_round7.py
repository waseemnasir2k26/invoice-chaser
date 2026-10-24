import csv
import json
import os
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from invoice_chaser import cli
from invoice_chaser.cli import main
from invoice_chaser.dedupe import is_hash_id, payment_row, repeat_of
from invoice_chaser.decision import has_negative_context, visible
from invoice_chaser.ledger import atomic_write_text, load_paid, load_payment_rows, record_sent
from invoice_chaser.models import Payment
from invoice_chaser.parsers import ParseOptions, parse_wise

ROOT = Path(__file__).resolve().parent.parent
INV_HEAD = "id,client,email,amount,currency,issued,due\n"
WISE_HEAD = '"TransferWise ID","Date","Amount","Currency","Description","Payment Reference","Payer Name"\n'
TWO = ("INV-2001,Holding Co,a@x.example,500,USD,2026-09-01,2026-10-01\nINV-2002,Holding Co,b@x.example,500,USD,2026-09-01,2026-10-01\n")


def setup(tmp_path, invoices, wise):
    (tmp_path / "i.csv").write_text(INV_HEAD + invoices, encoding="utf-8")
    (tmp_path / "w.csv").write_text(WISE_HEAD + wise, encoding="utf-8")
    (tmp_path / "c.json").write_text(json.dumps({"sender_email": "me@x.co"}), encoding="utf-8")


def run(tmp_path, today="2026-10-25"):
    assert main(["run", "--invoices", str(tmp_path / "i.csv"), "--wise", str(tmp_path / "w.csv"), "--config", str(tmp_path / "c.json"),
                 "--out", str(tmp_path / "out"), "--today", today]) == 0
    return {r["invoice_id"]: r for r in csv.DictReader(open(tmp_path / "out" / "status.csv", encoding="utf-8"))}


def confirm(tmp_path, inv):
    return main(["confirm", inv, "--out", str(tmp_path / "out"), "--today", "2026-10-25"])


# ---- M1: two genuine payments are not "repeats" ----------------------------------------------------

def test_two_same_day_equal_payments_with_their_own_transaction_ids_are_both_confirmable(tmp_path):
    setup(tmp_path, TWO, '"P-1","05-10-2026","500.00","USD","Payment INV-2001","INV-2001","Holding Co"\n'
                         '"P-2","05-10-2026","500.00","USD","Payment INV-2002","INV-2002","Holding Co"\n')
    run(tmp_path)
    assert confirm(tmp_path, "INV-2001") == 0
    rows = run(tmp_path)
    assert rows["INV-2002"]["status"] == "suggested_paid", "P-2 has its own bank transaction id: it is not a repeat of P-1"
    assert confirm(tmp_path, "INV-2002") == 0
    assert set(load_paid(tmp_path / "out" / "sent.json")) == {"INV-2001", "INV-2002"}


def test_without_transaction_ids_a_payment_naming_another_invoice_is_not_a_repeat(tmp_path):
    setup(tmp_path, TWO, '"","05-10-2026","500.00","USD","Payment INV-2001","INV-2001","Holding Co"\n'
                         '"","05-10-2026","500.00","USD","Payment INV-2002","INV-2002","Holding Co"\n')
    run(tmp_path)
    assert confirm(tmp_path, "INV-2001") == 0
    assert run(tmp_path)["INV-2002"]["status"] == "suggested_paid"
    assert confirm(tmp_path, "INV-2002") == 0


def test_repeat_of_rules():
    row = {"invoice": "INV-1", **payment_row({"key": "wise:h-aaaaaaaaaaaa", "id": "h-aaaaaaaaaaaa", "date": "2026-10-05", "amount": "500",
                                              "currency": "USD", "payer": "Acme Ltd", "description": "x"})}
    mk = lambda pid, payer="ACME LIMITED": Payment(pid, date(2026, 10, 5), Decimal("500"), "USD", "d", payer, "wise")      # noqa: E731
    assert repeat_of(mk("h-bbbbbbbbbbbb"), [row])                                    # hash id, same payer (renamed): a repeat
    assert repeat_of(mk("h-bbbbbbbbbbbb"), [row], named=["INV-1"])                   # names the SAME invoice: still held
    assert not repeat_of(mk("h-bbbbbbbbbbbb"), [row], named=["INV-2"])               # names a different invoice
    assert not repeat_of(mk("TXN-9"), [{**row, "key": "wise:TXN-8"}])                # two different real transaction ids
    assert repeat_of(mk("TXN-9"), [row])                                             # one side has no txn id: cannot tell, hold
    assert not repeat_of(mk("h-bbbbbbbbbbbb", payer="Beta Works"), [row])
    assert is_hash_id("h-0123456789ab") and not is_hash_id("TXN-1") and not is_hash_id("h-xyz")


def test_a_remainder_equal_to_the_deposit_is_held_with_honest_advice(tmp_path):
    setup(tmp_path, "INV-1,Acme Studio,a@x.example,1000,USD,2026-09-01,2026-10-01\n",
          '"","05-10-2026","500.00","USD","Deposit INV-1","INV-1","Acme Studio"\n')
    run(tmp_path)
    assert confirm(tmp_path, "INV-1") == 0
    (tmp_path / "w.csv").write_text(WISE_HEAD + '"","05-10-2026","500.00","USD","Deposit INV-1","INV-1","Acme Studio"\n'
                                    '"","05-10-2026","500.00","USD","Balance INV-1 final","INV-1","Acme Studio"\n', encoding="utf-8")
    rows = run(tmp_path)
    assert rows["INV-1"]["status"] == "needs_review" and "possible repeat" in rows["INV-1"]["why_not_paid"]
    entry = {"invoice_id": "INV-1", "payments": [{"key": "wise:h-cccccccccccc", "id": "h-cccccccccccc", "date": "2026-10-05",
                                                   "amount": "500", "currency": "USD", "payer": "Acme Studio", "named": ["INV-1"],
                                                   "description": "Balance"}]}
    msgs = cli._repeat_problems(entry, load_payment_rows(tmp_path / "out" / "sent.json"))
    assert len(msgs) == 1 and "mark-paid INV-1" in msgs[0] and "held to be safe" in msgs[0]
    assert "reject" not in msgs[0], "reject must never be the suggested remedy"


# ---- confirm --all-strong: all or nothing ---------------------------------------------------------

def test_all_strong_checks_everything_first_and_writes_nothing_if_one_is_held_back(tmp_path, monkeypatch, capsys):
    setup(tmp_path, TWO, '"P-1","05-10-2026","500.00","USD","Payment INV-2001","INV-2001","Holding Co"\n'
                         '"P-2","06-10-2026","500.00","USD","Payment INV-2002","INV-2002","Holding Co"\n')
    run(tmp_path)
    real = cli.repeat_of
    monkeypatch.setattr(cli, "repeat_of", lambda p, rows, named=(): {"invoice": "INV-X"} if p["id"] == "P-2" else real(p, rows, named))
    capsys.readouterr()
    assert main(["confirm", "--all-strong", "--yes", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 2
    err = capsys.readouterr().err
    assert "nothing was recorded" in err and "INV-2002" in err and "mark-paid INV-2002" in err
    assert not (tmp_path / "out" / "sent.json").exists()                       # not even INV-2001
    monkeypatch.undo()
    assert main(["confirm", "--all-strong", "--yes", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 0
    assert set(load_paid(tmp_path / "out" / "sent.json")) == {"INV-2001", "INV-2002"}


def test_all_strong_writes_the_ledger_once(tmp_path, monkeypatch):
    setup(tmp_path, TWO, '"P-1","05-10-2026","500.00","USD","Payment INV-2001","INV-2001","Holding Co"\n'
                         '"P-2","06-10-2026","500.00","USD","Payment INV-2002","INV-2002","Holding Co"\n')
    run(tmp_path)
    from invoice_chaser import ledger
    writes = []
    real = ledger._write
    monkeypatch.setattr(ledger, "_write", lambda path, data: (writes.append(1), real(path, data))[1])
    assert main(["confirm", "--all-strong", "--yes", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 0
    assert len(writes) == 1


# ---- M2: a renamed payer ----------------------------------------------------------------------------

def test_a_payer_rename_keeps_the_payment_id(tmp_path):
    f = tmp_path / "w.csv"
    f.write_text(WISE_HEAD + '"","05-10-2026","500.00","USD","Payment INV-1","","ACME LTD"\n', encoding="utf-8")
    a = parse_wise(f)[0].id
    f.write_text(WISE_HEAD + '"","05-10-2026","500.00","USD","Payment INV-1","","Acme Limited"\n', encoding="utf-8")
    assert parse_wise(f)[0].id == a


def test_equal_payments_from_different_payers_keep_distinct_ids_whatever_the_order(tmp_path):
    f = tmp_path / "w.csv"
    rows = ['"","05-10-2026","500.00","USD","transfer","","Alpha"\n', '"","05-10-2026","500.00","USD","transfer","","Beta"\n']
    f.write_text(WISE_HEAD + "".join(rows), encoding="utf-8")
    first = {p.payer: p.id for p in parse_wise(f)}
    f.write_text(WISE_HEAD + "".join(reversed(rows)), encoding="utf-8")
    second = {p.payer: p.id for p in parse_wise(f)}
    assert first == second and first["Alpha"] != first["Beta"]


def test_payer_rename_plus_new_wording_is_still_a_repeat(tmp_path):
    setup(tmp_path, "INV-1,Acme Ltd,a@x.example,1000,USD,2026-09-01,2026-10-01\n",
          '"","05-10-2026","500.00","USD","Deposit INV-1","INV-1","ACME LTD"\n')
    run(tmp_path)
    assert confirm(tmp_path, "INV-1") == 0
    (tmp_path / "w.csv").write_text(WISE_HEAD + '"","05-10-2026","500.00","USD","Received from client","","Acme Limited"\n', encoding="utf-8")
    rows = run(tmp_path)
    assert rows["INV-1"]["status"] in ("needs_review", "partial") and rows["INV-1"]["status"] != "suggested_paid"
    assert Decimal(rows["INV-1"]["paid"]) <= Decimal("500")


def test_a_total_that_depends_on_a_confirmed_part_payment_is_flagged(tmp_path, capsys):
    setup(tmp_path, "INV-1,Acme Studio,a@x.example,1000,USD,2026-09-01,2026-10-01\n",
          '"T-1","05-10-2026","500.00","USD","Deposit INV-1","INV-1","Acme Studio"\n')
    run(tmp_path)
    confirm(tmp_path, "INV-1")
    (tmp_path / "w.csv").write_text(WISE_HEAD + '"T-1","05-10-2026","500.00","USD","Deposit INV-1","INV-1","Acme Studio"\n'
                                    '"T-2","20-10-2026","500.00","USD","Balance INV-1","INV-1","Acme Studio"\n', encoding="utf-8")
    capsys.readouterr()
    rows = run(tmp_path)
    assert rows["INV-1"]["status"] == "suggested_paid"
    assert "only because it adds up with the part-payment of 500" in capsys.readouterr().err
    sug = json.loads((tmp_path / "out" / "suggestions.json").read_text(encoding="utf-8"))["suggestions"]
    assert "adds up with the 500" in sug[0]["reason"] and "you confirmed earlier" in sug[0]["reason"]


# ---- C: durable ledger and seen.json ------------------------------------------------------------------

def test_ledger_keeps_a_backup_and_cleans_orphans(tmp_path):
    ledger = tmp_path / "sent.json"
    record_sent(ledger, "INV-1", "nudge", date(2026, 10, 1))
    assert not (tmp_path / "sent.json.bak").exists()
    record_sent(ledger, "INV-2", "nudge", date(2026, 10, 1))
    assert set(json.loads((tmp_path / "sent.json.bak").read_text(encoding="utf-8"))["sent"]) == {"INV-1"}
    old, new = tmp_path / "sent.json.123.dead.tmp", tmp_path / "sent.json.456.live.tmp"
    old.write_text("x")
    new.write_text("x")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    record_sent(ledger, "INV-3", "nudge", date(2026, 10, 1))
    assert not old.exists() and new.exists()                                    # only orphans older than a minute go


def test_atomic_write_leaves_no_temp_file_and_survives_a_failure(tmp_path, monkeypatch):
    target = tmp_path / "seen.json"
    atomic_write_text(target, "one")
    assert target.read_text() == "one" and not list(tmp_path.glob("*.tmp"))
    monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        atomic_write_text(target, "two")
    assert target.read_text() == "one" and not list(tmp_path.glob("*.tmp"))


def test_a_corrupt_seen_json_is_kept_and_reported_not_silently_reset(tmp_path, capsys):
    setup(tmp_path, "INV-1,Acme Studio,a@x.example,1000,USD,2026-09-01,2026-10-01\n", '"W1","05-10-2026","1000.00","USD","transfer","","Stranger"\n')
    run(tmp_path, today="2026-10-10")
    (tmp_path / "out" / "seen.json").write_text("{ this is not json", encoding="utf-8")
    capsys.readouterr()
    run(tmp_path, today="2026-10-20")
    err = capsys.readouterr().err
    assert "seen.json could not be read" in err and "Waiting times restart today" in err
    backups = list((tmp_path / "out").glob("seen.json.corrupt-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "{ this is not json"
    assert "seen.json could not be read" in (tmp_path / "out" / "review.html").read_text(encoding="utf-8")


# ---- A: hiding a refund word -----------------------------------------------------------------------------

HIDDEN = ["r e f u n d", "R E F U N D", "r.e.f.u.n.d", "re​fund", "ref­und", "re⁠fund", "ref﻿und", "Rеfund",
          "rеfund", "refünd", "Réfund", "rückerstattung", "RUCKERSTATTUNG", "un​paid", "c h a r g e b a c k", "n s f",
          "re​​f​und", "Ｒｅｆｕｎｄ", "unpаid", "stornо"]


@pytest.mark.parametrize("text", HIDDEN)
def test_hidden_refund_words_are_still_found(text):
    assert has_negative_context(f"{text} INV-1004"), repr(text)


@pytest.mark.parametrize("text", ["Payment INV-1004 thanks", "A B C Ltd", "Stornoway", "Returnly", "Q a b", "Zoe Park"])
def test_ordinary_text_is_not_flagged(text):
    assert not has_negative_context(text)


def test_invisible_characters_are_shown_in_the_confirm_display(tmp_path, capsys):
    assert visible("a​b­c⁠d") == "a⟨ZWSP⟩b⟨SHY⟩c⟨WJ⟩d"
    setup(tmp_path, "INV-1,Acme Studio,a@x.example,1000,USD,2026-09-01,2026-10-01\n",
          '"W1","05-10-2026","1000.00","USD","Pay​ment INV-1","INV-1","Acme​ Studio"\n')
    run(tmp_path)
    capsys.readouterr()
    assert main(["confirm", "--all-strong", "--yes", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 0
    out = capsys.readouterr().out
    assert "Pay⟨ZWSP⟩ment" in out and "Acme⟨ZWSP⟩ Studio" in out
    assert "Pay⟨ZWSP⟩ment" in (tmp_path / "out" / "review.html").read_text(encoding="utf-8")


# ---- README ----------------------------------------------------------------------------------------------

def test_readme_notes_and_first_screen():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "UTC offset" in text
    top = text[:text.index("## What it does")]
    assert "never marks paid on a guess, never chases a client who paid" in top.lower()
    assert "What it will not do" in top and "docs/flow.png" in top
    assert len(top.splitlines()) < 60
