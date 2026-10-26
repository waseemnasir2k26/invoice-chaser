"""1.1.8: a STUCK held invoice also gets the normal client reminder, as a CHECK FIRST draft in drafts/check-first/ (never
for a double-bound or double-counted payment); mark-sent needs --checked for it; a repeat rebind says nothing changed; and a
bank-style "1.200,00" amount on a 1200 EUR invoice is read as 1200.00 (regression)."""
import csv
import json
from datetime import date, timedelta
from email import message_from_bytes
from pathlib import Path

from invoice_chaser.cli import SEEN_SCHEMA, main
from invoice_chaser.ledger import load_paid, load_payment_rows

WISE_HEAD = '"TransferWise ID","Date","Amount","Currency","Description","Payment Reference","Payer Name"\n'
INV_HEAD = "id,client,email,amount,currency,issued,due\n"
JUDGE_INV = "INV-Z,Quiet Co,z@q.example,300,USD,2026-06-01,2026-07-01\n"
JUDGE_PAY = '"","15-09-2026","300.00","USD","Transfer","","Quiet Holdings"\n'
TWO = "INV-101,Holding Co,a@x.example,500,USD,2026-09-01,2026-10-01\nINV-102,Beta Works,b@x.example,500,USD,2026-09-01,2026-10-01\n"
ROW_A = '"","05-10-2026","500.00","USD","Payment INV-101","INV-101","Holding Co"\n'


def setup(tmp_path, invoices=JUDGE_INV, wise=JUDGE_PAY):
    (tmp_path / "i.csv").write_text(INV_HEAD + invoices, encoding="utf-8")
    (tmp_path / "w.csv").write_text(WISE_HEAD + wise, encoding="utf-8")
    (tmp_path / "c.json").write_text(json.dumps({"sender_email": "me@x.co"}), encoding="utf-8")


def seed_first_seen(tmp_path, first_seen: str, *ids: str):
    """Pretend these invoices have been waiting since first_seen (the clock uses the real date otherwise)."""
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "seen.json").write_text(json.dumps({"schema": SEEN_SCHEMA, "invoices": {i.lower(): {"first_seen": first_seen} for i in ids}}),
                                   encoding="utf-8")


def run(tmp_path, today="2026-12-30", wise=None):
    wises = wise or [tmp_path / "w.csv"]
    args = ["run", "--invoices", str(tmp_path / "i.csv"), "--config", str(tmp_path / "c.json"), "--out", str(tmp_path / "out"),
            "--today", today]
    for w in wises:
        args += ["--wise", str(w)]
    code = main(args)
    rows = {r["invoice_id"]: r for r in csv.DictReader(open(tmp_path / "out" / "status.csv", encoding="utf-8"))}
    return code, rows


def cmd(tmp_path, *args, today=True, day="2026-12-30"):
    extra = ["--today", day] if today else []
    return main([*args, "--out", str(tmp_path / "out"), *extra])


def check_first_files(tmp_path) -> list[str]:
    d = tmp_path / "out" / "drafts" / "check-first"
    return sorted(p.name for p in d.glob("*.eml")) if d.is_dir() else []


def body_of(path: Path):
    msg = message_from_bytes(path.read_bytes())
    return msg["Subject"], msg.get_payload(decode=True).decode("utf-8")


# ---- 1. the judge case: a stuck held invoice gets a CHECK FIRST client reminder -------------------------------------

def test_judge_case_stuck_held_invoice_gets_a_check_first_reminder_and_no_plain_one(tmp_path):
    setup(tmp_path)
    seed_first_seen(tmp_path, "2026-09-01", "INV-Z")                  # waiting 120 days, 182 days overdue
    code, rows = run(tmp_path)
    assert code == 2 and rows["INV-Z"]["status"] == "needs_review"  # 1.1.13: 182 days overdue and held: CRITICAL OVERDUE_HELD, exit 2
    files = check_first_files(tmp_path)
    assert len(files) == 1 and files[0].startswith("INV-Z-") and files[0].endswith(".eml")
    drafts = sorted(p.name for p in (tmp_path / "out" / "drafts" / "internal").glob("INV-Z*.eml"))
    assert drafts == ["INV-Z-check.eml"]                             # no client draft in drafts/; the internal note stays
    subject, body = body_of(tmp_path / "out" / "drafts" / "check-first" / files[0])
    assert subject.startswith("[CHECK FIRST] ")
    first = body.splitlines()[0]
    assert first == ("CHECK FIRST: a possible payment was found (h-a20a2d0c80f9, 2026-09-15, 300.00 USD, payer Quiet Holdings). "
                     "Verify it before sending this.")
    entries = json.loads((tmp_path / "out" / "drafts.json").read_text(encoding="utf-8"))
    cf = [e for e in entries if e.get("check_first")]
    assert len(cf) == 1 and cf[0]["invoice_id"] == "INV-Z" and cf[0]["file"] == f"check-first/{files[0]}"
    assert any(e.get("internal") for e in entries)
    assert "Check-first reminders" in (tmp_path / "out" / "review.html").read_text(encoding="utf-8")


