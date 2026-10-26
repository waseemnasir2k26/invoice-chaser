"""1.1.10: ONE gate (invoice_chaser.gate.client_draft_allowed) decides every client-addressed draft, normal and CHECK FIRST alike.
A renamed confirmed invoice gets no client draft anywhere (the 1.1.9 blocker). A hold where the money may have arrived (possible
double count, refund-looking client name) past the stuck window prints a CRITICAL STUCK_HOLD and exits 2. mark-paid warns when a
confirmed payment with the same client and amount already backs another invoice (a warning only)."""
import ast
import csv
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from invoice_chaser import cli
from invoice_chaser.cli import main
from invoice_chaser.gate import Hold, client_draft_allowed
from invoice_chaser.models import Invoice
from test_v118 import INV_HEAD, JUDGE_PAY, ROW_A, WISE_HEAD, seed_first_seen

CLI_PATH = Path(cli.__file__)
ROW_Z = JUDGE_PAY                                                              # 300 USD from "Quiet Holdings" -> INV-Z suggestion
ROW_R = '"","16-09-2026","400.00","USD","Transfer","","Refund Ltd"\n'           # refund word in the payer: refund-name hold
ROW_D = '"","17-09-2026","250.00","USD","Payment INV-D","INV-D","Dup Co"\n'     # also in a second export: double count
INVOICES = (
    "INV-Z,Quiet Co,z@q.example,300,USD,2026-06-01,2026-07-01\n"
    "INV-R,Refund Ltd,r@r.example,400,USD,2026-06-01,2026-07-01\n"
    "INV-D,Dup Co,d@d.example,250,USD,2026-06-01,2026-07-01\n"
    "ACME-77,Renamed Co,a@a.example,350,USD,2026-06-01,2026-07-01\n"
    "INV-B,Bound Co,b@b.example,200,USD,2026-06-01,2026-07-01\n"
    "INV-C,Bound Two,c@c.example,200,USD,2026-06-01,2026-07-01\n"
    "INV-N,Normal Co,n@n.example,150,USD,2026-06-01,2026-07-01\n"
    "INV-P,Changed Co,p@p.example,120,USD,2026-06-01,2026-07-01\n"
    "FAR-7,Faraway Ltd,f@f.example,7771,EUR,2026-06-01,2026-07-01\n"  # unrelated: no client, amount or id near ACME-77
)


def inv(id="INV-1", client="Acme", amount="100", cur="USD"):
    return Invoice(id, client, "a@x.example", Decimal(amount), cur, date(2026, 6, 1), date(2026, 7, 1))


# ---- the gate, one rule at a time -----------------------------------------------------------------------------------

def test_gate_allows_a_plain_invoice():
    assert client_draft_allowed(inv(), {}, []) == (True, "")


def test_gate_blocks_any_full_confirmation_or_mark_paid_by_id_any_case():
    ledger = {"inv-1": {"kind": "paid", "date": "2026-09-20", "snapshot": {}, "gone": False}}
    for name in ("INV-1", "inv-1", " Inv-1 "):
        ok, reason = client_draft_allowed(inv(name), ledger, [])
        assert not ok and "2026-09-20" in reason


def test_gate_part_payment_is_no_block_unless_the_invoice_is_held():
    ledger = {"inv-1": {"kind": "received", "date": "2026-09-20", "snapshot": {}, "gone": False}}
    assert client_draft_allowed(inv(), ledger, []) == (True, "")                 # a part-paid invoice keeps its normal reminder
    ok, reason = client_draft_allowed(inv(), ledger, [Hold("review", "inv-1", frozenset({"inv-1"}), "x")])
    assert not ok and "held" in reason


def test_gate_orphaned_decision_blocks_every_invoice_it_references():
    holds = [Hold("orphan", "", frozenset({"acme-78", "inv-x"}), "ORPHANED_DECISION")]
    assert not client_draft_allowed(inv("ACME-78"), {}, holds)[0]                 # the renamed invoice
    assert not client_draft_allowed(inv("INV-X"), {}, holds)[0]                  # an invoice a candidate names
    assert client_draft_allowed(inv("INV-Q"), {}, holds) == (True, "")


