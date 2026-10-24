import csv
import json
import random
import time
from datetime import date, timedelta
from decimal import Decimal

import pytest

from conftest import inv, pay
from invoice_chaser import cli
from invoice_chaser.cli import main
from invoice_chaser.config import Config
from invoice_chaser.dedupe import dedupe_payments
from invoice_chaser.ledger import load_paid
from invoice_chaser.matching import MAX_CANDIDATES, match_all
from invoice_chaser.models import Invoice, Payment

TODAY = date(2026, 10, 25)


def _status(out):
    return {r["invoice_id"]: r for r in csv.DictReader(open(out / "status.csv", encoding="utf-8"))}


# ---- shared price points: never quadratic, never a reminder to the wrong person ---------

def _shared_price(n_inv, n_pay, prices, seed=3):
    rnd = random.Random(seed)
    invs = [Invoice(f"INV-{i}", f"Client {i}", "c@example.com", Decimal(prices[i % len(prices)]), "USD",
                    date(2026, 9, 1), date(2026, 9, 15)) for i in range(n_inv)]
    pays = [Payment(f"P{j}", date(2026, 9, 1) + timedelta(days=rnd.randint(1, 40)), Decimal(prices[j % len(prices)]), "USD",
                    f"Transfer {j}", f"Payer {rnd.randint(1, 4000)}", "wise") for j in range(n_pay)]
    return invs, pays


@pytest.mark.parametrize("n_inv,n_pay,prices,limit", [(1000, 5000, ["100"], 5), (600, 3000, ["100", "250", "999"], 5)])
def test_shared_price_points_stay_fast_and_safe(n_inv, n_pay, prices, limit):
    invs, pays = _shared_price(n_inv, n_pay, prices)
    t0 = time.perf_counter()
    results, _ = match_all(invs, dedupe_payments(pays, lambda m, c="": None), TODAY, Config())
    dt = time.perf_counter() - t0
    assert dt < limit, f"{n_inv}x{n_pay} took {dt:.1f}s"
    assert not [r for r in results if r.status in ("paid", "suggested_paid", "partial_suggested")]   # nobody is identified
    held = [r for r in results if r.status == "needs_review"]
    assert len(held) == len(results) and all(r.stage is None for r in results)   # and nobody is chased either
    assert "nothing identifies yours" in held[0].why_not_paid


def test_a_strangers_same_amount_payment_means_review_not_a_reminder():
    invs = [inv("INV-1", "Acme Studio", amount="500", issued=date(2026, 9, 1), due=date(2026, 9, 15))]
    stranger = pay("P1", d=date(2026, 9, 12), amount="500", payer="Someone Else", desc="transfer")
    (r,), _ = match_all(invs, [stranger], TODAY, Config())
    assert r.status == "needs_review" and r.stage is None and "Someone Else" in r.why_not_paid


def test_more_than_k_same_amount_payments_go_to_bulk_review():
    invs = [inv("INV-1", "Acme Studio", amount="500", issued=date(2026, 9, 1), due=date(2026, 9, 15))]
    pays = [pay(f"P{i}", d=date(2026, 9, 12), amount="500", payer=f"Person {i}", desc="transfer") for i in range(MAX_CANDIDATES + 1)]
    (r,), _ = match_all(invs, pays, TODAY, Config())
    assert r.status == "needs_review" and f"{MAX_CANDIDATES + 1} payments have exactly this amount" in r.why_not_paid


def test_the_clients_own_payment_is_still_found_inside_a_crowd():
    invs = [inv("INV-1", "Acme Studio", amount="500", issued=date(2026, 9, 1), due=date(2026, 9, 15))]
    pays = [pay(f"P{i}", d=date(2026, 9, 12), amount="500", payer=f"Person {i}", desc="transfer") for i in range(200)]
    pays.append(pay("MINE", d=date(2026, 9, 12), amount="500", payer="Acme Studio"))
    (r,), _ = match_all(invs, pays, TODAY, Config())
    assert r.status == "suggested_paid" and r.matches[0].payment_id == "MINE"
    pays[-1] = pay("MINE2", d=date(2026, 9, 12), amount="400", payer="Acme", desc="INV-1")
    (r,), _ = match_all(invs, pays[:-1] + [pays[-1]], TODAY, Config())
    assert r.status == "partial_suggested" and r.matches[0].payment_id == "MINE2"


