"""1.1.11: (1) a confirmed invoice renamed AND edited is an ORPHANED_DECISION candidate, held with no client draft, for every judge variant
(client punctuation/spacing, amount edit, amount alone, id alone); (2) the same payment in two files of ONE source is a possible double
count, held; rows inside one file are still counted separately; (3) notes to you live in drafts/internal/; (4) the partial remainder ruling."""
import csv
import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from test_v118 import INV_HEAD, ROW_A, WISE_HEAD  # noqa: F401  (same CSV heads as the other suites)
from test_v1110 import _client_eml, _cmd, _run, _write

from invoice_chaser.dedupe import dedupe_payments
from invoice_chaser.drafts import write_drafts
from invoice_chaser.gate import company_words, edit_distance, renamed_match
from invoice_chaser.models import Invoice, Payment

ROOT = Path(__file__).resolve().parents[1]


def inv(id="INV-1", client="Acme", amount="100", cur="USD"):
    return Invoice(id, client, "a@x.example", Decimal(amount), cur, date(2026, 6, 1), date(2026, 7, 1))


def _status(base: Path) -> dict:
    with open(base / "out" / "status.csv", encoding="utf-8") as fh:
        return {r["invoice_id"]: r for r in csv.DictReader(fh)}


def pay(id, d=date(2026, 10, 5), amount="1000.00", payer="Acme Studio", source="wise", file_no=1, cur="USD"):
    return Payment(id, d, Decimal(amount), cur, "Payment INV-1", payer, source, file_no=file_no)


# ---- (1) the renamed-and-edited confirmed invoice --------------------------------------------------------------------

def test_company_words_ignore_punctuation_spacing_case_and_legal_suffix():
    assert company_words("Renamed Co.") == company_words("Renamed  Co") == company_words("renamed co") == company_words("The Renamed Co")
    assert company_words("Acme Ltd") == company_words("ACME LIMITED")
    assert company_words("Acme Ltd") != company_words("Bravo Ltd")
    assert company_words("") is None and company_words(None) is None


def test_edit_distance_is_bounded():
    assert edit_distance("inv-a", "inv-a2") == 1
    assert edit_distance("inv-a", "inv-z") == 1
    assert edit_distance("inv-a", "inv-ab-cd") == 3          # over the limit: reported as limit + 1
    assert edit_distance("abc", "abc") == 0


@pytest.mark.parametrize("snap_id, new_id, client, amount, cur, reasons", [
    ("INV-A", "INV-A2", "Renamed Co.", "400", "USD", {"same client"}),                 # punctuation, and the amount edited too
    ("INV-A", "INV-A2", "Renamed  Co", "350", "USD", {"same client", "same amount and currency"}),   # spacing only
    ("INV-A", "ZZ-9001", "Other Ltd", "350", "USD", {"same amount and currency"}),       # amount alone, far id
    ("INV-A", "INV-A1", "Totally Other", "900", "EUR", {"id within two edits"}),         # id alone
])
def test_renamed_match_has_a_reason_for_each_judge_variant(snap_id, new_id, client, amount, cur, reasons):
    snapshot = {"client": "Renamed Co", "amount": "350", "currency": "USD"}
    assert set(renamed_match(snapshot, snap_id, inv(new_id, client, amount, cur))) >= reasons


def test_renamed_match_is_empty_for_an_unrelated_invoice():
    snapshot = {"client": "Renamed Co", "amount": "350", "currency": "USD"}
    assert renamed_match(snapshot, "INV-A", inv("FAR-7", "Faraway Ltd", "7771", "EUR")) == []


INVOICE_HEAD = "INV-A,Renamed Co,a@a.example,350,USD,2026-06-01,2026-07-01\n"
FAR = "FAR-7,Faraway Ltd,f@f.example,7771,EUR,2026-06-01,2026-07-01\n"  # unrelated, overdue: must still get its reminder


@pytest.mark.parametrize("client, amount, new_id", [
    ("Renamed Co.", "400", "INV-A2"),        # judge variant: punctuation, and the amount edited (350 -> 400)
    ("Renamed  Co", "350", "INV-A2"),        # judge variant: spacing only
    ("Other Ltd", "350", "ZZ-9001"),         # the amount alone, the client and the id changed
    ("Totally Other", "900", "INV-A1"),      # the id alone (one edit), client and amount changed
])
def test_renamed_and_edited_confirmed_invoice_is_held_for_every_judge_variant(tmp_path, capsys, client, amount, new_id):
    base = tmp_path / "case"
    row = ROW_A.replace("500.00", "350.00").replace("INV-101", "INV-A")
    _write(base, INVOICE_HEAD + FAR, [row])
    assert _run(base, "2026-09-20") in (0, 2)
    assert _cmd(base, "mark-paid", "INV-A", day="2026-09-20") == 0
    _write(base, f"{new_id},{client},a@a.example,{amount},USD,2026-06-01,2026-07-01\n" + FAR, [row])
    capsys.readouterr()
    assert _run(base, "2027-06-30") == 2                                  # ORPHANED_DECISION is critical
    err = capsys.readouterr().err
    assert "CRITICAL [ORPHANED_DECISION]" in err and "may be it renamed or edited" in err
    assert f"invoice-chaser rebind INV-A {new_id}" in err and "invoice-chaser forget INV-A" in err
    rows = _status(base)
    assert rows[new_id]["status"] == "needs_review"
    client_files = _client_eml(base)
    assert not any(n.split("/")[-1].startswith(f"{new_id}-") for n in client_files)   # no normal and no CHECK FIRST reminder
    assert any(n.startswith("drafts/FAR-7-") for n in client_files)        # non-vacuous: the unrelated invoice is still chased


