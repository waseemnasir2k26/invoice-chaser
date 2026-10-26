import csv
import json
from pathlib import Path

import pytest

from invoice_chaser.cli import main
from invoice_chaser.ledger import SCHEMA, load_paid, load_payment_rows, load_rejected, load_rejected_rows, reset_ledger
from invoice_chaser.parsers import ParseOptions, read_invoices

ROOT = Path(__file__).resolve().parent.parent
INV_HEAD = "id,client,email,amount,currency,issued,due\n"
WISE_HEAD = '"TransferWise ID","Date","Amount","Currency","Description","Payment Reference","Payer Name"\n'


def setup(tmp_path, invoices, wise="", bank=None, cfg=None):
    (tmp_path / "i.csv").write_text(INV_HEAD + invoices, encoding="utf-8")
    (tmp_path / "w.csv").write_text(WISE_HEAD + wise, encoding="utf-8")
    (tmp_path / "c.json").write_text(json.dumps({"sender_email": "me@x.co", **(cfg or {})}), encoding="utf-8")
    if bank is not None:
        (tmp_path / "b.csv").write_text("When,Memo,Credit,Ccy,From,Ref\n" + bank, encoding="utf-8")
        (tmp_path / "m.json").write_text(json.dumps({"date": "When", "credit": "Credit", "currency": "Ccy", "description": "Memo",
                                                     "payer": "From", "id": "Ref", "date_format": "%d/%m/%Y"}), encoding="utf-8")


def argv(tmp_path, today="2026-10-25", out="out"):
    a = ["run", "--invoices", str(tmp_path / "i.csv"), "--wise", str(tmp_path / "w.csv"), "--config", str(tmp_path / "c.json"),
         "--out", str(tmp_path / out), "--today", today]
    if (tmp_path / "b.csv").exists():
        a += ["--bank", str(tmp_path / "b.csv"), "--mapping", str(tmp_path / "m.json")]
    return a


def run(tmp_path, today="2026-10-25", out="out"):
    assert main(argv(tmp_path, today, out)) in (0, 2)
    return {r["invoice_id"]: r for r in csv.DictReader(open(tmp_path / out / "status.csv", encoding="utf-8"))}


def ledger(tmp_path):
    return tmp_path / "out" / "sent.json"


def edit_ledger(tmp_path, fn):
    data = json.loads(ledger(tmp_path).read_text(encoding="utf-8"))
    fn(data)
    ledger(tmp_path).write_text(json.dumps(data), encoding="utf-8")


ONE = ("INV-A,Alpha,a@x.example,500,USD,2026-09-01,2026-10-01\n", '"","05-10-2026","500.00","USD","500 USD Transfer","","Alpha"\n')


# ---- 1. the ledger has a schema; nothing is guessed --------------------------------------------------

def test_new_ledgers_carry_a_schema(tmp_path):
    setup(tmp_path, *ONE)
    run(tmp_path)
    main(["confirm", "INV-A", "--out", str(tmp_path / "out"), "--today", "2026-10-25"])
    data = json.loads(ledger(tmp_path).read_text(encoding="utf-8"))
    assert data["schema"] == SCHEMA == 1
    seen = json.loads((tmp_path / "out" / "seen.json").read_text(encoding="utf-8"))
    assert seen["schema"] == 1


@pytest.mark.parametrize("content", ['{"sent": {}, "paid": {}}', '{"INV-1": []}', '{"schema": 0, "sent": {}}', '{"schema": "1"}', '{"schema": null}'])
def test_a_ledger_without_the_right_schema_is_refused_with_a_clear_message(tmp_path, capsys, content):
    setup(tmp_path, *ONE)
    run(tmp_path)
    ledger(tmp_path).write_text(content, encoding="utf-8")
    capsys.readouterr()
    assert main(argv(tmp_path)) == 2
    err = capsys.readouterr().err
    assert "pre-release" in err and "ledger reset --backup" in err
    assert ledger(tmp_path).read_text(encoding="utf-8") == content            # never touched, never guessed at
    assert main(["mark-sent", "INV-A", "--stage", "nudge", "--force", "--out", str(tmp_path / "out")]) == 2
    assert ledger(tmp_path).read_text(encoding="utf-8") == content


def test_a_ledger_from_a_newer_version_says_upgrade(tmp_path, capsys):
    setup(tmp_path, *ONE)
    run(tmp_path)
    ledger(tmp_path).write_text('{"schema": 2, "sent": {}}', encoding="utf-8")
    assert main(argv(tmp_path)) == 2
    assert "newer invoice-chaser" in capsys.readouterr().err


