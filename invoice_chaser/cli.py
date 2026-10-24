from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from . import __version__
from .config import load_config, validate
from .drafts import valid_email, write_drafts
from .ladder import STAGES
from .dedupe import dedupe_payments
from .ledger import ci, highest_sent, last_sent_date, load_ledger, load_paid, record_paid, record_sent, unrecord_paid
from .llm import personal_line
from .matching import match_all_ex
from .parsers import (
    ParseError,
    ParseOptions,
    parse_generic,
    parse_payoneer,
    parse_wise,
    read_invoices,
)
from .report import render_html, write_status_csv
from .templating import TemplateError, flags_for, load_template, render, variables_for

DEMO_DIR = Path(__file__).resolve().parent / "demo_data"
DEMO_TODAY = date(2026, 10, 25)


def _ledger_path(args) -> Path:
    return Path(args.ledger) if getattr(args, "ledger", None) else Path(args.out) / "sent.json"


def run(args) -> int:
    cfg = load_config(args.config)
    if args.tone:
        cfg.tone = args.tone
    if args.sender_name:
        cfg.sender_name = args.sender_name
    validate(cfg)
    today = date.fromisoformat(args.today) if args.today else date.today()
    templates_dir = Path(args.templates) if args.templates else None
    llm_keys = bool(os.environ.get("GROQ_API_KEY") or os.environ.get("GEMINI_API_KEY"))

    opts = ParseOptions(date_order=getattr(args, "date_format", None), default_currency=cfg.default_currency,
                        strict=getattr(args, "strict", False),
                        decimal_comma=True if getattr(args, "decimal_comma", False) else cfg.decimal_comma)
    invoices = read_invoices(args.invoices, opts)
    payments = []
    for p in args.wise or []:
        payments += parse_wise(p, opts)
    for p in args.payoneer or []:
        payments += parse_payoneer(p, opts)
    mapping = dict(cfg.bank_mapping)
    if args.mapping:
        mapping = json.loads(Path(args.mapping).read_text(encoding="utf-8-sig"))
    for p in args.bank or []:
        if not mapping:
            raise ParseError("--bank needs a column mapping (--mapping file.json or bank_mapping in config)")
        payments += parse_generic(p, mapping, opts)
    payments = dedupe_payments(payments, opts.warn)

    out = Path(args.out)
    lpath = _ledger_path(args)
    ledger = ci(load_ledger(lpath))
    paid_marks = ci(load_paid(lpath))
    results, leftovers, early = match_all_ex(invoices, payments, today, cfg)
    for r in results:  # invoices you confirmed with `mark-paid`
        mark = paid_marks.get(r.invoice.id.strip().lower())
        if mark and r.status not in ("paid", "void"):
            r.status, r.paid, r.outstanding, r.stage, r.days_overdue = "paid", r.invoice.amount, Decimal("0.00"), None, 0
            r.why_not_paid = ""
            r.note = f"marked paid by you on {mark['date']} (mark-paid)" + (f": {mark['note']}" if mark.get("note") else "")
            r.confidence = 1.0

    warnings: list[str] = list(opts.warnings)
    notes: list[str] = []
    if not cfg.sender_email:
        warnings.append("sender_email is not set: set sender_email in your config file. "
                        "Drafts use a placeholder From address until you do.")
    if args.llm and not llm_keys:
        notes.append("note: --llm is set but neither GROQ_API_KEY nor GEMINI_API_KEY was found; "
                     "templates are used as written")

    items = []
    for r in results:
        entries = ledger.get(r.invoice.id.strip().lower())
        mine = last_sent_date(entries)
        r.last_reminder = max((d for d in (mine, r.invoice.last_reminder) if d), default=None)
        if r.status == "needs_review":
            r.draft_status = "no draft: review the possible payment first"
        if not r.stage or r.status not in ("overdue", "partial"):
            continue
        inv = r.invoice
        if not inv.email:
            r.draft_status = "missing email"
        elif not valid_email(inv.email):
            r.draft_status = "invalid email"
        if r.draft_status:
            warnings.append(f"{inv.id} ({inv.client}): {r.draft_status}, no draft written for the {r.stage} reminder")
            continue
        hs = highest_sent(entries)
        if hs and hs[0] >= STAGES.index(r.stage):
            nxt = STAGES.index(r.stage) + 1
            when = f"; next stage {STAGES[nxt]} in {cfg.stage_days[nxt] - r.days_overdue} day(s)" if nxt < len(STAGES) else ""
            r.draft_status = f"no draft: {hs[1]} already sent {hs[2]}{when}"
            continue
        line = ""
        if args.llm and llm_keys:
            v = variables_for(r, cfg.sender_name, cfg.payment_details)
            line = personal_line(v["first_name"], v["outstanding"], r.days_overdue, inv.id) or ""
        v = variables_for(r, cfg.sender_name, cfg.payment_details, line,
                          reply_days=cfg.reply_within_days, last_reminder=r.last_reminder)
        flags = flags_for(r.stage, cfg.previous_reminders, r.last_reminder, cfg.pause_work_on_final)
        subject, body = render(load_template(r.stage, cfg.tone, templates_dir), v, flags)
        items.append({"invoice_id": inv.id, "stage": r.stage, "to": inv.email,
                      "from_name": cfg.sender_name, "from_email": cfg.sender_email,
                      "subject": subject, "body": body})
        r.draft_status = "drafted"

    out.mkdir(parents=True, exist_ok=True)
    settled = {r.invoice.id.lower() for r in results if r.status in ("paid", "void")}
    write_drafts(items, out, warnings, settled)
    drafts = {it["invoice_id"]: it["file"] for it in items}
    write_status_csv(results, drafts, out / "status.csv")
    (out / "review.html").write_text(render_html(results, leftovers, drafts, today, warnings, early, opts.ignored, str(args.out)), encoding="utf-8")

    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print(f"invoice-chaser {__version__}  as of {today}")
    print("  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for r in results:
        if r.status in ("overdue", "partial", "needs_review"):
            print(f"  {r.invoice.id:<10} {r.status:<13} {r.outstanding:>10} {r.invoice.currency}  "
                  f"{r.days_overdue:>3}d  {r.stage or '-'}")
    print(f"  {len(items)} draft(s) written (nothing sent) -> {out / 'drafts'}")
    print(f"  {out / 'status.csv'}\n  {out / 'review.html'}")
    if early:
        print(f"  {len(early)} payment(s) dated outside an invoice's window: listed in review.html (check the date format)")
    for n in notes:
        print("  " + n)
    for w in warnings:
        print(f"  WARNING: {w}", file=sys.stderr)
    return 0


def _resolve_invoice(args) -> tuple[str, date]:
    """Validate the id and date shared by mark-sent and mark-paid.

    Ids match ignoring case and spaces, against out/status.csv. An unknown id, or a missing
    status.csv, is refused unless --force. Dates in the future are refused."""
    out = Path(args.out)
    wanted = args.invoice.strip()
    status = out / "status.csv"
    invoice_id = wanted
    if status.exists():
        with open(status, newline="", encoding="utf-8") as fh:
            known = {row["invoice_id"].strip().lower(): row["invoice_id"] for row in csv.DictReader(fh)}
        if wanted.lower() in known:
            invoice_id = known[wanted.lower()]
        elif not args.force:
            raise ValueError(f"unknown invoice id {wanted!r} (not in {status}); use --force to record it anyway")
    elif not args.force:
        raise ValueError(f"{status} not found: run `invoice-chaser run` first, or pass --force")
    real_today = date.fromisoformat(args.today) if getattr(args, "today", None) else date.today()
    when = date.fromisoformat(args.date) if args.date else real_today
    if when > real_today:
        raise ValueError(f"--date {when} is in the future")
    return invoice_id, when


def mark_sent(args) -> int:
    out = Path(args.out)
    invoice_id, when = _resolve_invoice(args)
    stage = args.stage
    if not stage:
        try:
            drafted = json.loads((out / "drafts.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            drafted = []
        stage = next((d["stage"] for d in drafted if str(d.get("invoice_id", "")).lower() == invoice_id.lower()), None)
        if not stage:
            raise ValueError(f"no --stage given and no draft for {invoice_id} in {out / 'drafts.json'}")
    if stage not in STAGES:
        raise ValueError(f"--stage must be one of {', '.join(STAGES)}")
    path = _ledger_path(args)
    record_sent(path, invoice_id, stage, when)
    print(f"recorded: {invoice_id} {stage} reminder sent {when} -> {path}")
    return 0


def mark_paid(args) -> int:
    invoice_id, when = _resolve_invoice(args)
    path = _ledger_path(args)
    existing = ci(load_paid(path)).get(invoice_id.strip().lower())
    if existing:
        print(f"WARNING: {invoice_id} is already marked paid (on {existing['date']}); nothing changed. "
              f"Run `invoice-chaser unmark-paid {invoice_id}` first to change it.", file=sys.stderr)
        return 0
    record_paid(path, invoice_id, when, args.note or "")
    print(f"recorded: {invoice_id} confirmed paid on {when} -> {path}")
    print("  re-run `invoice-chaser run` to update the report")
    return 0


def unmark_paid(args) -> int:
    path = _ledger_path(args)
    if not unrecord_paid(path, args.invoice):
        raise ValueError(f"{args.invoice} is not marked paid in {path}")
    print(f"removed: {args.invoice} is no longer marked paid -> {path}")
    print("  re-run `invoice-chaser run` to update the report")
    return 0


def demo(args) -> int:
    d = DEMO_DIR
    a = argparse.Namespace(
        invoices=str(d / "invoices.csv"), wise=[str(d / "wise-statement.csv")],
        payoneer=[str(d / "payoneer-transactions.csv")], bank=[str(d / "bank-export.csv")],
        mapping=str(d / "bank-mapping.json"), config=str(d / "invoice-chaser.json"),
        templates=None, out=args.out, today=args.today or DEMO_TODAY.isoformat(),
        tone=args.tone, sender_name=None, llm=False, ledger=None, date_format=None, strict=False, decimal_comma=False)
    return run(a)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="invoice-chaser", description="Match payments to invoices and draft overdue reminders. Never sends.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="match payments and write status.csv, review.html, drafts/")
    r.add_argument("--invoices", required=True)
    r.add_argument("--wise", action="append", help="Wise statement CSV (repeatable)")
    r.add_argument("--payoneer", action="append", help="Payoneer transactions CSV (repeatable)")
    r.add_argument("--bank", action="append", help="generic bank CSV (repeatable), needs --mapping")
    r.add_argument("--mapping", help="JSON column mapping for --bank files")
    r.add_argument("--config")
    r.add_argument("--templates", help="directory of <stage>.<tone>.md files")
    r.add_argument("--out", default="out")
    r.add_argument("--ledger", help="sent.json path (default: <out>/sent.json)")
    r.add_argument("--date-format", choices=["dmy", "mdy", "ymd"], help="read dates in this order (default: auto, day-first when ambiguous)")
    r.add_argument("--decimal-comma", action="store_true", help="amounts are written 1.234,56 (default: only unambiguous amounts are read)")
    r.add_argument("--strict", action="store_true", help="abort on the first bad row instead of skipping it with a warning")
    r.add_argument("--today", help="YYYY-MM-DD (default: today)")
    r.add_argument("--tone", choices=["friendly", "neutral", "firm"])
    r.add_argument("--sender-name")
    r.add_argument("--llm", action="store_true", help="personalise one line via GROQ_API_KEY or GEMINI_API_KEY if set")
    r.set_defaults(fn=run)

    m = sub.add_parser("mark-sent", help="record a reminder you actually sent, so it is not drafted twice")
    m.add_argument("invoice")
    m.add_argument("--stage", choices=list(STAGES), help="default: the stage drafted for it in <out>/drafts.json")
    m.add_argument("--note", help=argparse.SUPPRESS)
    mp = sub.add_parser("mark-paid", help="confirm an invoice is paid (e.g. after reviewing a needs-review row)")
    mp.add_argument("invoice")
    mp.add_argument("--note", help="free text, kept in sent.json")
    for sp in (m, mp):
        sp.add_argument("--date", help="YYYY-MM-DD (default: today; not in the future)")
        sp.add_argument("--today", help=argparse.SUPPRESS)
        sp.add_argument("--out", default="out")
        sp.add_argument("--ledger", help="sent.json path (default: <out>/sent.json)")
        sp.add_argument("--force", action="store_true", help="record an id that is not in <out>/status.csv")
    m.set_defaults(fn=mark_sent)
    mp.set_defaults(fn=mark_paid)
    um = sub.add_parser("unmark-paid", help="undo a mark-paid confirmation")
    um.add_argument("invoice")
    um.add_argument("--out", default="out")
    um.add_argument("--ledger", help="sent.json path (default: <out>/sent.json)")
    um.set_defaults(fn=unmark_paid)

    d = sub.add_parser("demo", help="run on the bundled synthetic samples")
    d.add_argument("--out", default="out")
    d.add_argument("--today")
    d.add_argument("--tone", choices=["friendly", "neutral", "firm"])
    d.set_defaults(fn=demo)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (ParseError, TemplateError, ValueError, ArithmeticError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (MemoryError, RecursionError):
        print("error: the input is too large or too repetitive to process (out of memory or recursion limit). "
              "Split the exports into smaller date ranges and try again.", file=sys.stderr)
        return 2
