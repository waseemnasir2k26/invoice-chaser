import csv
import re
import smtplib
import socket
from datetime import date
from decimal import Decimal
from email import message_from_bytes
from pathlib import Path

from conftest import inv

from invoice_chaser.cli import main
from invoice_chaser.models import InvoiceResult, Payment
from invoice_chaser.report import render_html, write_status_csv

PKG = Path(__file__).resolve().parent.parent / "invoice_chaser"


def test_demo_end_to_end(tmp_path):
    assert main(["demo", "--out", str(tmp_path)]) == 0
    rows = list(csv.DictReader(open(tmp_path / "status.csv", encoding="utf-8")))
    by = {r["invoice_id"]: r for r in rows}
    assert by["INV-1001"]["status"] == "suggested_paid"       # the tool never says "paid" by itself
    assert by["INV-1002"]["status"] == "partial_suggested" and by["INV-1002"]["outstanding"] == "1500.00"
    assert by["INV-1002"]["draft_file"] == "" and "waiting for your confirm" in by["INV-1002"]["draft_status"]
    assert by["INV-1003"]["status"] == "suggested_paid"       # Payoneer, fee shortfall
    assert by["INV-1009"]["status"] == "paid"                 # invoices.csv itself says paid
    assert not [r for r in rows if r["status"] == "paid" and r["invoice_id"] != "INV-1009"]
    assert by["INV-1004"]["stage"] == "nudge" and by["INV-1005"]["stage"] == "escalation"
    assert by["INV-1006"]["status"] == "needs_review" and by["INV-1006"]["draft_file"] == ""
    assert by["INV-1008"]["stage"] == "firm" and by["INV-1007"]["status"] == "open"
    assert sorted(p.name for p in (tmp_path / "drafts").glob("*.eml")) == [
        "INV-1004-nudge.eml", "INV-1005-escalation.eml", "INV-1008-firm.eml"]     # none for suggested or review rows
    msg = message_from_bytes((tmp_path / "drafts" / "INV-1004-nudge.eml").read_bytes())
    assert msg["To"] == "anna@kestrel.example" and "INV-1004" in msg["Subject"] and msg["X-Unsent"] == "1"
    assert (tmp_path / "review.html").read_text(encoding="utf-8").count("<html") == 1


def test_tone_flag_changes_text(tmp_path):
    main(["demo", "--out", str(tmp_path), "--tone", "firm"])
    msg = message_from_bytes((tmp_path / "drafts" / "INV-1005-escalation.eml").read_bytes())
    assert msg["Subject"] == "Escalation: invoice INV-1005 - 40 days overdue"


def test_html_escapes_everything():
    evil = '<script>alert(1)</script>"\'&'
    i = inv(client=evil, email=evil, id="<b>x</b>")
    r = InvoiceResult(i, "needs_review", Decimal("0.00"), i.amount, 3, None, 0.5, note=evil)
    p = Payment("<i>p</i>", date(2026, 10, 1), Decimal(5), "USD", evil, evil, "wise")
    html = render_html([r], [(p, Decimal(5))], {}, date(2026, 10, 25))
    assert "<script>" not in html and "<b>x</b>" not in html and "<i>p</i>" not in html
    assert "&lt;script&gt;" in html


def test_html_is_self_contained(tmp_path):
    main(["demo", "--out", str(tmp_path)])
    html = (tmp_path / "review.html").read_text(encoding="utf-8")
    assert not re.search(r"(src|href)=[\"']?https?://", html) and "<script" not in html
    for label in ("Confirm these", "Suggested paid", "Needs review", "Overdue", "Paid", "Totals by currency",
                  "invoice-chaser confirm INV-1001", "invoice-chaser confirm --all-strong"):
        assert label in html


def test_csv_formula_injection_neutralised(tmp_path):
    i = inv(client="=HYPERLINK(\"http://x\")")
    r = InvoiceResult(i, "overdue", Decimal("0.00"), i.amount, 3, "nudge", 0.0)
    write_status_csv([r], {}, tmp_path / "s.csv")
    assert "'=HYPERLINK" in (tmp_path / "s.csv").read_text(encoding="utf-8")


def test_never_sends_even_if_smtp_and_sockets_are_booby_trapped(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network/SMTP touched")
    monkeypatch.setattr(smtplib.SMTP, "__init__", boom)
    monkeypatch.setattr(smtplib.SMTP_SSL, "__init__", boom)
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert main(["demo", "--out", str(tmp_path)]) == 0
    assert main(["demo", "--out", str(tmp_path)]) == 0


def test_llm_flag_without_key_still_runs_offline(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    ex = Path(__file__).resolve().parent.parent / "examples"
    assert main(["run", "--invoices", str(ex / "invoices.csv"), "--wise", str(ex / "wise-statement.csv"),
                 "--out", str(tmp_path), "--today", "2026-10-25", "--llm"]) in (0, 2)  # 2 = NO_SENDER only


def test_source_has_no_send_capability():
    banned = re.compile(r"\b(smtplib|imaplib|poplib|sendmail|send_message|smtp)\b", re.IGNORECASE)
    for f in PKG.glob("*.py"):
        code = "\n".join(l for l in f.read_text(encoding="utf-8").splitlines() if not l.strip().startswith("#"))
        # docstrings may mention the words; strip triple-quoted blocks
        code = re.sub(r'"""[\s\S]*?"""', "", code)
        assert not banned.search(code), f"{f.name} references a send API"
    # only llm.py may open network connections
    net = re.compile(r"\b(urllib\.request|socket|http\.client|requests)\b")
    offenders = [f.name for f in PKG.glob("*.py") if net.search(re.sub(r'"""[\s\S]*?"""', "", f.read_text(encoding="utf-8")))]
    assert offenders == ["llm.py"], offenders


def test_bad_input_returns_error_code(tmp_path, capsys):
    bad = tmp_path / "i.csv"
    bad.write_text("nothing,useful\n1,2\n")
    assert main(["run", "--invoices", str(bad), "--out", str(tmp_path / "o")]) == 2
    assert "error:" in capsys.readouterr().err