@pytest.mark.parametrize("kind", ["double_bound", "double_count", "refund_name"])
def test_gate_blocks_payment_holds_on_their_own_invoice_only(kind):
    holds = [Hold(kind, "inv-1", frozenset({"inv-1"}), "the money may have arrived")]
    ok, reason = client_draft_allowed(inv(), {}, holds)
    assert not ok and reason == "the money may have arrived"
    assert client_draft_allowed(inv("INV-2"), {}, holds) == (True, "")


def test_gate_renamed_confirmed_invoice_is_blocked_by_client_or_amount_or_near_id():
    ledger = {"inv-old": {"kind": "paid", "date": "2026-09-20", "gone": True,
                          "snapshot": {"client": "Acme", "amount": "100.00", "currency": "USD", "due": "2026-07-01"}}}
    assert not client_draft_allowed(inv("INV-NEW"), ledger, [])[0]                # same client, amount and currency
    assert not client_draft_allowed(inv("INV-NEW", amount="101"), ledger, [])[0]  # same client, edited amount: still blocked
    assert not client_draft_allowed(inv("INV-NEW", client="Acme Ltd.", cur="EUR"), ledger, [])[0]  # same client (punctuation, suffix)
    assert not client_draft_allowed(inv("INV-O1", client="Other", amount="999", cur="EUR"), ledger, [])[0]  # id within two edits of inv-old
    assert client_draft_allowed(inv("FAR-9000", client="Other", amount="999", cur="EUR"), ledger, [])[0]    # nothing in common: a draft


# ---- the repro from the review: ACME-77 confirmed, renamed ACME-78, run far ahead: no client draft anywhere -----------------

def _write(base: Path, invoices: str, wise_rows: list[str], second_export: str | None = None):
    base.mkdir(parents=True, exist_ok=True)
    (base / "i.csv").write_text(INV_HEAD + invoices, encoding="utf-8")
    (base / "w.csv").write_text(WISE_HEAD + "".join(wise_rows), encoding="utf-8")
    if second_export is not None:
        (base / "w2.csv").write_text(WISE_HEAD + second_export, encoding="utf-8")
    (base / "c.json").write_text(json.dumps({"sender_email": "me@x.co"}), encoding="utf-8")


def _run(base: Path, today: str, second: bool = False) -> int:
    args = ["run", "--invoices", str(base / "i.csv"), "--wise", str(base / "w.csv"), "--config", str(base / "c.json"),
            "--out", str(base / "out"), "--today", today]
    if second:
        args += ["--wise", str(base / "w2.csv")]
    return main(args)


def _cmd(base: Path, *args, day: str):
    return main([*args, "--out", str(base / "out"), "--today", day])


def _client_eml(base: Path) -> list[str]:
    """Every .eml that a CLIENT could receive: anything in drafts/ or drafts/check-first/ that is not an internal -check note."""
    found = []
    for d in (base / "out" / "drafts", base / "out" / "drafts" / "check-first"):
        if d.is_dir():
            found += [f"{d.name}/{p.name}" for p in d.glob("*.eml") if not p.name.endswith("-check.eml")]
    return sorted(found)


