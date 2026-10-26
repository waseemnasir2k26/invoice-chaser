import csv
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import ABSOLUTE_PATH, assert_no_absolute_paths
from invoice_chaser import cli
from invoice_chaser.cli import main
from invoice_chaser.dedupe import payment_row, repeat_of
from invoice_chaser.ledger import load_paid, load_payment_rows, load_received
from invoice_chaser.models import Payment
from invoice_chaser.parsers import ParseOptions, parse_wise
from invoice_chaser.shellquote import quote

ROOT = Path(__file__).resolve().parent.parent
INV_HEAD = "id,client,email,amount,currency,issued,due\n"
WISE_HEAD = '"TransferWise ID","Date","Amount","Currency","Description","Payment Reference","Payer Name"\n'


def setup(tmp_path, invoices, wise, cfg=None, wise2=None):
    (tmp_path / "i.csv").write_text(INV_HEAD + invoices, encoding="utf-8")
    (tmp_path / "w.csv").write_text(WISE_HEAD + wise, encoding="utf-8")
    (tmp_path / "c.json").write_text(json.dumps({"sender_email": "me@x.co", **(cfg or {})}), encoding="utf-8")
    if wise2 is not None:
        (tmp_path / "w2.csv").write_text(WISE_HEAD + wise2, encoding="utf-8")


def run(tmp_path, today="2026-10-25", out="out", extra=()):
    argv = ["run", "--invoices", str(tmp_path / "i.csv"), "--wise", str(tmp_path / "w.csv"), "--config", str(tmp_path / "c.json"),
            "--out", str(tmp_path / out), "--today", today]
    if (tmp_path / "w2.csv").exists():
        argv += ["--wise", str(tmp_path / "w2.csv")]
    assert main(argv + list(extra)) in (0, 2)
    return {r["invoice_id"]: r for r in csv.DictReader(open(tmp_path / out / "status.csv", encoding="utf-8"))}


def confirm(tmp_path, inv, out="out"):
    assert main(["confirm", inv, "--out", str(tmp_path / out), "--today", "2026-10-25"]) == 0


HALF = "INV-1,Acme Studio,a@acme.example,1000,USD,2026-09-01,2026-10-01\n"
ROW = '"","05-10-2026","500.00","USD","Payment INV-1 deposit","INV-1","Acme Studio"\n'


# ---- M1: a cosmetic re-export must not count a confirmed payment twice ---------------------------

def test_punctuation_case_and_spacing_do_not_change_a_payment_id(tmp_path):
    f = tmp_path / "w.csv"
    f.write_text(WISE_HEAD + ROW, encoding="utf-8")
    a = parse_wise(f)[0].id
    f.write_text(WISE_HEAD + '"","05-10-2026","500.00","USD","PAYMENT  inv-1, deposit.","INV-1","ACME STUDIO!"\n', encoding="utf-8")
    assert parse_wise(f)[0].id == a


def test_a_reworded_re_export_is_an_orphaned_decision_nothing_is_repaired_for_you(tmp_path, capsys):
    setup(tmp_path, HALF, ROW)
    assert run(tmp_path)["INV-1"]["status"] == "partial_suggested"
    confirm(tmp_path, "INV-1")
    rows = run(tmp_path)
    assert rows["INV-1"]["status"] == "partial" and rows["INV-1"]["outstanding"] == "500.00"
    # the bank re-exports with different wording: a new id for what looks like the SAME payment (same date, amount, currency, payer).
    (tmp_path / "w.csv").write_text(WISE_HEAD + '"","05-10-2026","500.00","USD","Deposit received for invoice INV-1","INV-1","Acme Studio"\n',
                                    encoding="utf-8")
    capsys.readouterr()
    rows = run(tmp_path)
    err = capsys.readouterr().err
    assert "CRITICAL [ORPHANED_DECISION]" in err and "Nothing is re-bound automatically" in err and "invoice-chaser rebind" in err
    assert rows["INV-1"]["status"] == "needs_review" and rows["INV-1"]["draft_file"] == ""      # held: no reminder, no second suggestion
    assert load_received(tmp_path / "out" / "sent.json")["INV-1"]["amount"] == "500.00"        # the ledger was not touched
    # you decide: it is the same payment
    new_key = json.loads((tmp_path / "out" / "orphans.json").read_text(encoding="utf-8"))["orphans"][0]["candidates"][0]["key"]
    assert main(["rebind", "wise:" + load_payment_rows(tmp_path / "out" / "sent.json")[0]["key"].split(":", 1)[1], new_key,
                 "--out", str(tmp_path / "out")]) == 0
    rows = run(tmp_path)
    assert rows["INV-1"]["status"] == "partial" and rows["INV-1"]["outstanding"] == "500.00"