def test_ledger_reset_needs_backup_keeps_a_copy_and_starts_fresh(tmp_path, capsys):
    setup(tmp_path, *ONE)
    run(tmp_path)
    old = '{"sent": {"INV-A": [{"stage": "nudge", "date": "2026-10-01"}]}}'
    ledger(tmp_path).write_text(old, encoding="utf-8")
    assert main(["ledger", "reset", "--out", str(tmp_path / "out")]) == 2
    assert "add --backup" in capsys.readouterr().err and ledger(tmp_path).read_text(encoding="utf-8") == old
    assert main(["ledger", "reset", "--backup", "--out", str(tmp_path / "out")]) == 0
    backups = list((tmp_path / "out").glob("sent.json.pre-release-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == old
    assert json.loads(ledger(tmp_path).read_text(encoding="utf-8")) == {"schema": 1, "sent": {}, "paid": {}, "received": {}, "rejected": {}, "rejected_rows": {}}
    assert run(tmp_path)["INV-A"]["status"] == "suggested_paid"                  # and everything works again



def test_an_old_seen_json_is_kept_and_reported_not_trusted(tmp_path, capsys):
    setup(tmp_path, "INV-A,Alpha,a@x.example,500,USD,2026-09-01,2026-10-01\n", '"","05-10-2026","500.00","USD","transfer","","Stranger"\n')
    run(tmp_path, today="2026-10-10")
    (tmp_path / "out" / "seen.json").write_text('{"inv-a": {"first_seen": "2026-01-01"}}', encoding="utf-8")
    capsys.readouterr()
    run(tmp_path, today="2026-10-20")
    err = capsys.readouterr().err
    assert "seen.json could not be read" in err and "pre-release" in err
    assert len(list((tmp_path / "out").glob("seen.json.corrupt-*"))) == 1


# ---- 1b. a decision follows its payment when the id changes ---------------------------------------------



def test_a_decision_with_no_matching_payment_is_left_alone(tmp_path, capsys):
    setup(tmp_path, *ONE)
    run(tmp_path)
    main(["confirm", "INV-A", "--out", str(tmp_path / "out"), "--today", "2026-10-25"])

    def other_payment(data):
        data["paid"]["INV-A"]["payment_rows"][0].update(key="wise:gone", amount="999.00")
        data["paid"]["INV-A"]["payments"] = ["wise:gone"]
    edit_ledger(tmp_path, other_payment)
    capsys.readouterr()
    run(tmp_path)
    assert "re-bound" not in capsys.readouterr().err
    assert load_paid(ledger(tmp_path))["INV-A"]["payments"] == ["wise:gone"]


# ---- 2. the same payment in two exports ----------------------------------------------------------------------

INV9 = "INV-9,Acme Ltd,a@x.example,1200,USD,2026-09-01,2026-10-01\n"
WISE9 = '"W9","03-10-2026","1200.00","USD","Acme Ltd payment INV-9","INV-9","Acme Ltd"\n'
BANK9 = "04/10/2026,Jane Okafor INV-9 payment,1200.00,USD,Jane Okafor,B9\n"


def test_the_same_payment_in_wise_and_the_bank_is_held_not_counted_twice(tmp_path, capsys):
    setup(tmp_path, INV9, WISE9, bank=BANK9)
    capsys.readouterr()
    rows = run(tmp_path)
    err = capsys.readouterr().err
    assert rows["INV-9"]["status"] == "needs_review" and "possible same payment seen in two exports" in rows["INV-9"]["why_not_paid"]
    assert "Next step" in err and "--payment" in err
    assert rows["INV-9"]["draft_file"] == ""
    assert main(["confirm", "INV-9", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 2        # a plain confirm is refused
    assert "--payment" in capsys.readouterr().err and not ledger(tmp_path).exists()
    assert main(["confirm", "--all-strong", "--yes", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 0
    assert not ledger(tmp_path).exists() or load_paid(ledger(tmp_path)) == {}


def test_naming_the_payment_confirms_it_once(tmp_path):
    setup(tmp_path, INV9, WISE9, bank=BANK9)
    run(tmp_path)
    assert main(["confirm", "INV-9", "--payment", "W9", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 0
    assert load_paid(ledger(tmp_path))["INV-9"]["payments"] == ["wise:W9"]
    assert run(tmp_path)["INV-9"]["status"] == "paid"


def test_naming_both_payments_still_counts_the_invoice_only_once(tmp_path):
    setup(tmp_path, INV9, WISE9, bank=BANK9)
    run(tmp_path)
    assert main(["confirm", "INV-9", "--payment", "W9", "--payment", "B9", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 0
    entry = load_paid(ledger(tmp_path))["INV-9"]
    assert entry["payments"] == ["wise:W9", "bank:B9"]
    assert run(tmp_path)["INV-9"]["status"] == "paid"


def test_naming_an_unknown_payment_or_mixing_flags_is_refused(tmp_path, capsys):
    setup(tmp_path, INV9, WISE9, bank=BANK9)
    run(tmp_path)
    assert main(["confirm", "INV-9", "--payment", "NOPE", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 2
    assert "not a payment suggested" in capsys.readouterr().err
    assert main(["confirm", "--all-strong", "--payment", "W9", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 2


@pytest.mark.parametrize("bank,flagged", [
    ("05/10/2026,Jane Okafor INV-9 payment,1200.00,USD,Jane Okafor,B9\n", True),        # two days later
    ("07/10/2026,Jane Okafor INV-9 payment,1200.00,USD,Jane Okafor,B9\n", False),       # four days later: two payments
    ("04/10/2026,Jane Okafor INV-8 payment,1200.00,USD,Jane Okafor,B9\n", False),       # names another invoice
    ("04/10/2026,Jane Okafor thanks,1200.00,USD,Jane Okafor,B9\n", False),              # names none, payers not blank
    ("04/10/2026,Jane Okafor INV-9 payment,1100.00,USD,Jane Okafor,B9\n", False),       # another amount
    ("04/10/2026,Jane Okafor INV-9 payment,1200.00,EUR,Jane Okafor,B9\n", False),       # another currency
])
def test_the_cross_export_rule_has_edges(tmp_path, bank, flagged):
    setup(tmp_path, INV9 + "INV-8,Other,o@x.example,1100,USD,2026-09-01,2026-10-01\n", WISE9, bank=bank)
    rows = run(tmp_path)
    why = rows["INV-9"]["why_not_paid"]
    assert ("possible same payment seen in two exports" in why) is flagged, why


def test_two_blank_payers_in_two_exports_are_flagged_but_the_same_export_is_not(tmp_path):
    setup(tmp_path, "INV-1,Acme Ltd,a@x.example,300,USD,2026-09-01,2026-10-01\n", '"W1","03-10-2026","300.00","USD","","",""\n',
          bank="04/10/2026,,300.00,USD,,B1\n")
    assert "possible same payment seen in two exports" in run(tmp_path)["INV-1"]["why_not_paid"]
    setup(tmp_path, "INV-1,Acme Ltd,a@x.example,300,USD,2026-09-01,2026-10-01\n",
          '"W1","03-10-2026","300.00","USD","","",""\n"W2","04-10-2026","300.00","USD","","",""\n')
    (tmp_path / "b.csv").unlink()
    assert "two exports" not in run(tmp_path)["INV-1"]["why_not_paid"]


# ---- 3. INV-6001 and inv-6001 --------------------------------------------------------------------------------

CASE = ("INV-6001,Client A,a@x.example,800,USD,2026-09-01,2026-10-01\ninv-6001,Client B,b@x.example,800,USD,2026-09-01,2026-10-01\n")


def test_case_variant_duplicate_invoice_ids_are_held_and_nobody_is_chased(tmp_path, capsys):
    setup(tmp_path, CASE, '"W1","05-10-2026","800.00","USD","Payment INV-6001","INV-6001","Client A"\n')
    capsys.readouterr()
    rows = run(tmp_path)
    err = capsys.readouterr().err
    assert set(rows) == {"INV-6001", "inv-6001"}
    for r in rows.values():
        assert r["status"] == "needs_review" and "duplicate invoice id (case differs)" in r["why_not_paid"] and r["draft_file"] == ""
    assert "duplicate invoice id (case differs)" in err and "Next step" in err
    assert not list((tmp_path / "out" / "drafts").glob("*.eml"))                # no reminder to either client
    assert "duplicate invoice id (case differs)" in (tmp_path / "out" / "review.html").read_text(encoding="utf-8")


def test_an_exact_duplicate_id_is_still_a_skipped_row_and_distinct_ids_are_unaffected(tmp_path):
    o = ParseOptions()
    f = tmp_path / "i.csv"
    f.write_text(INV_HEAD + "A1,x,a@b.co,1,USD,2026-01-01,2026-02-01\nA1,y,a@b.co,1,USD,2026-01-01,2026-02-01\nB1,z,a@b.co,1,USD,2026-01-01,2026-02-01\n", encoding="utf-8")
    assert [i.id for i in read_invoices(f, o)] == ["A1", "B1"] and o.case_dup_ids == set()
    f.write_text(INV_HEAD + "A1,x,a@b.co,1,USD,2026-01-01,2026-02-01\na1,y,a@b.co,1,USD,2026-01-01,2026-02-01\n", encoding="utf-8")
    o = ParseOptions()
    assert [i.id for i in read_invoices(f, o)] == ["A1", "a1"] and o.case_dup_ids == {"a1"}


# ---- 4. fewer, clearer warnings; empty invoice files ----------------------------------------------------------

def test_warnings_become_one_summary_with_counts(tmp_path, capsys):
    (tmp_path / "i.csv").write_text(INV_HEAD + "INV-1,Acme,a@b.co,1000,USD,2026-09-01,2026-10-01\nBAD,x,a@b.co,abc,USD,2026-09-01,2026-10-01\n", encoding="utf-8")
    (tmp_path / "w.csv").write_text(WISE_HEAD + '"R1","05-10-2026","-250.00","USD","Refund","",""\n"R2","05-10-2026","-9.00","USD","fee","",""\n', encoding="utf-8")
    capsys.readouterr()
    assert main(["run", "--invoices", str(tmp_path / "i.csv"), "--wise", str(tmp_path / "w.csv"), "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) in (0, 2)
    err = capsys.readouterr().err
    assert "NOTICES: 1 routine (NEGATIVE_ROWS x1)" in err and "details in review.html" in err
    assert "CRITICAL [INVOICE_ROW_DROPPED]: i.csv line 3 (invoice id 'BAD')" in err            # a dropped invoice is never "routine"
    assert "Summary: 2 critical (INVOICE_ROW_DROPPED x1, NO_SENDER x1), 2 warning (DATE_AMBIGUOUS x1, REFUND_SEEN x1)" in err and "1 routine (NEGATIVE_ROWS x1)" in err
    assert "negative row(s) ignored" not in err                                            # routine: counted, not spelled out
    html = (tmp_path / "out" / "review.html").read_text(encoding="utf-8")
    assert "negative row(s) ignored" in html and "INVOICE_ROW_DROPPED" in html


def test_no_sender_is_critical_on_every_run(tmp_path, capsys):
    (tmp_path / "i.csv").write_text(INV_HEAD + "INV-1,Acme,a@b.co,1000,USD,2026-09-01,2026-10-01\n", encoding="utf-8")
    cmd = ["run", "--invoices", str(tmp_path / "i.csv"), "--out", str(tmp_path / "out"), "--today", "2026-10-25"]
    capsys.readouterr()
    main(cmd)
    first = capsys.readouterr().err
    main(cmd)
    second = capsys.readouterr().err
    main(cmd[:-4] + ["--out", str(tmp_path / "other"), "--today", "2026-10-25"])
    third = capsys.readouterr().err
    # 1.1.15: NO_SENDER is CRITICAL and printed on every run, not once per output folder
    assert "CRITICAL [NO_SENDER]" in first and "CRITICAL [NO_SENDER]" in second and "CRITICAL [NO_SENDER]" in third
    assert "set sender_email in your config" in (tmp_path / "out" / "review.html").read_text(encoding="utf-8")     # the page always says it


def test_an_invoices_file_with_no_invoices_says_so(tmp_path, capsys):
    for text in ("", INV_HEAD, INV_HEAD + "\n\n"):
        (tmp_path / "i.csv").write_text(text, encoding="utf-8")
        capsys.readouterr()
        assert main(["run", "--invoices", str(tmp_path / "i.csv"), "--out", str(tmp_path / "out")]) == 2
        err = capsys.readouterr().err
        assert "no invoices found in i.csv" in err and "Traceback" not in err
        assert not (tmp_path / "out" / "status.csv").exists()


def test_every_row_skipped_explains_why(tmp_path, capsys):
    (tmp_path / "i.csv").write_text(INV_HEAD + "A,x,a@b.co,abc,USD,2026-01-01,2026-02-01\n", encoding="utf-8")
    assert main(["run", "--invoices", str(tmp_path / "i.csv"), "--out", str(tmp_path / "out")]) == 2
    err = capsys.readouterr().err
    assert "no invoices found" in err and "unrecognised amount" in err and "line 2" in err


# ---- README / ruff -----------------------------------------------------------------------------------------------

def test_readme_describes_the_schema_and_future_migration():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "schema version" in text and "ledger reset --backup" in text and "migrate old ledgers by fingerprint" in text
    assert "--payment" in text and "duplicate invoice id" in text


def test_the_package_has_no_unused_imports_or_undefined_names():
    import shutil
    import subprocess
    import sys
    exe = shutil.which("ruff")
    if not exe:
        pytest.skip("ruff is not installed")
    r = subprocess.run([exe, "check", "--select", "F,E9", "--no-fix", str(ROOT / "invoice_chaser")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-800:]
