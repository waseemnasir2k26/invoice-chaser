"""The product rule: the tool suggests, only YOU (confirm / mark-paid) can make an invoice paid, and no
reminder is ever drafted for an invoice that has a suggested payment."""
import csv
import json
from decimal import Decimal

import pytest

from invoice_chaser import cli
from invoice_chaser.cli import main
from invoice_chaser.ledger import load_paid, load_received, load_rejected

INV_HEAD = "id,client,email,amount,currency,issued,due\n"
WISE_HEAD = '"TransferWise ID","Date","Amount","Currency","Description","Payment Reference","Payer Name"\n'
CFG = {"sender_email": "me@x.co"}


def setup(tmp_path, invoices, wise):
    (tmp_path / "i.csv").write_text(INV_HEAD + invoices, encoding="utf-8")
    (tmp_path / "w.csv").write_text(WISE_HEAD + wise, encoding="utf-8")
    (tmp_path / "c.json").write_text(json.dumps(CFG), encoding="utf-8")


def run(tmp_path, today="2026-10-25"):
    code = main(["run", "--invoices", str(tmp_path / "i.csv"), "--wise", str(tmp_path / "w.csv"), "--config", str(tmp_path / "c.json"),
                 "--out", str(tmp_path / "out"), "--today", today])
    assert code == 0
    return {r["invoice_id"]: r for r in csv.DictReader(open(tmp_path / "out" / "status.csv", encoding="utf-8"))}


ONE = ("INV-1,Acme Studio,a@acme.example,1000,USD,2026-09-01,2026-10-01\n",
       '"W1","05-10-2026","1000.00","USD","Payment INV-1","INV-1","Acme Studio"\n')


def test_run_never_writes_paid_and_never_drafts_for_a_suggestion(tmp_path):
    setup(tmp_path, *ONE)
    rows = run(tmp_path)
    assert rows["INV-1"]["status"] == "suggested_paid" and rows["INV-1"]["draft_file"] == ""
    assert list((tmp_path / "out" / "drafts").glob("*.eml")) == []          # overdue 24 days, yet no reminder
    assert not (tmp_path / "out" / "sent.json").exists()                   # nothing was decided by the tool
    sug = json.loads((tmp_path / "out" / "suggestions.json").read_text(encoding="utf-8"))["suggestions"]
    assert sug[0]["kind"] == "paid" and sug[0]["payments"][0]["key"] == "wise:W1"
    html = (tmp_path / "out" / "review.html").read_text(encoding="utf-8")
    assert html.index("Confirm these") < html.index("Totals by currency")
    assert "invoice-chaser confirm INV-1" in html and "invoice-chaser reject INV-1 W1" in html