def test_rebind_then_the_held_invoice_is_not_orphaned_any_more(tmp_path, capsys):
    base = tmp_path / "rebound"
    row = ROW_A.replace("500.00", "350.00").replace("INV-101", "INV-A")
    _write(base, INVOICE_HEAD, [row])
    _run(base, "2026-09-20")
    assert _cmd(base, "mark-paid", "INV-A", day="2026-09-20") == 0
    _write(base, "INV-A2,Renamed Co.,a@a.example,350,USD,2026-06-01,2026-07-01\n", [row])
    assert _run(base, "2027-06-30") == 2
    assert _cmd(base, "rebind", "INV-A", "INV-A2", day="2027-06-30") == 0
    capsys.readouterr()
    assert _run(base, "2027-06-30") in (0, 2)
    assert "ORPHANED_DECISION" not in capsys.readouterr().err


# ---- (2) the same payment in two files of one source ------------------------------------------------------------------

def test_same_transfer_id_in_two_files_of_one_source_is_kept_and_held_not_dropped():
    w = []
    a, b = pay("T1", file_no=1), pay("T1", file_no=2)
    out = dedupe_payments([a, b], lambda m, c="": w.append(m))
    assert len(out) == 2 and a.suspect and b.suspect
    assert any("possible duplicate across files" in m for m in w)


def test_same_transfer_id_twice_in_one_file_is_still_dropped_with_its_warning():
    w = []
    out = dedupe_payments([pay("T1", file_no=1), pay("T1", file_no=1)], lambda m, c="": w.append(m))
    assert len(out) == 1 and any("duplicate payment row dropped" in m for m in w)


def test_identical_hash_rows_in_two_files_of_one_source_with_the_same_payer_company_are_flagged():
    w = []
    a = pay("h-aaaaaaaaaaaa", payer="ACME LTD", file_no=1)
    b = pay("h-bbbbbbbbbbbb", d=date(2026, 10, 6), payer="Acme Limited", file_no=2)   # a day later, same company
    out = dedupe_payments([a, b], lambda m, c="": w.append(m))
    assert len(out) == 2 and a.suspect and b.suspect and any("possible duplicate across files" in m for m in w)


def test_identical_rows_inside_one_file_stay_two_payments():
    a = pay("h-aaaaaaaaaaaa", payer="Acme", file_no=1)
    b = pay("h-bbbbbbbbbbbb", payer="Acme", file_no=1)
    dedupe_payments([a, b], lambda m, c="": None)
    assert not a.suspect and not b.suspect


@pytest.mark.parametrize("kw", [
    {"payer": "Bravo Studio", "file_no": 2},                       # another company
    {"payer": "", "file_no": 2},                                   # no payer: no company to compare
    {"d": date(2026, 10, 8), "file_no": 2},                        # three days apart: a real second payment
    {"amount": "1000.01", "file_no": 2},                           # another amount
])
def test_different_payments_in_two_files_of_one_source_are_not_flagged(kw):
    a = pay("h-aaaaaaaaaaaa", payer="Acme Studio", file_no=1)
    b = pay("h-bbbbbbbbbbbb", **{"payer": "Acme Studio", **kw})
    dedupe_payments([a, b], lambda m, c="": None)
    assert not a.suspect and not b.suspect


def test_same_payment_in_two_files_of_one_wise_export_pair_is_held_end_to_end(tmp_path, capsys):
    base = tmp_path / "double"
    row = '"TRW-77","05-10-2026","500.00","USD","Payment INV-Q","INV-Q","Quiet Two"\n'
    _write(base, "INV-Q,Quiet Two,q@q.example,500,USD,2026-06-01,2026-07-01\n", [row], second_export=row)
    capsys.readouterr()
    assert _run(base, "2026-10-10", second=True) in (0, 2)
    err = capsys.readouterr().err
    assert "POSSIBLE_DOUBLE_COUNT" in err or "possible duplicate" in err
    assert _run(base, "2027-06-30", second=True) == 2                    # held past the stuck window: CRITICAL STUCK_HOLD
    err = capsys.readouterr().err
    assert "CRITICAL [STUCK_HOLD]" in err and "INV-Q" in err
    rows = _status(base)
    assert rows["INV-Q"]["status"] == "needs_review"
    assert not any("INV-Q" in n for n in _client_eml(base))              # no reminder: the money may have arrived


# ---- (3) internal notes live in drafts/internal/ ----------------------------------------------------------------------

def test_internal_note_goes_to_drafts_internal_and_an_unedited_root_note_from_1_1_10_is_removed(tmp_path):
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    old = drafts / "INV-Z-check.eml"
    old.write_text("old note", encoding="utf-8")
    (tmp_path / "drafts.json").write_text(json.dumps([{"invoice_id": "INV-Z", "file": "INV-Z-check.eml", "internal": True,
                                                        "sha256": hashlib.sha256(b"old note").hexdigest()}]), encoding="utf-8")
    write_drafts([{"invoice_id": "INV-Z", "stage": "check", "internal": True, "to": "a@x.example", "from_name": "invoice-chaser",
                   "from_email": "me@x.example", "subject": "Check INV-Z", "body": "for you"}], tmp_path, [])
    assert not old.exists()
    assert (drafts / "internal" / "INV-Z-check.eml").is_file()
    assert list(drafts.glob("*.eml")) == []                              # nothing that could pass for a client draft at the top level


# ---- (4) the partial ruling is in the README --------------------------------------------------------------------------

def test_readme_states_that_a_partial_remainder_reminder_is_correct():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "A remainder after a part-payment gets its reminder" in text and "ruled 1.1.11" in text
    assert "out/drafts/internal/" in text
