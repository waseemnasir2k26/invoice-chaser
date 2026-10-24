from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import date
from pathlib import Path

from . import __version__
from .config import load_config, validate
from .drafts import valid_email, write_drafts
from .ladder import STAGES
from .ledger import highest_sent, last_sent_date, load_ledger, record_sent
from .llm import personal_line
from .matching import match_all
from .parsers import (
    ParseError,
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

    invoices = read_invoices(args.invoices)
    payments = []
    for p in args.wise or []:
        payments += parse_wise(p)
    for p in args.payoneer or []:
        payments += parse_payoneer(p)
    mapping = dict(cfg.bank_mapping)
    if args.mapping:
        mapping = json.loads(Path(args.mapping).read_text(encoding="utf-8"))
    for p in args.bank or []:
        if not mapping:
            raise ParseError("--bank needs a column mapping (--mapping file.json or bank_mapping in config)")
        payments += parse_generic(p, mapping)

    out = Path(args.out)
    ledger = load_ledger(_ledger_path(args))
    results, leftovers = match_all(invoices, payments, today, cfg)

    warnings: list[str] = []
    notes: list[str] = []
    if not cfg.sender_email:
        warnings.append("sender_email is not set: set sender_email in your config file. "
                        "Drafts use a placeholder From address until you do.")
    if args.llm and not llm_keys:
        notes.append("note: --llm is set but neither GROQ_API_KEY nor GEMINI_API_KEY was found; "
                     "templates are used as written")

    items = []
    for r in results:
        entries = ledger.get(r.invoice.id)
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
    write_drafts(items, out)
    drafts = {it["invoice_id"]: it["file"] for it in items}
    write_status_csv(results, drafts, out / "status.csv")
    (out / "review.html").write_text(render_html(results, leftovers, drafts, today, warnings), encoding="utf-8")

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
    for n in notes:
        print("  " + n)
    for w in warnings:
        print(f"  WARNING: {w}", file=sys.stderr)
    return 0


def mark_sent(args) -> int:
    out = Path(args.out)
    stage = args.stage
    if not stage:
        try:
            drafted = json.loads((out / "drafts.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            drafted = []
        stage = next((d["stage"] for d in drafted if d.get("invoice_id") == args.invoice), None)
        if not stage:
            raise ValueError(f"no --stage given and no draft for {args.invoice} in {out / 'drafts.json'}")
    if stage not in STAGES:
        raise ValueError(f"--stage must be one of {', '.join(STAGES)}")
    status = out / "status.csv"
    if status.exists():
        with open(status, newline="", encoding="utf-8") as fh:
            known = {row["invoice_id"] for row in csv.DictReader(fh)}
        if args.invoice not in known:
            raise ValueError(f"unknown invoice id {args.invoice!r} (not in {status})")
    when = date.fromisoformat(args.date) if args.date else date.today()
    path = _ledger_path(args)
    record_sent(path, args.invoice, stage, when)
    print(f"recorded: {args.invoice} {stage} reminder sent {when} -> {path}")
    return 0


def demo(args) -> int:
    d = DEMO_DIR
    a = argparse.Namespace(
        invoices=str(d / "invoices.csv"), wise=[str(d / "wise-statement.csv")],
        payoneer=[str(d / "payoneer-transactions.csv")], bank=[str(d / "bank-export.csv")],
        mapping=str(d / "bank-mapping.json"), config=str(d / "invoice-chaser.json"),
        templates=None, out=args.out, today=args.today or DEMO_TODAY.isoformat(),
        tone=args.tone, sender_name=None, llm=False, ledger=None)
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
    r.add_argument("--today", help="YYYY-MM-DD (default: today)")
    r.add_argument("--tone", choices=["friendly", "neutral", "firm"])
    r.add_argument("--sender-name")
    r.add_argument("--llm", action="store_true", help="personalise one line via GROQ_API_KEY or GEMINI_API_KEY if set")
    r.set_defaults(fn=run)

    m = sub.add_parser("mark-sent", help="record a reminder you actually sent, so it is not drafted twice")
    m.add_argument("invoice")
    m.add_argument("--stage", choices=list(STAGES), help="default: the stage drafted for it in <out>/drafts.json")
    m.add_argument("--date", help="YYYY-MM-DD (default: today)")
    m.add_argument("--out", default="out")
    m.add_argument("--ledger", help="sent.json path (default: <out>/sent.json)")
    m.set_defaults(fn=mark_sent)

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