def test_a_reworded_copy_next_to_the_original_is_held_as_a_repeat(tmp_path):
    setup(tmp_path, HALF, ROW)
    run(tmp_path)
    confirm(tmp_path, "INV-1")
    # both the original row and a reworded copy of it are in the new export
    (tmp_path / "w.csv").write_text(WISE_HEAD + ROW + '"","05-10-2026","500.00","USD","Deposit received for invoice INV-1","INV-1","Acme Studio"\n',
                                    encoding="utf-8")
    rows = run(tmp_path)
    assert rows["INV-1"]["status"] == "needs_review", "the same 500 must not be suggested (and counted) a second time"
    assert "possible repeat of a payment you already confirmed for INV-1" in rows["INV-1"]["why_not_paid"]
    assert Decimal(rows["INV-1"]["paid"]) <= Decimal("500.00") and rows["INV-1"]["draft_file"] == ""
    assert load_received(tmp_path / "out" / "sent.json")["INV-1"]["amount"] == "500.00"


def test_a_genuine_second_payment_a_week_later_is_still_suggested(tmp_path):
    setup(tmp_path, HALF, ROW)
    run(tmp_path)
    confirm(tmp_path, "INV-1")
    (tmp_path / "w.csv").write_text(WISE_HEAD + ROW + '"","12-10-2026","500.00","USD","Balance INV-1","INV-1","Acme Studio"\n', encoding="utf-8")
    assert run(tmp_path)["INV-1"]["status"] == "suggested_paid"


def test_repeat_of_matches_within_a_day_and_needs_the_same_amount_currency_and_payer():
    rows = [payment_row({"source": "wise", "id": "h-aaaaaaaaaaaa", "date": "2026-10-05", "amount": "500.00", "currency": "USD",
                         "payer": "Acme Studio!", "description": "x"})]
    pay = lambda **k: Payment("h-bbbbbbbbbbbb", k.get("d", date(2026, 10, 5)), Decimal(k.get("amt", "500")), k.get("cur", "USD"), "y",  # noqa: E731
                              k.get("payer", "ACME studio"), "wise")
    assert repeat_of(pay(), rows) and repeat_of(pay(d=date(2026, 10, 6)), rows) and repeat_of(pay(d=date(2026, 10, 4)), rows)
    assert not repeat_of(pay(d=date(2026, 10, 7)), rows)
    assert not repeat_of(pay(amt="501"), rows) and not repeat_of(pay(cur="EUR"), rows) and not repeat_of(pay(payer="Beta"), rows)


def test_confirm_refuses_a_payment_that_looks_like_a_confirmed_one(tmp_path):
    setup(tmp_path, HALF, ROW)
    run(tmp_path)
    confirm(tmp_path, "INV-1")
    entry = {"invoice_id": "INV-1", "payments": [{"key": "wise:h-other", "id": "h-other", "date": "2026-10-05", "amount": "500.00",
                                                   "currency": "USD", "payer": "Acme Studio", "description": "reworded"}]}
    with pytest.raises(ValueError, match="looks like a repeat of a payment you already confirmed for INV-1"):
        cli._repeat_guard(entry, tmp_path / "out" / "sent.json")
    assert load_payment_rows(tmp_path / "out" / "sent.json")[0]["payer"] == "acme studio"