def test_confirm_writes_paid_to_the_ledger_and_the_next_run_shows_paid(tmp_path):
    setup(tmp_path, *ONE)
    run(tmp_path)
    assert main(["confirm", "inv-1", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 0
    entry = load_paid(tmp_path / "out" / "sent.json")["INV-1"]
    assert entry["source"] == "confirm" and entry["payments"] == ["wise:W1"]
    assert entry["snapshot"] == {"client": "Acme Studio", "amount": "1000", "currency": "USD", "due": "2026-10-01"}
    rows = run(tmp_path)
    assert rows["INV-1"]["status"] == "paid" and rows["INV-1"]["draft_file"] == ""
    assert "confirmed by you" in rows["INV-1"]["reason"]


def test_confirm_refuses_without_a_strong_suggestion(tmp_path, capsys):
    setup(tmp_path, "INV-1,Acme Studio,a@acme.example,1000,USD,2026-09-01,2026-10-01\n",
          '"W1","05-10-2026","1000.00","USD","transfer","","Someone Else"\n')
    rows = run(tmp_path)
    assert rows["INV-1"]["status"] == "needs_review"
    assert main(["confirm", "INV-1", "--out", str(tmp_path / "out")]) == 2
    assert "mark-paid" in capsys.readouterr().err
    assert main(["confirm", "INV-404", "--out", str(tmp_path / "out")]) == 2
    assert not (tmp_path / "out" / "sent.json").exists()


def test_reject_removes_the_payment_for_good(tmp_path):
    setup(tmp_path, *ONE)
    run(tmp_path)
    assert main(["reject", "INV-1", "W1", "--out", str(tmp_path / "out")]) == 0
    assert load_rejected(tmp_path / "out" / "sent.json") == {"INV-1": ["wise:W1"]}
    rows = run(tmp_path)
    assert rows["INV-1"]["status"] == "overdue" and rows["INV-1"]["draft_file"] != ""   # now it is chased
    assert main(["reject", "INV-1", "NOPE", "--out", str(tmp_path / "out")]) == 2


def test_confirm_all_strong_asks_and_only_then_writes(tmp_path, monkeypatch, capsys):
    setup(tmp_path, *ONE)
    run(tmp_path)
    out = str(tmp_path / "out")
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert main(["confirm", "--all-strong", "--out", out]) == 1
    assert "INV-1" in capsys.readouterr().out and not (tmp_path / "out" / "sent.json").exists()
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(EOFError()))
    assert main(["confirm", "--all-strong", "--out", out]) == 1                     # no terminal: refuse, never assume yes
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert main(["confirm", "--all-strong", "--out", out, "--today", "2026-10-25"]) == 0
    assert "INV-1" in load_paid(tmp_path / "out" / "sent.json")


def test_confirm_all_strong_yes_skips_the_question_and_skips_weak_rows(tmp_path):
    inv = ONE[0] + "INV-2,Beta Works,b@beta.example,500,USD,2026-09-01,2026-10-01\n"
    wise = ONE[1] + '"W2","05-10-2026","500.00","USD","transfer","","Stranger"\n'
    setup(tmp_path, inv, wise)
    rows = run(tmp_path)
    assert rows["INV-2"]["status"] == "needs_review"
    assert main(["confirm", "--all-strong", "--yes", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 0
    assert list(load_paid(tmp_path / "out" / "sent.json")) == ["INV-1"]


def test_confirmed_part_payment_is_chased_for_the_rest_and_a_second_payment_completes_it(tmp_path):
    inv = "INV-1,Acme Studio,a@acme.example,2500,USD,2026-09-01,2026-10-01\n"
    setup(tmp_path, inv, '"W1","05-10-2026","1000.00","USD","INV-1 deposit","INV-1","Acme Studio"\n')
    rows = run(tmp_path)
    assert rows["INV-1"]["status"] == "partial_suggested" and rows["INV-1"]["draft_file"] == ""
    assert main(["confirm", "INV-1", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 0
    assert Decimal(load_received(tmp_path / "out" / "sent.json")["INV-1"]["amount"]) == Decimal("1000")
    assert "INV-1" not in load_paid(tmp_path / "out" / "sent.json")
    rows = run(tmp_path)
    assert rows["INV-1"]["status"] == "partial" and rows["INV-1"]["outstanding"] == "1500.00" and rows["INV-1"]["draft_file"] != ""
    (tmp_path / "w.csv").write_text(WISE_HEAD + '"W1","05-10-2026","1000.00","USD","INV-1 deposit","INV-1","Acme Studio"\n'
                                    '"W2","20-10-2026","1500.00","USD","INV-1 balance","INV-1","Acme Studio"\n', encoding="utf-8")
    rows = run(tmp_path)
    assert rows["INV-1"]["status"] == "suggested_paid" and rows["INV-1"]["draft_file"] == ""   # the rest arrived: waiting again
    assert main(["confirm", "INV-1", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 0
    assert load_paid(tmp_path / "out" / "sent.json")["INV-1"]["payments"] == ["wise:W1", "wise:W2"]
    assert run(tmp_path)["INV-1"]["status"] == "paid"


# ---- the ledger snapshot -------------------------------------------------------------

@pytest.mark.parametrize("old,new", [("1000", "1200"), ("Acme Studio", "Other Client"), ("2026-10-01", "2026-12-01")])
def test_a_changed_invoice_is_no_longer_paid(tmp_path, capsys, old, new):
    setup(tmp_path, *ONE)
    run(tmp_path)
    main(["confirm", "INV-1", "--out", str(tmp_path / "out"), "--today", "2026-10-25"])
    text = (tmp_path / "i.csv").read_text(encoding="utf-8").replace(old, new)
    (tmp_path / "i.csv").write_text(text, encoding="utf-8")
    capsys.readouterr()
    rows = run(tmp_path)
    assert rows["INV-1"]["status"] == "needs_review" and rows["INV-1"]["draft_file"] == ""
    assert "invoice changed since you confirmed it" in rows["INV-1"]["why_not_paid"]
    assert "invoice changed since you confirmed it" in capsys.readouterr().err
    assert "invoice changed since you confirmed it" in (tmp_path / "out" / "review.html").read_text(encoding="utf-8")


def test_mark_paid_stores_a_snapshot_too(tmp_path):
    setup(tmp_path, *ONE)
    run(tmp_path)
    assert main(["mark-paid", "INV-1", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 0
    assert load_paid(tmp_path / "out" / "sent.json")["INV-1"]["snapshot"]["amount"] == "1000"
    (tmp_path / "i.csv").write_text((tmp_path / "i.csv").read_text(encoding="utf-8").replace("1000", "900"), encoding="utf-8")
    assert run(tmp_path)["INV-1"]["status"] == "needs_review"