def test_a_recent_hold_or_a_young_overdue_invoice_gets_no_check_first_reminder(tmp_path):
    setup(tmp_path)
    today = date.today()
    seed_first_seen(tmp_path, (today - timedelta(days=5)).isoformat(), "INV-Z")   # waiting 5 days: not longer than stuck_after_days (7)
    run(tmp_path, today=today.isoformat())
    assert check_first_files(tmp_path) == []
    setup(tmp_path)
    seed_first_seen(tmp_path, "2026-06-01", "INV-Z")                  # waiting long, but only 19 days overdue at 2026-07-20
    run(tmp_path, today="2026-07-20")
    assert check_first_files(tmp_path) == []


def test_a_possible_double_count_gets_no_client_draft_at_all(tmp_path):
    setup(tmp_path)
    (tmp_path / "w2.csv").write_text(WISE_HEAD + JUDGE_PAY, encoding="utf-8")          # the same payment in a second export
    seed_first_seen(tmp_path, "2026-09-01", "INV-Z")
    _, rows = run(tmp_path, wise=[tmp_path / "w.csv", tmp_path / "w2.csv"])
    assert rows["INV-Z"]["status"] == "needs_review"
    assert check_first_files(tmp_path) == []
    assert "double count" in rows["INV-Z"]["draft_status"]


def test_payment_double_bound_gets_no_client_draft(tmp_path, capsys):
    (tmp_path / "i.csv").write_text(INV_HEAD + TWO, encoding="utf-8")
    (tmp_path / "w.csv").write_text(WISE_HEAD + ROW_A, encoding="utf-8")
    (tmp_path / "c.json").write_text(json.dumps({"sender_email": "me@x.co"}), encoding="utf-8")
    run(tmp_path, today="2026-10-25")
    assert cmd(tmp_path, "confirm", "INV-101", day="2026-10-25") == 0
    key = load_payment_rows(tmp_path / "out" / "sent.json")[0]["key"]

    def bind_102(data):                                              # one payment now backs INV-101 AND INV-102
        data["paid"]["INV-102"] = {"date": "2026-10-25", "note": "", "source": "confirm", "payments": [key],
                                   "snapshot": {"client": "Beta Works", "amount": "500", "currency": "USD", "due": "2026-10-01"},
                                   "payment_rows": []}
    raw = json.loads((tmp_path / "out" / "sent.json").read_text(encoding="utf-8"))
    bind_102(raw)
    (tmp_path / "out" / "sent.json").write_text(json.dumps(raw), encoding="utf-8")
    seed_first_seen(tmp_path, "2026-09-01", "INV-101", "INV-102")
    _, rows = run(tmp_path, today="2026-12-30")
    assert rows["INV-101"]["status"] == "needs_review" and rows["INV-102"]["status"] == "needs_review"
    assert check_first_files(tmp_path) == []                          # the rule itself is unit-tested below (days overdue is 0 here)


def test_no_client_draft_rule_for_double_bound_and_double_counted_payments():
    from datetime import date as d
    from decimal import Decimal
    from invoice_chaser.cli import _build_holds
    from invoice_chaser.gate import client_draft_allowed
    from invoice_chaser.models import Invoice, InvoiceResult, Match

    def _no_client_draft(r, double_held):
        # 1.1.10: the gate decides; '' = a CHECK FIRST draft may be written
        holds, gate = _build_holds([r], [], double_held, [r.invoice], {}, {}, {r.invoice.id.lower(): [*r.matches, *r.suggestions]})
        ok, reason = client_draft_allowed(r.invoice, gate, holds)
        return "" if ok else reason

    def held(suspect=""):
        i = Invoice("INV-9", "Client", "c@x.example", Decimal("300"), "USD", d(2026, 6, 1), d(2026, 7, 1))
        m = Match("INV-9", "wise:P1", d(2026, 9, 15), "Transfer", 0.5, "x", payer="Quiet Holdings", source="wise", currency="USD",
                  amount=Decimal("300"), suspect=suspect)
        return InvoiceResult(invoice=i, status="needs_review", paid=Decimal("0"), outstanding=Decimal("300"), days_overdue=182,
                             stage=None, confidence=0.5, suggestions=[m])
    assert "PAYMENT_DOUBLE_BOUND" in _no_client_draft(held(), {"inv-9": "payment x also backs another invoice"})
    assert "double count" in _no_client_draft(held("identical payment found in two exports: one real payment or two?"), {})
    assert "double count" in _no_client_draft(held("possible repeat of a payment you already confirmed for INV-1: not counted again"), {})
    assert _no_client_draft(held("id collision with a payment you rejected for INV-2"), {}) == ""   # an ordinary hold may get a CHECK FIRST draft
    assert _no_client_draft(held(), {}) == ""


