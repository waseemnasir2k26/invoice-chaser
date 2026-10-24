from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from . import __version__
from .config import load_config
from .drafts import write_drafts
from .llm import personal_line
from .matching import match_all
from .parsers import ParseError, parse_generic, parse_payoneer, parse_wise, read_invoices
from .report import render_html, write_status_csv
from .templating import TemplateError, load_template, render, variables_for

REPO = Path(__file__).resolve().parent.parent
DEMO_TODAY = date(2026, 10, 25)


def run(args) -> int:
    cfg = load_config(args.config)
    if args.tone:
        cfg.tone = args.tone
    if args.sender_name:
        cfg.sender_name = args.sender_name
    today = date.fromisoformat(args.today) if args.today else date.today()
    templates_dir = Path(args.templates) if args.templates else None

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

    results, leftovers = match_all(invoices, payments, today, cfg)

    items = []
    for r in results:
        if not r.stage or r.status not in ("overdue", "partial"):
            continue
        line = ""
        if args.llm:
            v = variables_for(r, cfg.sender_name, cfg.payment_details)
            line = personal_line(v["first_name"], v["outstanding"], r.days_overdue, r.invoice.id) or ""
        v = variables_for(r, cfg.sender_name, cfg.payment_details, line)
        subject, body = render(load_template(r.stage, cfg.tone, templates_dir), v)
        items.append({"invoice_id": r.invoice.id, "stage": r.stage, "to": r.invoice.email,
                      "from_name": cfg.sender_name, "from_email": cfg.sender_email,
                      "subject": subject, "body": body})

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_drafts(items, out)
    drafts = {it["invoice_id"]: it["file"] for it in items}
    write_status_csv(results, drafts, out / "status.csv")
    (out / "review.html").write_text(render_html(results, leftovers, drafts, today), encoding="utf-8")

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
    return 0


def demo(args) -> int:
    ex = REPO / "examples"
    a = argparse.Namespace(
        invoices=str(ex / "invoices.csv"), wise=[str(ex / "wise-statement.csv")],
        payoneer=[str(ex / "payoneer-transactions.csv")], bank=[str(ex / "bank-export.csv")],
        mapping=str(ex / "bank-mapping.json"), config=str(ex / "invoice-chaser.json"),
        templates=None, out=args.out, today=args.today or DEMO_TODAY.isoformat(),
        tone=args.tone, sender_name=None, llm=False)
    return run(a)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="invoice-chaser", description=__doc__)
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
    r.add_argument("--today", help="YYYY-MM-DD (default: today)")
    r.add_argument("--tone", choices=["friendly", "neutral", "firm"])
    r.add_argument("--sender-name")
    r.add_argument("--llm", action="store_true", help="personalise one line via GROQ_API_KEY or GEMINI_API_KEY if set")
    r.set_defaults(fn=run)

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
    except (ParseError, TemplateError, ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
