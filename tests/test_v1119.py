"""1.1.19: `confirm INV --payment ID` on a held (review-kind) suggestion is allowed, prints the hold reason and score before it
records, and stores confirmed_weak with the reason and the score. --all-strong still skips held rows."""
import json

from invoice_chaser.cli import main
from invoice_chaser.ledger import load_paid
from test_confirm import run, setup

# payer "Acme Studio Ltd" is not exactly "Acme Studio": the payment is held as a review-kind suggestion at score 0.69
HELD = ("INV-1,Acme Studio,a@acme.example,1000,USD,2026-09-01,2026-10-01\n",
        '"W1","05-10-2026","1000.00","USD","payment","","Acme Studio Ltd"\n')


def test_weak_confirm_prints_the_hold_reason_and_records_confirmed_weak(tmp_path, capsys):
    setup(tmp_path, *HELD)
    run(tmp_path)
    sug = json.loads((tmp_path / "out" / "suggestions.json").read_text(encoding="utf-8"))["suggestions"]
    assert sug[0]["kind"] == "review" and sug[0]["confidence"] == 0.69
    capsys.readouterr()
    assert main(["confirm", "INV-1", "--payment", "W1", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 0
    out = capsys.readouterr().out
    line = out.splitlines()[0]
    assert line.startswith("weak match (score 0.69, held: W1: no invoice id in the payment; "), out
    assert "payer 'Acme Studio Ltd' is not exactly 'Acme Studio'" in line, out
    assert line.endswith("Recording because you named this payment explicitly.")
    assert out.index("weak match") < out.index("INV-1: paid")  # printed before the record is reported
    entry = load_paid(tmp_path / "out" / "sent.json")["INV-1"]
    assert entry["confirmed_weak"] is True and entry["payments"] == ["wise:W1"]
    assert entry["reason"] == sug[0]["reason"] and entry["score"] == 0.69


def test_a_strong_confirm_records_no_weak_fields(tmp_path, capsys):
    setup(tmp_path, "INV-1,Acme Studio,a@acme.example,1000,USD,2026-09-01,2026-10-01\n",
          '"W1","05-10-2026","1000.00","USD","Payment INV-1","INV-1","Acme Studio"\n')
    run(tmp_path)
    capsys.readouterr()
    assert main(["confirm", "INV-1", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 0
    entry = load_paid(tmp_path / "out" / "sent.json")["INV-1"]
    assert "confirmed_weak" not in entry and "reason" not in entry and "score" not in entry
    assert "weak match" not in capsys.readouterr().out


def test_all_strong_still_skips_a_held_row(tmp_path, capsys):
    setup(tmp_path, *HELD)
    run(tmp_path)
    capsys.readouterr()
    assert main(["confirm", "--all-strong", "--yes", "--out", str(tmp_path / "out"), "--today", "2026-10-25"]) == 0
    assert "nothing to confirm" in capsys.readouterr().out
    assert not (tmp_path / "out" / "sent.json").exists() or "INV-1" not in load_paid(tmp_path / "out" / "sent.json")
