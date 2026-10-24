from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from . import __version__
from .config import load_config, validate
from .dedupe import dedupe_payments
from .drafts import valid_email, write_drafts
from .ladder import STAGES, days_overdue, stage_for
from .ledger import (
    ci,
    highest_sent,
    last_sent_date,
    load_ledger,
    load_paid,
    load_received,
    load_rejected,
    record_paid,
    record_received,
    record_rejected,
    record_sent,
    snapshot_diff,
    snapshot_of,
    unrecord_paid,
)
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
CHECK_SNAPSHOT = True  # tests switch this off to prove the snapshot check is what catches a changed invoice


def _ledger_path(args) -> Path:
    return Path(args.ledger) if getattr(args, "ledger", None) else Path(args.out) / "sent.json"


def _pkey(p) -> str:
    return f"{p.source}:{p.id}"


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
    received_marks = ci(load_received(lpath))
    rejected = ci(load_rejected(lpath))
    warnings: list[str] = list(opts.warnings)
    notes: list[str] = []

    # ---- what you already decided: confirmed payments are consumed, rejected ones never come back ----
    consumed = {k for m in list(paid_marks.values()) + list(received_marks.values()) for k in m.get("payments", [])}
    payments = [p for p in payments if _pkey(p) not in consumed]
    excluded = {(iid, k) for iid, keys in rejected.items() for k in keys}
    by_id = {i.id.strip().lower(): i for i in invoices}
    engine_invoices = []
    for i in invoices:
        rec = received_marks.get(i.id.strip().lower())
        if rec and not (CHECK_SNAPSHOT and snapshot_diff(rec.get("snapshot", {}), i)):
            i = dataclasses.replace(i, amount=max(i.amount - Decimal(rec["amount"]), Decimal("0.01")))
        engine_invoices.append(i)
    results, leftovers, early = match_all_ex(engine_invoices, payments, today, cfg, excluded)
    for r in results:
        r.invoice = by_id[r.invoice.id.strip().lower()]

    # ---- the ledger decides paid / partial; a changed invoice goes back to review ----
    for r in results:
        key = r.invoice.id.strip().lower()
        mark, rec = paid_marks.get(key), received_marks.get(key)
        entry = mark or rec
        if not entry or r.status in ("void", "paid"):
            continue
        changes = snapshot_diff(entry.get("snapshot", {}), r.invoice) if CHECK_SNAPSHOT else []
        if changes:
            r.status, r.stage, r.paid, r.outstanding = "needs_review", None, Decimal("0.00"), r.invoice.amount
            r.why_not_paid = (f"invoice changed since you confirmed it ({'; '.join(changes)}): check it, then confirm again "
                              f"or run `invoice-chaser unmark-paid {r.invoice.id}`")
            r.note = "confirmation no longer matches invoices.csv: no reminder drafted until you check"
            r.matches, r.suggestions = [], []
            warnings.append(f"{r.invoice.id}: {r.why_not_paid}")
            continue
        r.matches, r.suggestions = ([m for m in r.matches] if rec and not mark else []), []
        if mark:
            r.status, r.paid, r.outstanding, r.stage, r.days_overdue = "paid", r.invoice.amount, Decimal("0.00"), None, 0
            r.note = f"confirmed by you on {mark['date']} ({mark.get('source', 'mark-paid')})" + (f": {mark['note']}" if mark.get("note") else "")
            r.why_not_paid, r.confidence = "", 1.0
        else:
            got = Decimal(rec["amount"])
            if r.status in ("suggested_paid", "partial_suggested"):
                continue  # a new payment is waiting for your confirm: keep waiting, no reminder
            od = days_overdue(r.invoice.due, today)
            r.status, r.paid, r.outstanding = "partial", got.quantize(Decimal("0.01")), (r.invoice.amount - got).quantize(Decimal("0.01"))
            r.days_overdue, r.stage = od, stage_for(od, cfg.stage_days)
            r.note = f"part-payment of {got:.2f} confirmed by you on {rec['date']}"
            r.why_not_paid = f"{got:.2f} of {r.invoice.amount:.2f} confirmed; {r.outstanding:.2f} still outstanding"
            r.confidence = 1.0

    # ---- the tool's own output: suggestions, never paid ----
    suggestions = _suggestions(results, received_marks, cfg, today)
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
        if r.status in ("suggested_paid", "partial_suggested"):
            r.draft_status = "no draft: waiting for your confirm or reject"
        elif r.status == "needs_review":
            r.draft_status = "no draft: check the possible payment first"
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
    (out / "suggestions.json").write_text(json.dumps({"run_date": today.isoformat(), "suggestions": suggestions},
                                                     indent=2, ensure_ascii=False), encoding="utf-8")
    drafts = {it["invoice_id"]: it["file"] for it in items}
    write_status_csv(results, drafts, out / "status.csv")
    (out / "review.html").write_text(render_html(results, leftovers, drafts, today, warnings, early, opts.ignored,
                                                 str(args.out), suggestions), encoding="utf-8")

    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print(f"invoice-chaser {__version__}  as of {today}")
    print("  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for r in results:
        if r.status in ("suggested_paid", "partial_suggested", "overdue", "partial", "needs_review"):
            print(f"  {r.invoice.id:<10} {r.status:<17} {r.outstanding:>10} {r.invoice.currency}  "
                  f"{r.days_overdue:>3}d  {r.stage or '-'}")
    strong = [s for s in suggestions if s["kind"] in ("paid", "partial")]
    if strong:
        print(f"  {len(strong)} suggested payment(s) wait for you: `invoice-chaser confirm <INV>`"
              f"{' or `confirm --all-strong`' if any(s['kind'] == 'paid' for s in strong) else ''}")
    print(f"  {len(items)} draft(s) written (nothing sent) -> {out / 'drafts'}")
    print(f"  {out / 'status.csv'}\n  {out / 'review.html'}")
    if early:
        print(f"  {len(early)} payment(s) dated outside an invoice's window: listed in review.html (check the date format)")
    for n in notes:
        print("  " + n)
    for w in warnings:
        print(f"  WARNING: {w}", file=sys.stderr)
    return 0


def _suggestions(results, received_marks, cfg, today) -> list[dict]:
    """What the engine found, as data: strong suggestions (paid / partial) and weaker candidates (review)."""
    tol_pct = Decimal(str(cfg.fx_tolerance_pct)) / 100
    out = []
    for r in results:
        if r.status not in ("suggested_paid", "partial_suggested", "needs_review"):
            continue
        matches = r.matches if r.status != "needs_review" else r.suggestions
        if not matches:
            continue
        inv = r.invoice
        prev = received_marks.get(inv.id.strip().lower())
        out.append({
            "invoice_id": inv.id,
            "kind": {"suggested_paid": "paid", "partial_suggested": "partial", "needs_review": "review"}[r.status],
            "client": inv.client, "amount": str(inv.amount), "currency": inv.currency, "due": inv.due.isoformat(),
            "snapshot": snapshot_of(inv),
            "tolerance": str(inv.amount * tol_pct),
            "previous_received": prev["amount"] if prev else "0",
            "previous_payments": list(prev.get("payments", [])) if prev else [],
            "confidence": r.confidence,
            "reason": "; ".join(m.reason for m in matches) if r.status != "needs_review" else r.why_not_paid,
            "payments": [{"key": f"{m.source}:{m.payment_id}", "id": m.payment_id, "source": m.source,
                          "date": m.payment_date.isoformat(), "amount": str(m.amount), "currency": m.currency,
                          "payer": m.payer, "description": m.payment_description, "allocated": str(m.allocated)}
                         for m in matches],
        })
    return out


# ---------------------------------------------------------------- commands that record YOUR decisions

def _status_ids(out: Path):
    status = out / "status.csv"
    if not status.exists():
        return None
    with open(status, newline="", encoding="utf-8") as fh:
        return {row["invoice_id"].strip().lower(): row["invoice_id"] for row in csv.DictReader(fh)}


def _resolve_invoice(args) -> tuple[str, date]:
    """Validate the id and date shared by the commands that record a decision.

    Ids match ignoring case and spaces, against out/status.csv. An unknown id, or a missing
    status.csv, is refused unless --force. Dates in the future are refused."""
    out = Path(args.out)
    wanted = args.invoice.strip()
    status = out / "status.csv"
    invoice_id = wanted
    known = _status_ids(out)
    if known is not None:
        if wanted.lower() in known:
            invoice_id = known[wanted.lower()]
        elif not getattr(args, "force", False):
            raise ValueError(f"unknown invoice id {wanted!r} (not in {status}); use --force to record it anyway")
    elif not getattr(args, "force", False):
        raise ValueError(f"{status} not found: run `invoice-chaser run` first, or pass --force")
    real_today = date.fromisoformat(args.today) if getattr(args, "today", None) else date.today()
    when = date.fromisoformat(args.date) if getattr(args, "date", None) else real_today
    if when > real_today:
        raise ValueError(f"--date {when} is in the future")
    return invoice_id, when


def _load_suggestions(out: Path) -> list[dict]:
    f = out / "suggestions.json"
    if not f.exists():
        raise ValueError(f"{f} not found: run `invoice-chaser run` first")
    try:
        return json.loads(f.read_text(encoding="utf-8-sig"))["suggestions"]
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError(f"{f} is not readable ({e}); run `invoice-chaser run` again") from e


def _apply_confirm(entry: dict, when: date, lpath: Path) -> str:
    keys = list(entry["previous_payments"]) + [p["key"] for p in entry["payments"]]
    got = Decimal(entry["previous_received"]) + sum((Decimal(p["allocated"]) for p in entry["payments"]), Decimal(0))
    amount = Decimal(entry["amount"])
    if entry["kind"] == "paid" or got >= amount - Decimal(entry["tolerance"]):
        record_paid(lpath, entry["invoice_id"], when, "", entry["snapshot"], keys, source="confirm")
        return f"{entry['invoice_id']}: paid ({entry['currency']} {got:,.2f} of {amount:,.2f}, payments {', '.join(p['id'] for p in entry['payments'])})"
    record_received(lpath, entry["invoice_id"], when, got, keys, entry["snapshot"])
    return (f"{entry['invoice_id']}: part-payment {entry['currency']} {got:,.2f} of {amount:,.2f} recorded; "
            f"{amount - got:,.2f} stays outstanding and will be chased")


def confirm(args) -> int:
    out = Path(args.out)
    lpath = _ledger_path(args)
    entries = _load_suggestions(out)
    real_today = date.fromisoformat(args.today) if getattr(args, "today", None) else date.today()
    when = date.fromisoformat(args.date) if args.date else real_today
    if when > real_today:
        raise ValueError(f"--date {when} is in the future")
    if args.all_strong:
        todo = [e for e in entries if e["kind"] == "paid"]
        if not todo:
            print("nothing to confirm: no strong suggested-paid invoices in the last run")
            return 0
        print("These invoices will be recorded as PAID in the ledger:")
        for e in todo:
            pays = "; ".join(f"{p['id']} {p['date']} {p['amount']} {p['currency']} from {p['payer'] or '(no payer)'}" for p in e["payments"])
            print(f"  {e['invoice_id']:<10} {e['client']}  {e['currency']} {Decimal(e['amount']):,.2f}  <- {pays}")
        if not args.yes:
            try:
                answer = input(f"Confirm {len(todo)} invoice(s) as paid? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer not in ("y", "yes"):
                print("nothing recorded")
                return 1
        for e in todo:
            print("  " + _apply_confirm(e, when, lpath))
        print(f"recorded {len(todo)} confirmation(s) -> {lpath}\n  re-run `invoice-chaser run` to update the report")
        return 0
    if not args.invoice:
        raise ValueError("give an invoice id, or use --all-strong")
    want = args.invoice.strip().lower()
    entry = next((e for e in entries if e["invoice_id"].strip().lower() == want and e["kind"] in ("paid", "partial")), None)
    if entry is None:
        weak = any(e["invoice_id"].strip().lower() == want for e in entries)
        raise ValueError(f"no strong suggestion for {args.invoice} in the last run"
                         + (" (it only has weak candidates: if you checked and it is paid, use `invoice-chaser mark-paid`)" if weak
                            else " (use `invoice-chaser mark-paid` if you checked yourself)"))
    print(_apply_confirm(entry, when, lpath))
    print(f"recorded -> {lpath}\n  re-run `invoice-chaser run` to update the report")
    return 0


def reject(args) -> int:
    out = Path(args.out)
    invoice_id, _ = _resolve_invoice(argparse.Namespace(invoice=args.invoice, out=args.out, force=args.force, today=None, date=None))
    entries = _load_suggestions(out)
    want = args.payment.strip()
    keys = {}
    for e in entries:
        if e["invoice_id"].lower() == invoice_id.lower():
            for p in e["payments"]:
                keys[p["id"].lower()] = p["key"]
                keys[p["key"].lower()] = p["key"]
    key = keys.get(want.lower())
    if key is None:
        if ":" in want and args.force:
            key = want
        else:
            raise ValueError(f"{want!r} is not a payment suggested for {invoice_id} in the last run "
                             f"(known: {', '.join(sorted({k for k in keys if ':' not in k})) or 'none'})")
    record_rejected(_ledger_path(args), invoice_id, key)
    print(f"rejected: {key} will never be suggested for {invoice_id} again\n  re-run `invoice-chaser run` to update the report")
    return 0


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
    """You checked yourself (no suggested payment needed). Stores a snapshot of the invoice as in status.csv."""
    invoice_id, when = _resolve_invoice(args)
    path = _ledger_path(args)
    existing = ci(load_paid(path)).get(invoice_id.strip().lower())
    if existing:
        print(f"WARNING: {invoice_id} is already marked paid (on {existing['date']}); nothing changed. "
              f"Run `invoice-chaser unmark-paid {invoice_id}` first to change it.", file=sys.stderr)
        return 0
    snapshot = {}
    status = Path(args.out) / "status.csv"
    if status.exists():
        with open(status, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row["invoice_id"].lower() == invoice_id.lower():
                    snapshot = {"client": row["client"], "amount": row["amount"], "currency": row["currency"], "due": row["due"]}
    record_paid(path, invoice_id, when, args.note or "", snapshot, (), source="mark-paid")
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
    p = argparse.ArgumentParser(prog="invoice-chaser",
                                description="Finds the payments for your invoices and drafts overdue reminders. "
                                            "You confirm; it never marks a client paid or chases a paid client on a guess. Never sends.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="find payments and write status.csv, review.html, suggestions.json, drafts/")
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

    def common(sp, invoice=True):
        if invoice:
            sp.add_argument("invoice")
        sp.add_argument("--out", default="out")
        sp.add_argument("--ledger", help="sent.json path (default: <out>/sent.json)")

    c = sub.add_parser("confirm", help="accept a suggested payment: the only way an invoice becomes paid")
    c.add_argument("invoice", nargs="?")
    c.add_argument("--all-strong", action="store_true", help="confirm every strong suggested-paid invoice (prints the list, asks y/N)")
    c.add_argument("--yes", action="store_true", help="do not ask (use after reviewing the list)")
    c.add_argument("--date", help="payment received on YYYY-MM-DD (default: today; not in the future)")
    c.add_argument("--today", help=argparse.SUPPRESS)
    common(c, invoice=False)
    c.set_defaults(fn=confirm)

    rj = sub.add_parser("reject", help="a suggested payment is NOT for this invoice: never suggest it again")
    common(rj)
    rj.add_argument("payment", help="payment id as shown in review.html")
    rj.add_argument("--force", action="store_true")
    rj.set_defaults(fn=reject)

    m = sub.add_parser("mark-sent", help="record a reminder you actually sent, so it is not drafted twice")
    m.add_argument("--stage", choices=list(STAGES), help="default: the stage drafted for it in <out>/drafts.json")
    mp = sub.add_parser("mark-paid", help="you checked yourself and the invoice is paid (no suggestion needed)")
    mp.add_argument("--note", help="free text, kept in sent.json")
    for sp in (m, mp):
        sp.add_argument("--date", help="YYYY-MM-DD (default: today; not in the future)")
        sp.add_argument("--today", help=argparse.SUPPRESS)
        sp.add_argument("--force", action="store_true", help="record an id that is not in <out>/status.csv")
        common(sp)
    m.set_defaults(fn=mark_sent)
    mp.set_defaults(fn=mark_paid)
    um = sub.add_parser("unmark-paid", help="undo a confirm / mark-paid")
    common(um)
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