def test_judge_repro_confirmed_then_renamed_gets_no_client_draft_anywhere(tmp_path):
    base = tmp_path / "repro"
    _write(base, "ACME-77,Renamed Co,a@a.example,350,USD,2026-06-01,2026-07-01\n", [ROW_A.replace("500.00", "350.00").replace("INV-101", "ACME-77")])
    assert _run(base, "2026-09-20") in (0, 2)
    assert _cmd(base, "mark-paid", "ACME-77", day="2026-09-20") == 0
    _write(base, "ACME-78,Renamed Co,a@a.example,350,USD,2026-06-01,2026-07-01\n", [ROW_A.replace("500.00", "350.00").replace("INV-101", "ACME-77")])
    seed_first_seen(base, "2026-09-20", "ACME-78")                        # waiting since the confirmation
    code = _run(base, "2027-06-30")
    assert code == 2                                                      # ORPHANED_DECISION is critical
    rows = {r["invoice_id"]: r for r in csv.DictReader(open(base / "out" / "status.csv", encoding="utf-8"))}
    assert rows["ACME-78"]["status"] == "needs_review"
    assert _client_eml(base) == []                                       # no reminder to the client, normal or CHECK FIRST
    assert list((base / "out" / "drafts").glob("*.eml")) == []                       # no note to you in the root of drafts/
    assert all(p.name.endswith("-check.eml") for p in (base / "out" / "drafts" / "internal").glob("*.eml"))
    assert not (base / "out" / "drafts" / "check-first").exists() or not list((base / "out" / "drafts" / "check-first").glob("*.eml"))


# ---- every hold type in one scenario; the gate is the only thing that can stop a client draft --------------------------

def _scenario(base: Path) -> None:
    """Hold types: ORPHANED_DECISION (renamed), PAYMENT_DOUBLE_BOUND, possible double count, refund-looking client name,
    CHANGED_INVOICE (confirmed then edited), and a suggestion stuck long enough for CHECK FIRST. Plus INV-N, an ordinary overdue invoice."""
    _write(base, INVOICES, [ROW_Z, ROW_R, ROW_D], second_export=ROW_D)
    assert _run(base, "2026-09-20", second=True) in (0, 2)
    for inv_id in ("ACME-77", "INV-B", "INV-C", "INV-P"):
        assert _cmd(base, "mark-paid", inv_id, day="2026-09-20") == 0
    ledger = base / "out" / "sent.json"
    data = json.loads(ledger.read_text(encoding="utf-8"))
    for inv_id in ("INV-B", "INV-C"):                                    # one payment key behind two invoices
        data["paid"][inv_id]["payments"] = ["wise:bound-0001"]
    ledger.write_text(json.dumps(data), encoding="utf-8")
    _write(base, INVOICES.replace("ACME-77,", "ACME-78,").replace("INV-P,Changed Co,p@p.example,120", "INV-P,Changed Co,p@p.example,130"),
           [ROW_Z, ROW_R, ROW_D], second_export=ROW_D)
    seed_first_seen(base, "2026-09-01", "INV-Z", "INV-R")


def test_scenario_control_has_client_drafts_and_every_hold_type(tmp_path, capsys):
    base = tmp_path / "control"
    _scenario(base)
    code = _run(base, "2027-06-30", second=True)
    err = capsys.readouterr().err
    assert code == 2
    assert _client_eml(base) != []                                       # the scenario really writes client drafts when nothing blocks
    assert any(n.startswith("drafts/FAR-7-") for n in _client_eml(base))
    assert any(n.startswith("check-first/INV-Z-") for n in _client_eml(base))   # CHECK FIRST path exercised
    for needed in ("ORPHANED_DECISION", "PAYMENT_DOUBLE_BOUND", "CHANGED_INVOICE", "STUCK_HOLD"):
        assert needed in err, needed
    rows = {r["invoice_id"]: r for r in csv.DictReader(open(base / "out" / "status.csv", encoding="utf-8"))}
    assert rows["ACME-78"]["status"] == "needs_review" and rows["INV-B"]["status"] == "needs_review"
    assert rows["INV-R"]["status"] == "needs_review" and rows["INV-D"]["status"] == "needs_review"
    assert rows["INV-P"]["status"] == "needs_review"


