import csv
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from invoice_chaser.cli import main
from invoice_chaser.ledger import load_dropped, load_ledger, load_paid, load_rejected
from invoice_chaser.parsers import ParseOptions, parse_wise

ROOT = Path(__file__).resolve().parent.parent
INV_HEAD = "id,client,email,amount,currency,issued,due\n"
WISE_HEAD = '"TransferWise ID","Date","Amount","Currency","Description","Payment Reference","Payer Name"\n'
CFG = {"sender_email": "me@x.co"}


def setup(tmp_path, invoices, wise):
    (tmp_path / "i.csv").write_text(INV_HEAD + invoices, encoding="utf-8")
    (tmp_path / "w.csv").write_text(WISE_HEAD + wise, encoding="utf-8")
    (tmp_path / "c.json").write_text(json.dumps(CFG), encoding="utf-8")


def run(tmp_path, today="2026-10-25", out="out"):
    code = main(["run", "--invoices", str(tmp_path / "i.csv"), "--wise", str(tmp_path / "w.csv"), "--config", str(tmp_path / "c.json"),
                 "--out", str(tmp_path / out), "--today", today])
    assert code == 0
    return {r["invoice_id"]: r for r in csv.DictReader(open(tmp_path / out / "status.csv", encoding="utf-8"))}


# no transaction ids: the exports carry no id column, so ids must come from the content, never the row number
TWO = ("INV-1,Acme Studio,a@acme.example,1000,USD,2026-09-01,2026-10-01\nINV-2,Beta Works,b@beta.example,500,USD,2026-09-01,2026-10-01\n")
ROW_A = '"","05-10-2026","1000.00","USD","Payment INV-1","INV-1","Acme Studio"\n'
ROW_B = '"","06-10-2026","500.00","USD","Payment INV-2","INV-2","Beta Works"\n'
ROW_NEW = '"","04-10-2026","75.00","USD","Coffee refund","","Cafe"\n'


# ---- B1: stable payment ids ------------------------------------------------------------

def test_ids_do_not_depend_on_row_numbers(tmp_path):
    f = tmp_path / "w.csv"
    f.write_text(WISE_HEAD + ROW_A + ROW_B, encoding="utf-8")
    first = {p.payer: p.id for p in parse_wise(f)}
    f.write_text(WISE_HEAD + ROW_NEW + ROW_B + ROW_A, encoding="utf-8")        # re-export: new row, other order
    second = {p.payer: p.id for p in parse_wise(f)}
    assert first["Acme Studio"] == second["Acme Studio"] and first["Beta Works"] == second["Beta Works"]
    assert all(re.fullmatch(r"h-[0-9a-f]{12}", v) for v in first.values())
    assert not any(re.search(r"(wise|payoneer|bank)-\d+$", v) for v in first.values())


def test_a_real_transaction_id_is_used_as_is(tmp_path):
    f = tmp_path / "w.csv"
    f.write_text(WISE_HEAD + '"TRANSFER-77","05-10-2026","10.00","USD","x","","Zed"\n', encoding="utf-8")
    assert parse_wise(f)[0].id == "TRANSFER-77"


def test_identical_rows_stay_two_payments_but_the_same_row_in_two_files_is_one(tmp_path):
    f = tmp_path / "w.csv"
    f.write_text(WISE_HEAD + ROW_A + ROW_A, encoding="utf-8")
    a = parse_wise(f)
    assert len(a) == 2 and a[0].id != a[1].id
    from invoice_chaser.dedupe import dedupe_payments
    g = tmp_path / "w2.csv"
    g.write_text(WISE_HEAD + ROW_A, encoding="utf-8")
    msgs = []
    kept = dedupe_payments(a[:1] + parse_wise(g), msgs.append)
    assert len(kept) == 1 and "duplicate payment row dropped" in msgs[0]