# ---- M3: identical rows in two exports -----------------------------------------------------------

def test_identical_rows_in_two_exports_are_kept_and_flagged_not_merged(tmp_path, capsys):
    setup(tmp_path, HALF, ROW, wise2=ROW)
    rows = run(tmp_path)
    err = capsys.readouterr().err
    assert "identical in two of your exports" in err
    assert rows["INV-1"]["status"] == "needs_review"
    assert "may be the same payment as wise" in rows["INV-1"]["why_not_paid"]
    assert "identical in two of your exports" in (tmp_path / "out" / "review.html").read_text(encoding="utf-8")


def test_identical_rows_in_one_file_are_two_payments_and_not_flagged(tmp_path):
    f = tmp_path / "w.csv"
    f.write_text(WISE_HEAD + ROW + ROW, encoding="utf-8")
    opts = ParseOptions()
    ps = parse_wise(f, opts)
    assert len(ps) == 2 and ps[0].id != ps[1].id and opts.cross_file_ids() == set()


def test_the_same_file_given_twice_is_flagged_too(tmp_path):
    f = tmp_path / "w.csv"
    f.write_text(WISE_HEAD + ROW, encoding="utf-8")
    opts = ParseOptions()
    ps = parse_wise(f, opts) + parse_wise(f, opts)
    assert len(ps) == 2 and opts.cross_file_ids() == {p.id for p in ps}


# ---- the stuck clock ------------------------------------------------------------------------------

STRANGER = ("INV-1,Acme Studio,a@acme.example,1000,USD,2026-09-01,2026-10-01\n", '"W1","05-10-2026","1000.00","USD","transfer","","Someone Else"\n')


def test_a_future_today_cannot_poison_first_seen(tmp_path):
    setup(tmp_path, *STRANGER)
    run(tmp_path, today="2030-01-01")
    seen = json.loads((tmp_path / "out" / "seen.json").read_text(encoding="utf-8"))
    assert date.fromisoformat(seen["invoices"]["inv-1"]["first_seen"]) <= date.today()
    rows = run(tmp_path, today="2026-10-25")
    assert rows["INV-1"]["waiting_days"] == "0"                              # not negative, not stuck by a poisoned date


def test_the_earliest_first_seen_wins_and_survives_a_flip_between_review_and_suggestion(tmp_path):
    setup(tmp_path, *STRANGER)
    run(tmp_path, today="2026-10-10")                                        # needs_review
    (tmp_path / "w.csv").write_text(WISE_HEAD + '"W9","05-10-2026","1000.00","USD","Payment INV-1","INV-1","Acme Studio"\n', encoding="utf-8")
    rows = run(tmp_path, today="2026-10-18")                                 # now a strong suggestion, different payment id
    assert rows["INV-1"]["status"] == "suggested_paid" and rows["INV-1"]["waiting_days"] == "8" and rows["INV-1"]["stuck"] == "yes"


@pytest.mark.parametrize("backstop,stuck", [(30, "yes"), (60, "")])
def test_the_overdue_backstop_is_configurable(tmp_path, backstop, stuck):
    setup(tmp_path, "INV-1,Acme Studio,a@acme.example,1000,USD,2026-08-01,2026-09-10\n",
          '"W1","12-09-2026","1000.00","USD","Payment INV-1","INV-1","Acme Studio"\n', {"stuck_overdue_days": backstop})
    assert run(tmp_path)["INV-1"]["stuck"] == stuck                          # 45 days overdue, suggestion waiting


def test_the_stuck_note_says_first_seen_today_and_days_overdue(tmp_path):
    setup(tmp_path, "INV-1,Acme Studio,a@acme.example,1000,USD,2026-08-01,2026-09-10\n",
          '"W1","12-09-2026","1000.00","USD","Payment INV-1","INV-1","Acme Studio"\n')
    run(tmp_path)
    note = (tmp_path / "out" / "drafts" / "internal" / "INV-1-check.eml").read_text(encoding="utf-8").replace("=\n", "")
    assert "first seen today" in note and "0 day(s)" not in note and "45 days overdue" in note
    assert "Subject: Check INV-1: first seen today, 45 days overdue" in note


