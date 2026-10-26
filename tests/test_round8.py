import csv
import json
from pathlib import Path

import pytest

from invoice_chaser import cli
from invoice_chaser.cli import main
from invoice_chaser.dedupe import payment_row, row_matches
from invoice_chaser.ledger import load_payment_rows, load_rejected_rows
from invoice_chaser.models import Payment
from invoice_chaser.parsers import ParseOptions, parse_wise

ROOT = Path(__file__).resolve().parent.parent
INV_HEAD = "id,client,email,amount,currency,issued,due\n"
WISE_HEAD = '"TransferWise ID","Date","Amount","Currency","Description","Payment Reference","Payer Name"\n'
TWO = "INV-A,Alpha,a@x.example,500,USD,2026-09-01,2026-10-01\nINV-B,Beta,b@x.example,500,USD,2026-09-01,2026-10-01\n"


def setup(tmp_path, invoices, wise):
    (tmp_path / "i.csv").write_text(INV_HEAD + invoices, encoding="utf-8")
    (tmp_path / "w.csv").write_text(WISE_HEAD + wise, encoding="utf-8")
    (tmp_path / "c.json").write_text(json.dumps({"sender_email": "me@x.co"}), encoding="utf-8")


def run(tmp_path, today="2026-10-25"):
    assert main(["run", "--invoices", str(tmp_path / "i.csv"), "--wise", str(tmp_path / "w.csv"), "--config", str(tmp_path / "c.json"),
                 "--out", str(tmp_path / "out"), "--today", today]) in (0, 2)
    return {r["invoice_id"]: r for r in csv.DictReader(open(tmp_path / "out" / "status.csv", encoding="utf-8"))}


def cli_ok(tmp_path, *args):
    extra = [] if args[0] == "reject" else ["--today", "2026-10-25"]       # reject has no date
    assert main([*args, "--out", str(tmp_path / "out"), *extra]) == 0


def ledger(tmp_path):
    return tmp_path / "out" / "sent.json"


def edit_ledger(tmp_path, fn):
    data = json.loads(ledger(tmp_path).read_text(encoding="utf-8"))
    fn(data)
    ledger(tmp_path).write_text(json.dumps(data), encoding="utf-8")


ALPHA = '"","05-10-2026","500.00","USD","500 USD Transfer","","Alpha"\n'
BETA = '"","05-10-2026","500.00","USD","500 USD Transfer","","Beta"\n'


# ---- the id never depends on how many other rows exist -----------------------------------------------

def test_a_lone_row_keeps_its_id_when_a_same_day_sibling_appears(tmp_path):
    f = tmp_path / "w.csv"
    f.write_text(WISE_HEAD + ALPHA, encoding="utf-8")
    alone = parse_wise(f)[0].id
    f.write_text(WISE_HEAD + ALPHA + BETA, encoding="utf-8")
    both = {p.payer: p.id for p in parse_wise(f)}
    assert both["Alpha"] == alone and both["Beta"] != alone
    f.write_text(WISE_HEAD + BETA + ALPHA, encoding="utf-8")
    assert {p.payer: p.id for p in parse_wise(f)} == both


def test_a_rejection_survives_a_sibling_appearing_in_a_later_export(tmp_path):
    setup(tmp_path, "INV-A,Alpha,a@x.example,500,USD,2026-09-01,2026-10-01\n", ALPHA)
    assert run(tmp_path)["INV-A"]["status"] == "suggested_paid"
    pid = json.loads((tmp_path / "out" / "suggestions.json").read_text(encoding="utf-8"))["suggestions"][0]["payments"][0]["id"]
    cli_ok(tmp_path, "reject", "INV-A", pid)
    assert load_rejected_rows(ledger(tmp_path))["INV-A"][f"wise:{pid}"]["payer"] == "alpha"
    assert run(tmp_path)["INV-A"]["status"] == "overdue"
    (tmp_path / "w.csv").write_text(WISE_HEAD + ALPHA + BETA, encoding="utf-8")      # a same-day sibling shows up
    rows = run(tmp_path)
    # Beta's same-amount payment may park INV-A in review (a stranger's payment), but Alpha's rejected one must not be suggested again
    assert rows["INV-A"]["status"] in ("overdue", "needs_review") and rows["INV-A"]["status"] != "suggested_paid"
    suggested = json.loads((tmp_path / "out" / "suggestions.json").read_text(encoding="utf-8"))["suggestions"]
    assert pid not in [p["id"] for e in suggested for p in e["payments"]]          # Alpha's rejected payment is not a candidate any more
    assert pid in {p.id for p in parse_wise(tmp_path / "w.csv")}