def test_stuck_holds_for_money_that_may_have_arrived_get_no_client_draft_and_a_critical_line(tmp_path, capsys):
    base = tmp_path / "control"
    _scenario(base)
    capsys.readouterr()                                                  # run one also prints STUCK_HOLD (its days overdue are past 30): look at the final run only
    _run(base, "2027-06-30", second=True)
    err = capsys.readouterr().err
    client = _client_eml(base)
    for held in ("INV-R", "INV-D", "ACME-78", "INV-B", "INV-C", "INV-P"):
        assert not any(n.split("/")[-1].startswith(held + "-") for n in client), held
    lines = [ln for ln in err.splitlines() if ln.lstrip().startswith("CRITICAL [STUCK_HOLD]")]
    assert len(lines) == 2                                               # refund-name INV-R and double-count INV-D, one line each
    assert any("INV-R" in ln and "days overdue" in ln for ln in lines)
    assert any("INV-D" in ln and "days overdue" in ln for ln in lines)


def test_monkeypatched_gate_to_false_writes_zero_client_drafts_in_every_folder(tmp_path, monkeypatch):
    control = tmp_path / "control"
    _scenario(control)
    _run(control, "2027-06-30", second=True)
    assert _client_eml(control)                                          # non-vacuous: the same scenario does produce client drafts

    patched = tmp_path / "patched"
    _scenario(patched)
    monkeypatch.setattr(cli, "client_draft_allowed", lambda *a, **k: (False, "forced by the test"))
    _run(patched, "2027-06-30", second=True)
    assert _client_eml(patched) == []                                    # every path that writes a client draft consults the gate
    for d in (patched / "out" / "drafts", patched / "out" / "drafts" / "check-first"):
        if d.is_dir():
            assert [p.name for p in d.glob("*.eml") if not p.name.endswith("-check.eml")] == []


def test_only_one_function_builds_client_drafts_and_it_asks_the_gate():
    """Structure, not behaviour: a function that builds a client-addressed draft (a dict with 'subject' and no 'internal' key)
    must call client_draft_allowed. Any future path that skips the gate fails here."""
    tree = ast.parse(CLI_PATH.read_text(encoding="utf-8"))
    builders, gated = set(), set()
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        keys = [set(k.value for k in node.keys if isinstance(k, ast.Constant)) for node in ast.walk(fn) if isinstance(node, ast.Dict)]
        if any("subject" in k and "internal" not in k for k in keys):
            builders.add(fn.name)
        if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "client_draft_allowed" for n in ast.walk(fn)):
            gated.add(fn.name)
    assert builders == {"_client_item"}, builders
    assert builders <= gated


# ---- M1 wording and M2: mark-paid warns, never blocks ---------------------------------------------------------------

def test_mark_paid_warns_when_a_confirmed_payment_of_the_same_client_and_amount_backs_another_invoice(tmp_path, capsys):
    base = tmp_path / "twice"
    _write(base, "INV-901,Twice Co,t@t.example,500,USD,2026-06-01,2026-07-01\n"
                 "INV-902,Twice Co,t@t.example,500,USD,2026-06-01,2026-07-01\n",
           ['"","15-09-2026","500.00","USD","Payment INV-901","INV-901","Twice Co"\n'])
    assert _run(base, "2026-09-20") in (0, 2)
    assert _cmd(base, "confirm", "INV-901", day="2026-09-20") == 0
    capsys.readouterr()
    assert _cmd(base, "mark-paid", "INV-902", day="2026-09-20") == 0          # the override is allowed: no block
    out = capsys.readouterr().out
    assert "WARNING: payment" in out and "already backs INV-901" in out and "same client and amount as INV-902" in out


def test_mark_paid_stays_quiet_for_another_client_or_amount(tmp_path, capsys):
    base = tmp_path / "other"
    _write(base, "INV-901,Twice Co,t@t.example,500,USD,2026-06-01,2026-07-01\n"
                 "INV-903,Other Co,o@o.example,500,USD,2026-06-01,2026-07-01\n",
           ['"","15-09-2026","500.00","USD","Payment INV-901","INV-901","Twice Co"\n'])
    assert _run(base, "2026-09-20") in (0, 2)
    assert _cmd(base, "confirm", "INV-901", day="2026-09-20") == 0
    capsys.readouterr()
    assert _cmd(base, "mark-paid", "INV-903", day="2026-09-20") == 0
    assert "WARNING: payment" not in capsys.readouterr().out