# ---- legacy migration must not double count -----------------------------------------------------------



# ---- no absolute paths in anything shareable -----------------------------------------------------------

def test_absolute_path_detector_catches_windows_and_unix_paths():
    for bad in ["C:\\Users\\info\\x.csv", "d:/data/x.csv", "see /home/bob/x", "/Users/a/b", "in /tmp/pytest-1/x"]:
        assert ABSOLUTE_PATH.search(bad), bad
    for ok in ["wise-statement.csv", "a/b/c", "x: 1", "Payment INV-1 2026-10-05", "https://example.com/tmp", "out/drafts/INV-1.eml"]:
        assert not ABSOLUTE_PATH.search(ok), ok
    with pytest.raises(AssertionError):
        assert_no_absolute_paths("oops C:\\Users\\info\\x")


def test_nothing_shareable_names_a_folder_on_this_computer(tmp_path):
    out = tmp_path / "some folder" / "out"
    assert main(["demo", "--out", str(out), "--keep-ledger"]) == 0
    main(["confirm", "--all-strong", "--yes", "--out", str(out), "--today", "2026-10-25"])
    main(["demo", "--out", str(out), "--keep-ledger"])
    checked = 0
    for f in out.rglob("*"):
        if f.is_file() and f.name != "local-state.json":
            assert_no_absolute_paths(f.read_text(encoding="utf-8", errors="replace"), f.name)
            checked += 1
    assert checked >= 8
    local = json.loads((out / "local-state.json").read_text(encoding="utf-8"))
    assert "local only" in local["note"] and all(Path(i["path"]).is_absolute() for i in local["inputs"])
    meta = json.loads((out / "suggestions.json").read_text(encoding="utf-8"))
    assert all(set(i) == {"name", "sha256"} for i in meta["inputs"])


def test_the_negative_row_list_shows_file_names_only(tmp_path):
    out = tmp_path / "o"
    main(["demo", "--out", str(out)])
    html = (out / "review.html").read_text(encoding="utf-8")
    assert "wise-statement.csv" in html and "Negative rows ignored" in html


# ---- copy-paste commands are safe for the user's shell -----------------------------------------------------

@pytest.mark.parametrize("raw,windows,expected", [
    ("INV-1004", True, "INV-1004"), ("INV-1004", False, "INV-1004"),
    ("INV 1", True, "'INV 1'"), ("INV 1", False, "'INV 1'"),
    ("it's", True, "'it''s'"), ("it's", False, "'it'\"'\"'s'"),
    ("a;b|c&d", True, "'a;b|c&d'"), ("$(rm -rf x)", False, "'$(rm -rf x)'"), ("$env:X", True, "'$env:X'"),
    ("a`b", True, "'a`b'"),
])
def test_quote(raw, windows, expected):
    assert quote(raw, windows) == expected


def test_ids_with_control_characters_get_a_harmless_placeholder():
    assert "control-characters" in quote("INV-1\nrm -rf /", True) and "\n" not in quote("INV-1\nrm -rf /", False)


def test_review_html_commands_quote_odd_ids(tmp_path):
    setup(tmp_path, "INV 1;x,Acme Studio,a@acme.example,1000,USD,2026-09-01,2026-10-01\n",
          '"W1","05-10-2026","1000.00","USD","transfer","","Someone Else"\n')
    run(tmp_path)
    html = (tmp_path / "out" / "review.html").read_text(encoding="utf-8")
    assert "mark-paid" in html and ("&#x27;INV 1;x&#x27;" in html or "&quot;" in html or "&#x27;" in html)
    assert not re.search(r"mark-paid INV 1;x[^&]", html)


# ---- README ----------------------------------------------------------------------------------------------

def test_readme_has_no_round_counts_and_has_the_re_export_section():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert not re.search(r"(?i)\b(one|two|three|four|five|six|\d+) review rounds?\b", text)
    assert "## The re-export trap" in text and "docs/flow.png" in text