def test_a_payer_renamed_within_the_company_keeps_the_id_and_a_different_company_does_not(tmp_path):
    f = tmp_path / "w.csv"
    ids = {}
    for payer in ("ACME LTD", "Acme Limited", "acme ltd.", "Acme Inc", "Acme"):
        f.write_text(WISE_HEAD + f'"","05-10-2026","500.00","USD","Transfer","","{payer}"\n', encoding="utf-8")
        ids[payer] = parse_wise(f)[0].id
    assert ids["ACME LTD"] == ids["Acme Limited"] == ids["acme ltd."]
    assert len({ids["ACME LTD"], ids["Acme Inc"], ids["Acme"]}) == 3


def test_identical_full_keys_still_get_an_occurrence_index(tmp_path):
    f = tmp_path / "w.csv"
    f.write_text(WISE_HEAD + ALPHA + ALPHA + BETA, encoding="utf-8")
    ps = parse_wise(f)
    assert len({p.id for p in ps}) == 3
    f.write_text(WISE_HEAD + BETA + ALPHA + ALPHA, encoding="utf-8")
    assert {p.id for p in parse_wise(f)} == {p.id for p in ps}


# ---- Alpha / Beta: consume only the very payment that was confirmed ----------------------------------------

def test_a_different_payer_with_the_same_content_in_a_later_export_is_not_consumed(tmp_path):
    setup(tmp_path, TWO, ALPHA)
    run(tmp_path)
    cli_ok(tmp_path, "confirm", "INV-A")
    (tmp_path / "w.csv").write_text(WISE_HEAD + ALPHA + BETA, encoding="utf-8")        # Beta's row: same date, amount and text
    rows = run(tmp_path)
    assert rows["INV-A"]["status"] == "paid"
    assert rows["INV-B"]["status"] == "suggested_paid", "Beta's payment must not have been swallowed by Alpha's confirmation"
    assert rows["INV-B"]["draft_file"] == ""


def test_the_same_payment_in_a_later_export_is_still_consumed_quietly(tmp_path, capsys):
    setup(tmp_path, TWO, ALPHA)
    run(tmp_path)
    cli_ok(tmp_path, "confirm", "INV-A")
    capsys.readouterr()
    rows = run(tmp_path)
    assert rows["INV-A"]["status"] == "paid" and "collision" not in capsys.readouterr().err
    assert rows["INV-B"]["status"] == "overdue"


def test_a_bank_reusing_a_transaction_id_for_another_payment_is_a_loud_collision(tmp_path, capsys):
    setup(tmp_path, TWO, '"R1","05-10-2026","500.00","USD","Payment INV-A","INV-A","Alpha"\n')
    run(tmp_path)
    cli_ok(tmp_path, "confirm", "INV-A")
    (tmp_path / "w.csv").write_text(WISE_HEAD + '"R1","12-10-2026","500.00","USD","Payment INV-B","INV-B","Beta"\n', encoding="utf-8")
    capsys.readouterr()
    rows = run(tmp_path)
    err = capsys.readouterr().err
    assert rows["INV-B"]["status"] == "needs_review" and rows["INV-B"]["draft_file"] == ""
    assert "id collision with a payment you confirmed for INV-A" in rows["INV-B"]["why_not_paid"]
    assert "WARNING" in err and "id collision" in err and "Next step" in err and "mark-paid" in err
    assert "id collision" in (tmp_path / "out" / "review.html").read_text(encoding="utf-8")


def test_a_ledger_key_that_points_at_a_different_payment_is_a_collision_not_a_consume(tmp_path):
    setup(tmp_path, TWO, ALPHA + BETA)
    run(tmp_path)
    cli_ok(tmp_path, "confirm", "INV-A")
    beta_id = next(p.id for p in parse_wise(tmp_path / "w.csv") if p.payer == "Beta")

    def point_at_beta(data):                                                    # the old design: Alpha's and Beta's rows shared one id
        data["paid"]["INV-A"]["payments"] = [f"wise:{beta_id}"]
        data["paid"]["INV-A"]["payment_rows"][0]["key"] = f"wise:{beta_id}"
    edit_ledger(tmp_path, point_at_beta)
    rows = run(tmp_path)
    assert rows["INV-B"]["status"] == "needs_review" and "id collision" in rows["INV-B"]["why_not_paid"]


def test_a_legacy_ledger_without_a_fingerprint_cannot_consume_either(tmp_path, capsys):
    setup(tmp_path, TWO, ALPHA + BETA)
    run(tmp_path)
    cli_ok(tmp_path, "confirm", "INV-A")

    def forget_rows(data):
        data["paid"]["INV-A"]["payment_rows"] = []
    edit_ledger(tmp_path, forget_rows)
    capsys.readouterr()
    rows = run(tmp_path)
    assert rows["INV-A"]["status"] == "paid"
    err = capsys.readouterr().err
    assert "no record of what that payment looked like" in err and "Next step" in err