def test_mark_sent_on_a_check_first_draft_needs_checked(tmp_path):
    setup(tmp_path)
    seed_first_seen(tmp_path, "2026-09-01", "INV-Z")
    run(tmp_path)
    assert cmd(tmp_path, "mark-sent", "INV-Z", today=False) == 2
    assert not (tmp_path / "out" / "sent.json").exists() or "INV-Z" not in (tmp_path / "out" / "sent.json").read_text(encoding="utf-8")
    assert cmd(tmp_path, "mark-sent", "INV-Z", "--checked", today=False) == 0
    assert "escalation" in (tmp_path / "out" / "sent.json").read_text(encoding="utf-8")


def test_an_edited_check_first_draft_is_kept_not_overwritten(tmp_path):
    setup(tmp_path)
    seed_first_seen(tmp_path, "2026-09-01", "INV-Z")
    run(tmp_path)
    f = tmp_path / "out" / "drafts" / "check-first" / "INV-Z-escalation.eml"
    f.write_text(f.read_text(encoding="utf-8") + "\nmy edit\n", encoding="utf-8")
    run(tmp_path)
    assert "my edit" in f.read_text(encoding="utf-8")                  # kept
    assert (tmp_path / "out" / "drafts" / "check-first" / "INV-Z-escalation.new.eml").is_file()


# ---- 2. a repeat rebind of an already-resolved pair says nothing changed --------------------------------------------

def test_a_repeat_invoice_rebind_says_nothing_changed_and_exits_1(tmp_path, capsys):
    same = "INV-101,Holding Co,a@x.example,500,USD,2026-09-01,2026-10-01\n"
    setup_pay = '"","05-10-2026","500.00","USD","Payment INV-101","INV-101","Holding Co"\n'
    (tmp_path / "i.csv").write_text(INV_HEAD + same, encoding="utf-8")
    (tmp_path / "w.csv").write_text(WISE_HEAD + setup_pay, encoding="utf-8")
    (tmp_path / "c.json").write_text(json.dumps({"sender_email": "me@x.co"}), encoding="utf-8")
    run(tmp_path, today="2026-10-25")
    assert cmd(tmp_path, "confirm", "INV-101", day="2026-10-25") == 0
    (tmp_path / "i.csv").write_text(INV_HEAD + "X1,Holding Co,a@x.example,500,USD,2026-09-01,2026-10-01\n", encoding="utf-8")
    run(tmp_path, today="2026-10-25")
    assert cmd(tmp_path, "rebind", "INV-101", "X1", today=False) == 0
    capsys.readouterr()
    assert cmd(tmp_path, "rebind", "INV-101", "X1", today=False) == 1
    err = capsys.readouterr()
    assert "nothing changed" in (err.out + err.err) and "not an orphaned decision" not in (err.out + err.err)
    assert load_paid(tmp_path / "out" / "sent.json").keys() == {"X1"}


def test_a_repeat_payment_rebind_says_nothing_changed_and_exits_1(tmp_path, capsys):
    setup(tmp_path, invoices=TWO.split("\n")[0] + "\n", wise=ROW_A)
    run(tmp_path, today="2026-10-25")
    assert cmd(tmp_path, "confirm", "INV-101", day="2026-10-25") == 0
    real = load_payment_rows(tmp_path / "out" / "sent.json")[0]["key"]
    old = "wise:old-format-0001"

    def go(data):
        entry = data["paid"]["INV-101"]
        entry["payments"] = [old]
        entry["payment_rows"][0]["key"] = old
    raw = json.loads((tmp_path / "out" / "sent.json").read_text(encoding="utf-8"))
    go(raw)
    (tmp_path / "out" / "sent.json").write_text(json.dumps(raw), encoding="utf-8")
    run(tmp_path, today="2026-10-25")
    assert cmd(tmp_path, "rebind", old, real, today=False) == 0
    capsys.readouterr()
    assert cmd(tmp_path, "rebind", old, real, today=False) == 1
    text = capsys.readouterr()
    assert "nothing changed" in (text.out + text.err) and "not an orphaned decision" not in (text.out + text.err)


# ---- 3. regression: "1.200,00" on a 1200 EUR invoice is read as 1200.00 ----------------------------------------------

def test_bank_style_decimal_comma_amount_matches_a_1200_eur_invoice(tmp_path):
    setup(tmp_path, invoices="INV-E,Euro Client,e@x.example,1200,EUR,2026-09-01,2026-10-01\n",
          wise='"","05-10-2026","1.200,00","EUR","Payment INV-E","INV-E","Euro Client"\n')
    _, rows = run(tmp_path, today="2026-10-25")
    assert rows["INV-E"]["status"] == "suggested_paid"
    sug = json.loads((tmp_path / "out" / "suggestions.json").read_text(encoding="utf-8"))["suggestions"]
    assert sug and str(sug[0]["amount"]) in ("1200.00", "1200") and sug[0]["currency"] == "EUR"


def test_decimal_comma_regression_does_not_break_the_comma_thousands_form(tmp_path):
    setup(tmp_path, invoices="INV-E,Euro Client,e@x.example,1200,EUR,2026-09-01,2026-10-01\n",
          wise='"","05-10-2026","1,200.00","EUR","Payment INV-E","INV-E","Euro Client"\n')
    _, rows = run(tmp_path, today="2026-10-25")
    assert rows["INV-E"]["status"] == "suggested_paid"