def test_memory_and_recursion_errors_are_reported_cleanly(monkeypatch, tmp_path, capsys):
    (tmp_path / "i.csv").write_text("id,client,email,amount,currency,issued,due\nA,x,a@b.co,1,USD,2026-01-01,2026-02-01\n", encoding="utf-8")
    for exc in (MemoryError, RecursionError):
        def boom(*a, **k):
            raise exc()
        monkeypatch.setattr(cli, "match_all_ex", boom)
        assert main(["run", "--invoices", str(tmp_path / "i.csv"), "--out", str(tmp_path / "o")]) == 2
        assert "too large or too repetitive" in capsys.readouterr().err


# ---- stale drafts -----------------------------------------------------------------

def test_edited_draft_of_a_paid_invoice_is_moved_not_deleted(tmp_path, capsys):
    out = tmp_path / "out"
    main(["demo", "--keep-ledger", "--out", str(out)])
    f = out / "drafts" / "INV-1004-nudge.eml"
    f.write_text("MY EDITED WORDS", encoding="utf-8")
    main(["demo", "--keep-ledger", "--out", str(out)])                                       # still unpaid: kept, and the warning repeats
    assert f.read_text(encoding="utf-8") == "MY EDITED WORDS"
    assert "INV-1004-nudge.eml was edited by you" in (out / "review.html").read_text(encoding="utf-8")
    assert json.loads((out / "kept.json").read_text(encoding="utf-8")) == {"INV-1004-nudge.eml": "INV-1004"}
    capsys.readouterr()
    assert main(["mark-paid", "INV-1004", "--out", str(out), "--today", "2026-10-25"]) == 0
    main(["demo", "--keep-ledger", "--out", str(out)])
    assert not f.exists()
    moved = out / "drafts" / "stale" / "INV-1004-nudge.eml"
    assert moved.read_text(encoding="utf-8") == "MY EDITED WORDS"           # never deleted
    assert "moved to drafts/stale/" in (out / "review.html").read_text(encoding="utf-8")
    assert json.loads((out / "kept.json").read_text(encoding="utf-8")) == {}
    main(["demo", "--keep-ledger", "--out", str(out)])
    assert moved.exists() and "moved to drafts/stale/" not in (out / "review.html").read_text(encoding="utf-8")


def test_stale_name_collisions_do_not_overwrite(tmp_path):
    from invoice_chaser.drafts import write_drafts
    item = dict(invoice_id="A", stage="nudge", to="a@b.co", from_name="Me", from_email="me@b.co", subject="s", body="b")
    for round_ in (1, 2):
        write_drafts([dict(item)], tmp_path, [])
        (tmp_path / "drafts" / "A-nudge.eml").write_text(f"edit {round_}", encoding="utf-8")
        write_drafts([], tmp_path, [], {"a"})
    stale = sorted(p.read_text(encoding="utf-8") for p in (tmp_path / "drafts" / "stale").glob("*.eml"))
    assert stale == ["edit 1", "edit 2"]


# ---- mark-paid / unmark-paid --------------------------------------------------------

def test_unmark_paid_and_second_mark_warns(tmp_path, capsys):
    out = tmp_path / "out"
    main(["demo", "--keep-ledger", "--out", str(out)])
    assert main(["mark-paid", "INV-1006", "--out", str(out), "--date", "2026-10-20", "--today", "2026-10-25"]) == 0
    capsys.readouterr()
    assert main(["mark-paid", "inv-1006", "--out", str(out), "--date", "2026-10-24", "--today", "2026-10-25"]) == 0
    err = capsys.readouterr().err
    assert "already marked paid (on 2026-10-20)" in err
    assert load_paid(out / "sent.json")["INV-1006"]["date"] == "2026-10-20"      # unchanged
    main(["demo", "--keep-ledger", "--out", str(out)])
    assert _status(out)["INV-1006"]["status"] == "paid"
    assert main(["unmark-paid", "inv-1006", "--out", str(out)]) == 0
    assert load_paid(out / "sent.json") == {}
    main(["demo", "--keep-ledger", "--out", str(out)])
    assert _status(out)["INV-1006"]["status"] == "needs_review"
    assert main(["unmark-paid", "INV-1006", "--out", str(out)]) == 2
    assert "not marked paid" in capsys.readouterr().err