def test_reexport_cannot_move_a_confirmation_onto_another_clients_payment(tmp_path):
    setup(tmp_path, TWO, ROW_A + ROW_B)
    run(tmp_path)
    assert main(["confirm", "INV-1", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 0
    # the bank re-exports: a new row shows up first and everything shifts down
    (tmp_path / "w.csv").write_text(WISE_HEAD + ROW_NEW + ROW_B + ROW_A, encoding="utf-8")
    rows = run(tmp_path)
    assert rows["INV-1"]["status"] == "paid"
    assert rows["INV-2"]["status"] == "suggested_paid", "Beta's payment must not have been 'consumed' by the shifted row numbers"
    assert rows["INV-2"]["draft_file"] == ""


def test_old_row_keyed_ledger_entries_are_dropped_with_a_warning_and_shown(tmp_path, capsys):
    setup(tmp_path, TWO, ROW_A + ROW_B)
    run(tmp_path)
    ledger = tmp_path / "out" / "sent.json"
    ledger.write_text(json.dumps({"sent": {}, "paid": {"INV-1": {"date": "2026-10-20", "note": "", "source": "confirm",
                                                                 "payments": ["wise:wise-2"], "snapshot": {}}},
                                  "received": {}, "rejected": {"INV-2": ["wise:wise-3", "wise:h-0123456789ab"]}}), encoding="utf-8")
    capsys.readouterr()
    rows = run(tmp_path)
    err = capsys.readouterr().err
    assert "wise:wise-2" in err and "wise:wise-3" in err and "row-numbered" in err
    assert load_paid(ledger)["INV-1"]["payments"] == [] and rows["INV-1"]["status"] == "paid"
    assert load_rejected(ledger) == {"INV-2": ["wise:h-0123456789ab"]}
    assert load_dropped(ledger) == {"INV-1": ["wise:wise-2"], "INV-2": ["wise:wise-3"]}
    assert "row-numbered" in (tmp_path / "out" / "review.html").read_text(encoding="utf-8")
    run(tmp_path)                                                           # the note stays on every run
    assert "wise:wise-3" in capsys.readouterr().err


# ---- B2: aging ------------------------------------------------------------------------

STRANGER = ("INV-1,Acme Studio,a@acme.example,1000,USD,2026-09-01,2026-10-01\n",
            '"W1","05-10-2026","1000.00","USD","transfer","","Someone Else"\n')


def test_a_waiting_review_becomes_stuck_and_an_internal_note_is_drafted(tmp_path, capsys):
    setup(tmp_path, *STRANGER)
    rows = run(tmp_path, today="2026-10-10")
    assert rows["INV-1"]["status"] == "needs_review" and rows["INV-1"]["stuck"] == "" and rows["INV-1"]["waiting_days"] == "0"
    assert not (tmp_path / "out" / "drafts" / "INV-1-check.eml").exists()
    rows = run(tmp_path, today="2026-10-17")                                # 7 days: not yet
    assert rows["INV-1"]["stuck"] == "" and rows["INV-1"]["waiting_days"] == "7"
    capsys.readouterr()
    rows = run(tmp_path, today="2026-10-18")                                # 8 days: stuck
    out = capsys.readouterr().out
    assert rows["INV-1"]["stuck"] == "yes" and rows["INV-1"]["waiting_days"] == "8"
    assert "STUCK waiting for your decision: oldest INV-1" in out
    html = (tmp_path / "out" / "review.html").read_text(encoding="utf-8")
    assert "1 invoice stuck waiting for your decision" in html and html.index("stuck waiting") < html.index("Totals by currency")
    note = (tmp_path / "out" / "drafts" / "INV-1-check.eml").read_text(encoding="utf-8")
    assert "To: me@x.co" in note and "a@acme.example" not in note            # to YOU, never the client
    assert rows["INV-1"]["draft_file"] == ""                                 # and still no reminder to the client
    first = json.loads((tmp_path / "out" / "seen.json").read_text(encoding="utf-8"))
    assert set(first.values()) == {"2026-10-10"}                             # first_seen is persisted, not reset by later runs


def test_more_than_30_days_overdue_with_a_suggestion_is_stuck_at_once(tmp_path):
    setup(tmp_path, "INV-1,Acme Studio,a@acme.example,1000,USD,2026-08-01,2026-09-01\n",
          '"W1","05-09-2026","1000.00","USD","Payment INV-1","INV-1","Acme Studio"\n')
    rows = run(tmp_path, today="2026-10-25")
    assert rows["INV-1"]["status"] == "suggested_paid" and rows["INV-1"]["stuck"] == "yes" and rows["INV-1"]["waiting_days"] == "0"


def test_deciding_ends_the_wait(tmp_path):
    setup(tmp_path, *STRANGER)
    run(tmp_path, today="2026-10-10")
    assert main(["mark-paid", "INV-1", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 0
    rows = run(tmp_path, today="2026-10-25")
    assert rows["INV-1"]["status"] == "paid" and rows["INV-1"]["stuck"] == ""
    assert json.loads((tmp_path / "out" / "seen.json").read_text(encoding="utf-8")) == {}


def test_a_rejected_suggestion_is_chased_and_says_so(tmp_path):
    setup(tmp_path, "INV-1,Acme Studio,a@acme.example,1000,USD,2026-09-01,2026-10-01\n",
          '"W1","05-10-2026","1000.00","USD","Payment INV-1","INV-1","Someone Else"\n')
    run(tmp_path)
    assert main(["reject", "INV-1", "W1", "--out", str(tmp_path / "out")]) == 0
    rows = run(tmp_path)
    assert rows["INV-1"]["status"] == "overdue" and rows["INV-1"]["draft_file"] != ""
    assert "you rejected W1 for this invoice" in rows["INV-1"]["why_not_paid"]


# ---- confirm re-validates -----------------------------------------------------------------

ONE = ("INV-1,Acme Studio,a@acme.example,1000,USD,2026-09-01,2026-10-01\n", '"W1","05-10-2026","1000.00","USD","Payment INV-1","INV-1","Acme Studio"\n')


def test_confirm_after_reject_without_a_rerun_is_refused(tmp_path, capsys):
    setup(tmp_path, *ONE)
    run(tmp_path)
    assert main(["reject", "INV-1", "W1", "--out", str(tmp_path / "out")]) == 0
    assert main(["confirm", "INV-1", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 2
    assert "ledger changed for INV-1" in capsys.readouterr().err
    assert load_paid(tmp_path / "out" / "sent.json") == {}


def test_confirm_is_refused_when_the_exports_changed_or_it_is_another_day(tmp_path, capsys):
    setup(tmp_path, *ONE)
    run(tmp_path)
    (tmp_path / "w.csv").write_text(WISE_HEAD + '"W1","05-10-2026","900.00","USD","Payment INV-1","INV-1","Acme Studio"\n', encoding="utf-8")
    assert main(["confirm", "INV-1", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 2
    assert "w.csv changed since the last run" in capsys.readouterr().err
    (tmp_path / "w.csv").write_text(WISE_HEAD + ONE[1], encoding="utf-8")
    assert main(["confirm", "INV-1", "--out", str(tmp_path / "out"), "--today", "2026-10-26"]) == 2
    assert "not today" in capsys.readouterr().err
    assert main(["confirm", "INV-1", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 0


def test_confirming_several_invoices_in_a_row_needs_no_rerun_between(tmp_path):
    setup(tmp_path, TWO, ROW_A + ROW_B)
    run(tmp_path)
    assert main(["confirm", "INV-1", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 0
    assert main(["confirm", "INV-2", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 0
    assert set(load_paid(tmp_path / "out" / "sent.json")) == {"INV-1", "INV-2"}


def test_all_strong_prompt_shows_payer_and_text_and_ends_with_a_newline(tmp_path, monkeypatch, capsys):
    setup(tmp_path, *ONE)
    run(tmp_path)
    capsys.readouterr()

    def eof(prompt=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    assert main(["confirm", "--all-strong", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 1
    out = capsys.readouterr().out
    assert "payer: Acme Studio" in out and "text:  Payment INV-1 | INV-1" in out
    assert out.endswith("\n\nnothing recorded\n")      # a blank line first: the prompt had no newline of its own


def test_snapshot_advice_is_something_that_works(tmp_path, capsys):
    setup(tmp_path, *ONE)
    run(tmp_path)
    main(["confirm", "INV-1", "--out", str(tmp_path / "out"), "--today", "2026-10-25"])
    (tmp_path / "i.csv").write_text((tmp_path / "i.csv").read_text(encoding="utf-8").replace(",1000,", ",1200,"), encoding="utf-8")
    capsys.readouterr()
    rows = run(tmp_path)
    why = rows["INV-1"]["why_not_paid"]
    assert "unmark-paid INV-1" in why and "confirm again" not in why
    assert main(["unmark-paid", "INV-1", "--out", str(tmp_path / "out")]) == 0
    rows = run(tmp_path)
    assert rows["INV-1"]["status"] == "partial_suggested"                   # 1000 of the new 1200: a fresh, honest suggestion


# ---- the rest ------------------------------------------------------------------------------

def test_demo_uses_a_fresh_ledger_unless_asked(tmp_path):
    out = str(tmp_path / "o")
    assert main(["demo", "--out", out]) == 0
    assert main(["mark-paid", "INV-1006", "--out", out, "--today", "2026-10-25"]) == 0
    main(["demo", "--out", out])
    assert {r["invoice_id"]: r["status"] for r in csv.DictReader(open(tmp_path / "o" / "status.csv", encoding="utf-8"))}["INV-1006"] == "needs_review"
    main(["demo", "--out", out, "--keep-ledger"])
    assert {r["invoice_id"]: r["status"] for r in csv.DictReader(open(tmp_path / "o" / "status.csv", encoding="utf-8"))}["INV-1006"] == "paid"


def test_review_html_has_no_absolute_paths(tmp_path):
    out = tmp_path / "some folder" / "out"
    assert main(["demo", "--out", str(out)]) == 0
    html = (out / "review.html").read_text(encoding="utf-8")
    assert str(tmp_path) not in html and not re.search(r"[A-Za-z]:\\\\|/(home|Users|tmp|var)/", html.replace("&#x27;", "'"))
    assert "invoice-chaser confirm INV-1001" in html


@pytest.mark.parametrize("bad", ["Infinity", "NaN", "-5", "-Infinity", "1e999", "abc"])
def test_hand_edited_received_amounts_give_a_clean_error(tmp_path, capsys, bad):
    out = tmp_path / "o"
    main(["demo", "--out", str(out), "--keep-ledger"])
    (out / "sent.json").write_text(json.dumps({"sent": {}, "paid": {}, "rejected": {}, "received": {
        "INV-1002": {"date": "2026-10-20", "amount": bad, "payments": [], "snapshot": {}}}}), encoding="utf-8")
    capsys.readouterr()
    assert main(["demo", "--out", str(out), "--keep-ledger"]) == 2
    err = capsys.readouterr().err
    assert "error:" in err and "not a valid sent ledger" in err and "Traceback" not in err


def test_twelve_parallel_mark_sent_keep_every_entry(tmp_path):
    ledger = tmp_path / "sent.json"
    procs = [subprocess.Popen([sys.executable, "-m", "invoice_chaser", "mark-sent", f"INV-{n}", "--stage", "nudge", "--force",
                               "--out", str(tmp_path), "--ledger", str(ledger), "--date", "2026-10-01", "--today", "2026-10-25"],
                              cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for n in range(12)]
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode()
    assert sorted(load_ledger(ledger), key=lambda k: int(k.split("-")[1])) == [f"INV-{n}" for n in range(12)]
    assert not list(tmp_path.glob("*.tmp"))


def test_parse_options_default_unchanged():
    assert ParseOptions().strict is False and date(2026, 1, 1)