def test_a_rejection_with_a_different_fingerprint_is_not_applied_and_is_reported(tmp_path, capsys):
    setup(tmp_path, "INV-B,Beta,b@x.example,500,USD,2026-09-01,2026-10-01\n", BETA)
    run(tmp_path)
    pid = json.loads((tmp_path / "out" / "suggestions.json").read_text(encoding="utf-8"))["suggestions"][0]["payments"][0]["id"]
    cli_ok(tmp_path, "reject", "INV-B", pid)

    def other_payment(data):
        data["rejected_rows"]["INV-B"][f"wise:{pid}"]["payer"] = "alpha"
    edit_ledger(tmp_path, other_payment)
    capsys.readouterr()
    rows = run(tmp_path)
    err = capsys.readouterr().err
    assert rows["INV-B"]["status"] == "needs_review" and "id collision with a payment you rejected for inv-b" in rows["INV-B"]["why_not_paid"].lower()
    assert "different payment" in err and "Next step" in err


def test_a_rejection_made_by_an_older_version_is_reported_and_the_payment_returns(tmp_path, capsys):
    setup(tmp_path, "INV-B,Beta,b@x.example,500,USD,2026-09-01,2026-10-01\n", BETA)
    run(tmp_path)
    pid = json.loads((tmp_path / "out" / "suggestions.json").read_text(encoding="utf-8"))["suggestions"][0]["payments"][0]["id"]
    cli_ok(tmp_path, "reject", "INV-B", pid)
    edit_ledger(tmp_path, lambda d: d.update(rejected_rows={}))
    capsys.readouterr()
    assert run(tmp_path)["INV-B"]["status"] == "suggested_paid"
    err = capsys.readouterr().err
    assert "no fingerprint" in err and "reject it once more" in err


def test_confirm_refuses_a_payment_whose_id_collides_with_a_confirmed_one(tmp_path):
    setup(tmp_path, TWO, ALPHA)
    run(tmp_path)
    cli_ok(tmp_path, "confirm", "INV-A")
    rows = load_payment_rows(ledger(tmp_path))
    entry = {"invoice_id": "INV-B", "payments": [{"key": rows[0]["key"], "id": rows[0]["key"].split(":")[1], "date": "2026-10-12",
                                                   "amount": "500.00", "currency": "USD", "payer": "Beta", "description": "x"}]}
    msgs = cli._repeat_problems(entry, rows)
    assert msgs and "same id as a payment you confirmed for INV-A" in msgs[0] and "mark-paid INV-B" in msgs[0] and "Next step" in msgs[0]


def test_row_matches_demands_date_amount_currency_and_company():
    row = {"key": "wise:h-1", **{k: v for k, v in payment_row({"key": "wise:h-1", "id": "h-1", "date": "2026-10-05", "amount": "500",
                                                              "currency": "USD", "payer": "Alpha Ltd", "description": ""}).items() if k != "key"}}
    mk = lambda **k: Payment("h-1", k.get("d"), k.get("amt", __import__("decimal").Decimal("500")), k.get("cur", "USD"), "", k.get("payer", "ALPHA LIMITED"), "wise")  # noqa: E731
    from datetime import date
    assert row_matches(mk(d=date(2026, 10, 5)), row)
    assert not row_matches(mk(d=date(2026, 10, 6)), row) and row_matches(mk(d=date(2026, 10, 6)), row, days=1)
    assert not row_matches(mk(d=date(2026, 10, 5), amt=__import__("decimal").Decimal("501")), row)
    assert not row_matches(mk(d=date(2026, 10, 5), cur="EUR"), row)
    assert not row_matches(mk(d=date(2026, 10, 5), payer="Alpha Inc"), row) and not row_matches(mk(d=date(2026, 10, 5), payer="Beta"), row)


# ---- README -------------------------------------------------------------------------------------------------

def test_readme_states_the_exact_id_rule_the_deposit_hold_and_the_screenshot():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "never depends on how many other rows exist" in text and "ids never move" not in text.lower()
    assert "never a row number" in text and "same id AND the same date, amount, currency and payer" in text
    assert "deposit" in text.lower() and "mark-paid" in text and "remainder" in text.lower()
    top = text[:text.index("## What it does")]
    assert top.index("docs/review.png") < top.index("docs/flow.png") and "What it will not do" in top
    assert top.index("pip install .") < top.index("docs/review.png")


def test_every_hold_warning_says_what_to_do_next(tmp_path, capsys):
    setup(tmp_path, TWO, '"R1","05-10-2026","500.00","USD","Payment INV-A","INV-A","Alpha"\n')
    run(tmp_path)
    cli_ok(tmp_path, "confirm", "INV-A")
    (tmp_path / "w.csv").write_text(WISE_HEAD + '"R1","12-10-2026","500.00","USD","Payment INV-B","INV-B","Beta"\n'
                                    '"","05-10-2026","500.00","USD","reworded","","ALPHA"\n', encoding="utf-8")
    capsys.readouterr()
    run(tmp_path)
    err = capsys.readouterr().err
    holds = [ln for ln in err.splitlines() if "held in needs review" in ln or "id collision" in ln or "looks like a repeat" in ln]
    assert len(holds) >= 2 and all("Next step" in ln for ln in holds), holds
