from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from . import __version__
from .config import load_config, validate
from .decision import IdIndex, canon_id, field_texts, visible
from .notices import Notice
from .dedupe import (
    is_hash_id,
    dedupe_payments,
    flag_same_payment_in_two_exports,
    payment_row,
    repeat_of,
    row_matches,
)
from .drafts import CHECK_FIRST_DIR, PLACEHOLDER_FROM, valid_email, write_drafts
from .gate import Hold, client_draft_allowed, renamed_match, same_money
from .ladder import STAGES, days_overdue, stage_for
from .ledger import (
    apply_confirmations,
    atomic_write_text,
    bound_to,
    ci,
    highest_sent,
    last_sent_date,
    load_double_bindings,
    load_ledger,
    load_paid,
    load_payment_rows,
    load_received,
    load_rejected,
    load_rejected_rows,
    forget_invoice,
    forget_key,
    read_ledger,
    rebind_keys,
    rename_invoice,
    record_paid,
    record_rejected,
    record_sent,
    reset_ledger,
    snapshot_diff,
    snapshot_of,
    unbind_payment,
    unrecord_paid,
)
from .llm import personal_line
from .matching import match_all_ex, plausible_payment_refs, refund_rows
from .models import Match
from .parsers import (
    ParseError,
    ParseOptions,
    parse_generic,
    parse_payoneer,
    parse_wise,
    read_invoices,
)
from .report import render_html, write_status_csv
from .shellquote import quote
from .templating import TemplateError, flags_for, load_template, money, render, variables_for

DEMO_DIR = Path(__file__).resolve().parent / "demo_data"
DEMO_TODAY = date(2026, 10, 25)
SEEN_SCHEMA = 1
HELD = ("suggested_paid", "partial_suggested", "needs_review")  # statuses that wait for your decision
# a payment that may be counted twice (another export, or a re-export of a confirmed payment): the money may already have arrived
DOUBLE_COUNT = re.compile(r"two exports|may be the same payment|possible repeat of a payment|identical payment found")
REFUND_NAME = "client name contains a refund word"  # Match.blocker text when a refund-looking client name is the reason
STUCK_HOLD_KINDS = ("double_count", "refund_name")  # holds where the money may have arrived: CRITICAL STUCK_HOLD past the stuck window
CHECK_SNAPSHOT = True  # tests switch this off to prove the snapshot check is what catches a changed invoice


def _ledger_path(args) -> Path:
    return Path(args.ledger) if getattr(args, "ledger", None) else Path(args.out) / "sent.json"


def _load_exports(args, cfg):
    """Parse the invoices and payment exports exactly as `run` does. The commands that look for orphan candidates use the same code,
    on the exports as they are now, so a candidate is always one that exists today."""
    opts = ParseOptions(date_order=getattr(args, "date_format", None), default_currency=cfg.default_currency,
                        strict=getattr(args, "strict", False),
                        decimal_comma=True if getattr(args, "decimal_comma", False) else cfg.decimal_comma)
    invoices = read_invoices(args.invoices, opts)
    if not invoices:
        skipped = [str(w) for w in opts.warnings if getattr(w, "code", "") == "INVOICE_ROW_DROPPED"]
        raise ParseError(f"no invoices found in {Path(args.invoices).name}: it has no usable rows (an empty file, only the header line, or every "
                         f"row was skipped{': ' + '; '.join(skipped[:3]) if skipped else ''}). Nothing was written.")
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
    return invoices, payments, opts


def _export_args(args) -> dict:
    """The inputs of a run, as absolute paths, so the candidate lookups can re-read the same kinds of exports later (local only)."""
    def ab(p):
        return str(Path(p).resolve()) if p else None
    return {"invoices": ab(args.invoices), "wise": [ab(p) for p in args.wise or []], "payoneer": [ab(p) for p in args.payoneer or []],
            "bank": [ab(p) for p in args.bank or []], "mapping": ab(args.mapping), "config": ab(args.config),
            "date_format": getattr(args, "date_format", None), "decimal_comma": bool(getattr(args, "decimal_comma", False)),
            "strict": bool(getattr(args, "strict", False))}


def _pkey(p) -> str:
    return f"{p.source}:{p.id}"


def _file_hash(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _out_hint(out) -> str:
    """The --out value shown in copy-paste commands: relative to where you are now, never an absolute path."""
    out = Path(out)
    if not out.is_absolute():
        return str(out)
    try:
        rel = os.path.relpath(out)
    except ValueError:  # another drive on Windows
        return out.name
    return rel if rel != "." else "."


def _ledger_state(paid, received, rejected, invoice_id: str) -> str:
    """A fingerprint of everything the ledger says about one invoice: a suggestion is only valid while it is unchanged."""
    k = invoice_id.strip().lower()
    state = {"paid": paid.get(k), "received": received.get(k), "rejected": sorted(rejected.get(k, []))}
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


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

    invoices, payments, opts = _load_exports(args, cfg)
    all_rows = [*payments, *opts.ignored]  # 1.1.20: every payment row of the exports, refunds included, before any decision removes one

    out = Path(args.out)
    lpath = _ledger_path(args)
    warnings: list[str] = list(opts.warnings)
    notes: list[str] = []
    ledger = ci(load_ledger(lpath))
    paid_marks = ci(load_paid(lpath))
    received_marks = ci(load_received(lpath))
    rejected = ci(load_rejected(lpath))
    # I1, checked on every run: a payment key backing two invoices holds both of them
    double = load_double_bindings(lpath)
    double_held: dict[str, str] = {}
    for key, owners in sorted(double.items()):
        cmds = "; ".join(f"`invoice-chaser unbind {quote(inv)} {quote(key)}`" for inv in owners)
        _warn(warnings, "PAYMENT_DOUBLE_BOUND", f"payment {key} backs {' AND '.join(owners)} in your ledger, but a payment can back one invoice only. "
                        f"Both are held: no reminder, not counted paid. Next step: check your statement, then run {cmds} for each invoice "
                        f"that did NOT receive it (both, if neither did), and run again.")
        for inv in owners:
            double_held.setdefault(inv.strip().lower(), f"payment {key} also backs another invoice (PAYMENT_DOUBLE_BOUND)")

    # ---- what you already decided: confirmed payments are consumed, rejected ones never come back ----
    consumed = {k: inv for inv, m in list(paid_marks.items()) + list(received_marks.items()) for k in m.get("payments", [])}
    confirmed_rows = load_payment_rows(lpath)
    rows_by_key: dict[str, dict] = {}
    for row in confirmed_rows:
        rows_by_key.setdefault(row["key"], row)
    all_keys = {_pkey(p) for p in payments}
    kept = []
    human_confirmed: set[str] = set()  # 1.1.20: payments you confirmed (for any invoice): the PLAUSIBLE_PAYMENT gate leaves them out
    for p in payments:
        k = _pkey(p)
        if k in consumed:
            row = rows_by_key.get(k)
            if row is not None and row_matches(p, row, 0):
                human_confirmed.add(k)
                continue  # the very payment you confirmed (same id AND same date, amount, currency and payer): consumed
            owner = row["invoice"] if row else consumed[k]
            if row is not None:
                p.suspect = f"id collision with a payment you confirmed for {owner}: same id, but a different date, amount or payer"
            else:
                p.suspect = (f"its id is in your ledger for {owner}, but the ledger has no record of what that payment looked like, "
                             f"so it cannot be shown to be the same one")
            _warn(warnings, "ID_COLLISION", f"{p.source} payment {p.id} ({p.amount} {p.currency}, {p.date}, payer {p.payer or 'none'}): {p.suspect}. It is NOT "
                            f"treated as already confirmed: it is held in needs review. Next step: compare it with the confirmed payment on your "
                            f"statement; if it is a different payment, check it and run `invoice-chaser mark-paid <invoice>`; if it is the same one, "
                            f"nothing to do.")
        kept.append(p)
    payments = kept
    cross = opts.cross_file_ids()
    id_index = IdIndex((i.id for i in invoices), cfg.ref_prefixes)
    for p in payments:
        if p.id in cross and not p.suspect:
            p.suspect = "identical payment found in two exports: one real payment or two?"
        named = _named_ids(p, id_index)
        hit = repeat_of(p, confirmed_rows, named)
        if hit:  # a cosmetic re-export changed the id, but it is the same date, amount, currency and payer
            p.suspect = f"possible repeat of a payment you already confirmed for {hit['invoice']}: not counted again"
            _warn(warnings, "POSSIBLE_DOUBLE_COUNT", f"{p.source} payment {p.id} ({p.amount} {p.currency}, {p.date}) looks like a repeat of a payment you "
                            f"confirmed for {hit['invoice']}; it is held in needs review and not counted again. Next step: if it is the same payment, "
                            f"leave it. If you checked and it is a separate one, run `invoice-chaser mark-paid {quote(hit['invoice'])}` or the invoice it belongs to.")
    flag_same_payment_in_two_exports(payments, lambda p: _named_ids(p, id_index), lambda m, c="NOTE": _warn(warnings, c, m))
    if cross:
        _warn(warnings, "POSSIBLE_DOUBLE_COUNT", f"{len(cross)} payment row(s) are identical in two of your exports (one real payment or two?): both are "
                        f"kept and held in needs review, never silently merged or dropped. Next step: check your statement. If it is one payment, "
                        f"leave one of the two files out of the command and run again; if both are real, check them and `invoice-chaser mark-paid <invoice>`.")
    # a rejection only applies to the payment you rejected: same id AND same date, amount, currency and payer
    rejected_rows = ci(load_rejected_rows(lpath))
    by_key: dict[str, list] = {}
    for p in payments:
        by_key.setdefault(_pkey(p), []).append(p)
    excluded = set()
    for iid, keys in rejected.items():
        for k in keys:
            row = rejected_rows.get(iid, {}).get(k)
            for p in by_key.get(k, []):
                if row is None:
                    _warn(warnings, "REJECTION_UNVERIFIED", f"your rejection of {k} for {iid} was made by an older version and has no fingerprint, so it cannot be "
                                    f"checked: the payment is suggested again. Next step: reject it once more to make it stick.")
                elif row_matches(p, row, 0):
                    excluded.add((iid, k))
                else:
                    p.suspect = p.suspect or f"id collision with a payment you rejected for {iid}: same id, but a different date, amount or payer"
                    _warn(warnings, "ID_COLLISION", f"{p.source} payment {p.id} ({p.amount} {p.currency}, {p.date}) shares its id with a payment you rejected for "
                                    f"{iid}, but it is a different payment; it is held in needs review. Next step: check it on your statement, "
                                    f"then `invoice-chaser mark-paid <invoice>` if it is paid.")
    orphans = _find_orphans(lpath, payments, invoices, id_index, all_keys)
    for o in orphans:
        _warn(warnings, "ORPHANED_DECISION", _orphan_text(o))
    by_exact = {i.id: i for i in invoices}
    engine_invoices = []
    for i in invoices:
        rec = received_marks.get(i.id.strip().lower())
        if rec and not (CHECK_SNAPSHOT and snapshot_diff(rec.get("snapshot", {}), i)):
            i = dataclasses.replace(i, amount=max(i.amount - Decimal(rec["amount"]), Decimal("0.01")))
        engine_invoices.append(i)
    results, leftovers, early = match_all_ex(engine_invoices, payments, today, cfg, excluded)
    for r in results:
        r.invoice = by_exact[r.invoice.id]
    # the payment evidence of every invoice, kept before the decisions below clear it: the holds that stop a client draft come from it
    evidence = {r.invoice.id.strip().lower(): [*r.matches, *r.suggestions] for r in results}

    # ---- the ledger decides paid / partial; a changed invoice goes back to review ----
    for r in results:
        key = r.invoice.id.strip().lower()
        mark, rec = paid_marks.get(key), received_marks.get(key)
        entry = mark or rec
        if entry:
            r.confirmed_on = entry.get("date", "")
        if not entry or r.status in ("void", "paid"):
            continue
        changes = snapshot_diff(entry.get("snapshot", {}), r.invoice) if CHECK_SNAPSHOT else []
        if changes:
            r.status, r.stage, r.paid, r.outstanding = "needs_review", None, Decimal("0.00"), r.invoice.amount
            r.why_not_paid = (f"invoice changed since you confirmed it ({'; '.join(changes)}): check it, then run "
                              f"`invoice-chaser unmark-paid {quote(r.invoice.id)}` and run again for a fresh suggestion "
                              f"(or `invoice-chaser mark-paid {quote(r.invoice.id)}` once you have checked)")
            r.note = "confirmation no longer matches invoices.csv: no reminder drafted until you check"
            r.matches, r.suggestions = [], []
            _warn(warnings, "CHANGED_INVOICE", f"{r.invoice.id}: {r.why_not_paid}")
            continue
        r.matches, r.suggestions = ([m for m in r.matches] if rec and not mark else []), []
        if mark:
            r.status, r.paid, r.outstanding, r.stage, r.days_overdue = "paid", r.invoice.amount, Decimal("0.00"), None, 0
            r.note = f"confirmed by you on {mark['date']} ({mark.get('source', 'mark-paid')})" + (f": {mark['note']}" if mark.get("note") else "")
            r.why_not_paid, r.confidence = "", 1.0
        else:
            got = Decimal(rec["amount"])
            if r.status in ("suggested_paid", "partial_suggested", "needs_review"):
                if r.status == "needs_review":  # e.g. a possible repeat of the confirmed payment: show what you confirmed, send no reminder
                    r.paid = got_amount = Decimal(rec["amount"]).quantize(Decimal("0.01"))
                    r.outstanding = (r.invoice.amount - got_amount).quantize(Decimal("0.01"))
                continue  # a payment is waiting for your decision: no reminder
            od = days_overdue(r.invoice.due, today)
            r.status, r.paid, r.outstanding = "partial", got.quantize(Decimal("0.01")), (r.invoice.amount - got).quantize(Decimal("0.01"))
            r.days_overdue, r.stage = od, stage_for(od, cfg.stage_days)
            r.note = f"part-payment of {got:.2f} confirmed by you on {rec['date']}"
            r.why_not_paid = f"{got:.2f} of {r.invoice.amount:.2f} confirmed; {r.outstanding:.2f} still outstanding"
            r.confidence = 1.0

    # an orphaned decision holds the invoices it touches: no reminder, no suggestion, until you rebind or forget it
    for o in orphans:
        for hold_id in o["hold"]:
            for r in results:
                if r.invoice.id.strip().lower() == hold_id.strip().lower():
                    r.status, r.stage, r.paid, r.outstanding = "needs_review", None, Decimal("0.00"), r.invoice.amount
                    r.matches, r.suggestions = [], []
                    r.why_not_paid = _orphan_text(o)
                    r.note = "ORPHANED_DECISION: no reminder is drafted until you rebind or forget it"
    # a payment backing two invoices (I1): both are held, neither is counted paid, until one of them is unbound
    for r in results:
        reason = double_held.get(r.invoice.id.strip().lower())
        if reason:
            r.status, r.stage, r.paid, r.outstanding = "needs_review", None, Decimal("0.00"), r.invoice.amount
            r.matches, r.suggestions = [], []
            r.why_not_paid = reason
            r.note = "PAYMENT_DOUBLE_BOUND: not counted paid, no reminder, until unbound"
    # INV-6001 and inv-6001 in the same file: never merged, never guessed. Both wait in needs review and neither gets a reminder.
    for r in results:
        if " ".join(r.invoice.id.casefold().split()) in opts.case_dup_ids:
            r.status, r.stage, r.paid, r.outstanding = "needs_review", None, Decimal("0.00"), r.invoice.amount
            r.matches, r.suggestions = [], []
            r.why_not_paid = ("duplicate invoice id (case differs): another invoice in the file has the same id with different capitals. "
                              "Give each invoice its own id in invoices.csv, then run again")
            r.note = "no reminder is drafted for either invoice until the ids are unique"

    # every held invoice shows its real days overdue, whichever path held it (a confirmed invoice that is now held was zeroed above)
    for r in results:
        if r.status in HELD:
            r.days_overdue = days_overdue(r.invoice.due, today)
    # 1.1.13: what the client still owes = the invoice amount less what you CONFIRMED. An unconfirmed suggestion never reduces it,
    # so a CHECK FIRST draft never says "for 0.00".
    for r in results:
        rec = received_marks.get(r.invoice.id.strip().lower())
        confirmed = Decimal(rec["amount"]) if rec and not (CHECK_SNAPSHOT and snapshot_diff(rec.get("snapshot", {}), r.invoice)) else Decimal("0")
        r.owed = (r.invoice.amount - confirmed).quantize(Decimal("0.01"))

    # 1.1.20 PLAUSIBLE_PAYMENT and REFUND: the last gate before a client reminder. EVERY row of the exports is checked, not only the rows
    # matched to an invoice, so a payment consumed by another invoice still holds this one. The held invoice is needs_review with the
    # exact confirm / reject commands, and gets no reminder in drafts/. Only a payment you confirmed, or rejected for THIS invoice, is left out.
    flag_here = "" if _out_hint(args.out) == "out" else f" --out {quote(_out_hint(args.out))}"
    for r in results:
        inv = r.invoice
        key = inv.id.strip().lower()
        if r.status == "suggested_paid":
            refunds = refund_rows(inv, all_rows, today)
            if refunds:
                why = "; ".join(f"refund seen on {p.date.isoformat()} for {abs(p.amount):.2f} {p.currency}: check the repayment"
                                for p in refunds)
                _hold_for_check(r, why, "REFUND_REPAYMENT", "refund seen: no reminder is drafted until you check the repayment", warnings,
                                r.matches)
            continue
        if r.status not in ("overdue", "partial") or not r.stage:
            continue  # only an invoice that would get an ordinary reminder reaches the gate
        skip = human_confirmed | {k for (iid, k) in excluded if iid == key}
        hits = plausible_payment_refs(inv, all_rows, r.owed, skip, cfg.ref_prefixes)
        if not hits:
            continue
        matches = [_plausible_match(inv, p, why, flag_here) for p, why in hits]
        why = " | ".join(m.blocker for m in matches)
        _hold_for_check(r, why, "PLAUSIBLE_PAYMENT", "possible payment found: no reminder is drafted until you check it", warnings, matches)

    # 1.1.16: an unidentified payment near an open invoice holds it: say so on every run, with the confirm / reject commands
    for r in results:
        if r.status == "needs_review":
            for m in r.suggestions:
                if m.near:
                    _warn(warnings, "NEAR_AMOUNT", f"{r.invoice.id}: {m.blocker}")
                elif m.mismatch:
                    _warn(warnings, "CURRENCY_MISMATCH", f"{r.invoice.id}: {m.blocker}")

    # ---- the tool's own output: suggestions, never paid ----
    for r in results:  # say why a rejected payment is no longer suggested
        keys = rejected.get(r.invoice.id.strip().lower())
        if keys and r.status in ("overdue", "needs_review"):
            r.why_not_paid = f"you rejected {', '.join(k.split(':', 1)[-1] for k in keys)} for this invoice; " + (r.why_not_paid or "")
    _age_and_stick(results, out, cfg, today, warnings)
    suggestions = _suggestions(results, received_marks, cfg, today)
    for s in suggestions:
        s["ledger_state"] = _ledger_state(paid_marks, received_marks, rejected, s["invoice_id"])
        if s["kind"] == "paid" and Decimal(s["previous_received"]) > 0:
            _warn(warnings, "DEPENDENT_TOTAL", f"{s['invoice_id']}: suggested as FULLY paid only because it adds up with the part-payment of "
                            f"{s['previous_received']} you confirmed earlier. If that earlier confirmation was wrong, this is not paid.")
    if not cfg.sender_email:
        _warn(warnings, "NO_SENDER", f"NO_SENDER: sender_email is not set: set sender_email in your config file before you send anything. "
                        f"Drafts are still written, with the placeholder From address {PLACEHOLDER_FROM}.")
    if args.llm and not llm_keys:
        notes.append("note: --llm is set but neither GROQ_API_KEY nor GEMINI_API_KEY was found; "
                     "templates are used as written")

    holds, gate_ledger = _build_holds(results, orphans, double_held, invoices, paid_marks, received_marks, evidence)
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
        item = _client_item(r, r.stage, cfg, args, templates_dir, llm_keys, entries, orphans, warnings, holds, gate_ledger, check_first=False)
        if item is not None:
            items.append(item)

    stuck = sorted((r for r in results if r.stuck), key=lambda r: -r.days_overdue)
    n_client = len(items)
    for r in stuck:  # an internal note to YOU, never to the client
        items.append(_check_item(r, cfg, _out_hint(args.out)))
        r.draft_status = "no client draft; STUCK: internal note drafted for you"
    # a held invoice that has waited long enough AND is far enough overdue also gets the normal client reminder for its stage,
    # written to drafts/check-first/ with CHECK FIRST in the subject and body. Every client draft passes client_draft_allowed first.
    check_first: list[dict] = []
    for r in results:
        if r.status not in HELD or not (r.waiting_days > cfg.stuck_after_days and r.days_overdue > cfg.stuck_overdue_days):
            continue
        prefix = "STUCK: internal note drafted for you; " if r.stuck else ""
        it = _check_first_item(r, cfg, args, templates_dir, llm_keys, ledger.get(r.invoice.id.strip().lower()), orphans, warnings,
                               holds, gate_ledger)
        r.draft_status = prefix + r.draft_status
        if it:
            check_first.append(it)
    items.extend(check_first)
    # M1: a hold where the money may have arrived (possible double count, or a refund-looking client name) that has passed the stuck
    # window never gets a client reminder, and is reported on every run: CRITICAL STUCK_HOLD, exit code 2.
    for r in results:
        if r.status not in HELD or not r.stuck:
            continue
        key = r.invoice.id.strip().lower()
        kinds = {h.kind for h in holds if h.invoice == key}
        if not kinds & set(STUCK_HOLD_KINDS):
            continue
        if {"double_count", "refund_name"} <= kinds:
            why = "a possible double count of another payment, and a client name that contains a refund word"
        elif "double_count" in kinds:
            why = "a possible double count of another payment"
        else:
            why = "a client name that contains a refund word"
        _warn(warnings, "STUCK_HOLD", f"{r.invoice.id} ({r.invoice.client}) is {r.days_overdue} days overdue and has waited {r.waiting_days} day(s) "
                                      f"because of {why}. No client reminder is drafted: the money may have arrived. Next step: check your bank "
                                      f"statement, then `invoice-chaser confirm`, `reject` or `mark-paid {quote(r.invoice.id)}`.")

    # 1.1.13: ANY held invoice more than stuck_overdue_days overdue is CRITICAL on every run, including the first time it is seen,
    # and names the exact commands that resolve it. run exits 2 until it is decided.
    flag_out = "" if _out_hint(args.out) == "out" else f" --out {quote(_out_hint(args.out))}"
    for r in results:
        if r.status not in HELD or r.days_overdue <= cfg.stuck_overdue_days:
            continue
        cmds = _resolve_commands(r, orphans, paid_marks, received_marks, flag_out)
        _warn(warnings, "OVERDUE_HELD", f"{r.invoice.id} ({r.invoice.client}) is {r.days_overdue} days overdue and held for your decision "
                                        f"({r.why_not_paid or r.note}). No client reminder is drafted while it waits. Resolve it now: "
                                        f"{'; '.join(cmds)}")

    out.mkdir(parents=True, exist_ok=True)
    settled = {r.invoice.id.lower() for r in results if r.status in ("paid", "void")}
    write_drafts(items, out, warnings, settled)
    files = [args.invoices, *(args.wise or []), *(args.payoneer or []), *(args.bank or []),
             *([args.mapping] if args.mapping else []), *([args.config] if args.config else [])]
    inputs = [{"name": Path(f).name, "sha256": _file_hash(f)} for f in files]
    # Full paths stay in a local-only file (never in anything you might share): confirm needs them to re-check the exports.
    (out / "local-state.json").write_text(json.dumps({"note": "local only: do not share", "args": _export_args(args),
                                                      "inputs": [{"name": Path(f).name, "path": str(Path(f).resolve()),
                                                                  "sha256": _file_hash(f)} for f in files]}, indent=2), encoding="utf-8")
    (out / "orphans.json").write_text(json.dumps({"generated_on": today.isoformat(), "orphans": orphans}, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "suggestions.json").write_text(json.dumps({"run_date": today.isoformat(), "generated_on": today.isoformat(),
                                                     "inputs": inputs, "suggestions": suggestions}, indent=2, ensure_ascii=False), encoding="utf-8")
    drafts = {it["invoice_id"]: it["file"] for it in items if not it.get("internal")}
    write_status_csv(results, drafts, out / "status.csv")
    (out / "review.html").write_text(render_html(results, leftovers, drafts, today, warnings, early, opts.ignored,
                                                 _out_hint(args.out), suggestions, check_first=check_first), encoding="utf-8")

    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print(f"invoice-chaser {__version__}  as of {today}")
    print("  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for r in results:
        if r.status in ("suggested_paid", "partial_suggested", "overdue", "partial", "needs_review"):
            when = (f"held: {r.note or r.why_not_paid or 'waiting for your decision'}" if r.status == "needs_review"
                    else f"{r.days_overdue:>3}d")
            print(f"  {r.invoice.id:<10} {r.status:<17} {r.outstanding:>10} {r.invoice.currency}  {when}  {r.stage or '-'}")
    strong = [s for s in suggestions if s["kind"] in ("paid", "partial")]
    if strong:
        print(f"  {len(strong)} suggested payment(s) wait for you: `invoice-chaser confirm <INV>`"
              f"{' or `confirm --all-strong`' if any(s['kind'] == 'paid' for s in strong) else ''}")
    if stuck:
        print(f"  !! {len(stuck)} invoice(s) STUCK waiting for your decision: oldest {stuck[0].invoice.id}, "
              f"{stuck[0].days_overdue} days overdue. Decide with confirm / reject / mark-paid (a note to you is in drafts/internal/).")
    print(f"  {n_client} draft(s) written (nothing sent) -> {out / 'drafts'}")
    if check_first:
        print(f"  {len(check_first)} CHECK FIRST reminder(s) written -> {out / 'drafts' / CHECK_FIRST_DIR}: verify the possible payment before sending")
    print(f"  {out / 'status.csv'}\n  {out / 'review.html'}")
    if early:
        print(f"  {len(early)} payment(s) dated outside an invoice's window: listed in review.html (check the date format)")
    for n in notes:
        print("  " + n)
    _print_warnings(warnings, out)
    if any(getattr(w, "severity", "warning") == "critical" for w in warnings):
        return 2  # a CRITICAL notice (see README: exit codes) must not be missed by a script or a scheduled job
    return 0


def _plausible_match(inv, p, why: list[str], flag: str) -> Match:
    """A candidate for a PLAUSIBLE_PAYMENT hold: listed under suggestions so `confirm --payment` and `reject` can name it."""
    cmd_inv, cmd_pay = quote(inv.id), quote(p.id)
    reasons = "; ".join(why)
    return Match(inv.id, p.id, p.date, p.description, 0.0, reasons,
                 blocker=(f"PLAUSIBLE_PAYMENT: payment {p.id} ({p.amount} {p.currency}, {p.date.isoformat()}, payer {p.payer or 'none'}) "
                          f"could be this invoice's payment: {reasons}. If it pays this invoice: invoice-chaser confirm {cmd_inv} "
                          f"--payment {cmd_pay}{flag}. If not: invoice-chaser reject {cmd_inv} {cmd_pay}{flag}"),
                 payer=p.payer, source=p.source, currency=p.currency, amount=p.amount, suspect=p.suspect)


def _hold_for_check(r, why: str, code: str, note: str, warnings: list, suggestions: list) -> None:
    """1.1.20: hold an invoice that would have had a reminder as needs_review. Nothing is paid, nothing is drafted."""
    r.status, r.stage, r.paid, r.outstanding = "needs_review", None, Decimal("0.00"), r.invoice.amount
    r.suggestions, r.matches = list(suggestions), []
    r.why_not_paid, r.note = why, note
    _warn(warnings, code, f"{r.invoice.id} ({r.invoice.client}): {why}")


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
            "reason": (("; ".join(m.reason for m in matches) if r.status != "needs_review" else r.why_not_paid)
                       + (f"; adds up with the {prev['amount']} you confirmed earlier for this invoice" if prev and r.status == "suggested_paid" else "")),
            "payments": [{"key": f"{m.source}:{m.payment_id}", "id": m.payment_id, "source": m.source,
                          "date": m.payment_date.isoformat(), "amount": str(m.amount), "currency": m.currency,
                          "payer": m.payer, "description": m.payment_description, "allocated": str(m.allocated),
                          "named": list(m.named)}
                         for m in matches],
        })
    return out


def _named_ids(p, index: IdIndex) -> set:
    named: set = set()
    for text in field_texts(p.description, p.payer):
        named |= index.find(text)
    return named


def _warn(warnings: list, code: str, message: str) -> None:
    warnings.append(Notice(message, code))


def _name_clash(subject: str, invoice: str, named) -> str | None:
    """The one rule for a payment whose text names known invoice ids: it names a DIFFERENT invoice when the named ids do not include
    `invoice`. None when it names nothing, or names this invoice."""
    if named and invoice.strip().lower() not in {n.strip().lower() for n in named}:
        return f"{subject} names {', '.join(named)}, not {invoice}"
    return None


def _verdict(row_invoice: str, row_key: str, p, named: list[str]) -> str:
    """May this current payment stand in for the decision's old one? The same checks as the repeat check: two different real bank
    transaction ids are two payments, and a payment that names a different invoice is not the one that was decided for this invoice."""
    old_id = row_key.split(":", 1)[-1]
    if not is_hash_id(p.id) and not is_hash_id(old_id) and p.id != old_id:
        return f"different bank transaction ids ({old_id} / {p.id}): two payments"
    return _name_clash("it", row_invoice, named) or "ok"


def _find_orphans(lpath: Path, payments: list, invoices: list, id_index: IdIndex, all_keys: set) -> list[dict]:
    """A decision (a confirmed or rejected payment, or a confirmed invoice) that points at something that is not in today's data, while a
    look-alike is. NOTHING is repaired automatically: that would be a new way to attach a decision to the wrong client. Each one is reported
    with its candidates and the exact commands (`rebind`, `forget`), and the invoices involved are held with no reminder until you decide."""
    present = all_keys  # every payment in today's exports, including the ones already consumed by a confirmation
    ledger_keys = {r["key"] for r in load_payment_rows(lpath)}
    rejected_rows = load_rejected_rows(lpath)
    for rows in rejected_rows.values():
        ledger_keys.update(rows)
    today_ids = {i.id.strip().lower() for i in invoices}
    out: list[dict] = []

    def candidates(row: dict, inv: str) -> list[dict]:
        found = []
        for p in payments:
            k = _pkey(p)
            if k in ledger_keys or not row_matches(p, row, 0):
                continue
            named = sorted(_named_ids(p, id_index))
            found.append({"key": k, "id": p.id, "source": p.source, "date": p.date.isoformat(), "amount": str(p.amount), "currency": p.currency,
                          "payer": p.payer, "named": named, "verdict": _verdict(inv, row["key"] if "key" in row else "", p, named)})
        return found

    decided = list(load_paid(lpath).items()) + list(load_received(lpath).items())
    snapshots = {inv.strip().lower(): (e.get("snapshot") or {}) for inv, e in decided}
    real_ids = {inv.strip().lower(): inv for inv, _ in decided}
    for row in load_payment_rows(lpath):
        if row["key"] in present:
            continue
        cands = candidates(row, row["invoice"])
        if cands:
            hold = {row["invoice"]} | {n for c in cands for n in c["named"]}
            out.append({"kind": "payment", "decision": "confirmed", "old": row["key"], "invoice": row["invoice"], "row": {k: v for k, v in row.items() if k != "invoice"},
                        "snapshot": snapshots.get(row["invoice"].strip().lower(), {}), "candidates": cands, "hold": sorted(hold)})
    for inv, rows in rejected_rows.items():
        for key, row in rows.items():
            if key in present:
                continue
            cands = candidates({**row, "key": key}, inv)
            if cands:
                hold = {inv} | {n for c in cands for n in c["named"]}
                out.append({"kind": "payment", "decision": "rejected", "old": key, "invoice": inv, "row": row, "snapshot": {},
                            "candidates": cands, "hold": sorted(hold)})
    for inv_lower, snap in snapshots.items():  # a confirmed invoice whose id is gone: every current invoice that may be it renamed or edited
        inv_id = real_ids[inv_lower]
        if not snap or inv_lower in today_ids:
            continue
        for i in invoices:
            why = renamed_match(snap, inv_id, i)
            if why:
                out.append({"kind": "invoice", "decision": "confirmed", "old": inv_id, "new": i.id, "invoice": i.id, "row": {}, "snapshot": snap,
                            "candidates": [], "hold": [i.id], "why": why})
    for o in out:  # a payment that may be the confirmed one must never be suggested for anything meanwhile
        for c in o["candidates"]:
            for p in payments:
                if _pkey(p) == c["key"]:
                    p.suspect = p.suspect or f"possible look-alike of a payment in an orphaned decision for {o['invoice']}"
    return out


def _orphan_text(o: dict) -> str:
    snap = o["snapshot"]
    who = (f"the invoice was {snap.get('client', '?')}, {snap.get('amount', '?')} {snap.get('currency', '')}, due {snap.get('due', '?')}"
           if snap else "no invoice snapshot is stored")
    holds = ", ".join(o["hold"])
    if o["kind"] == "invoice":
        return (f"ORPHANED_DECISION: you confirmed invoice {o['old']} ({who}), but there is no invoice {o['old']} in invoices.csv any more, while "
                f"{o['new']} may be it renamed or edited ({'; '.join(o.get('why', []))}). Nothing is moved automatically. If it is the same "
                f"invoice: `invoice-chaser rebind {quote(o['old'])} {quote(o['new'])}`; if not: `invoice-chaser forget {quote(o['old'])}`. "
                f"Until you decide, {holds} is held in needs review with no reminder.")
    row = o["row"]
    cands = "; ".join(f"{c['key']} ({c['date']}, {c['amount']} {c['currency']}, payer {c['payer'] or 'none'}; names "
                      f"{', '.join(c['named']) or 'no invoice'}; check: {c['verdict']})" for c in o["candidates"])
    cmds = []
    for c in o["candidates"]:
        if c["verdict"] == "ok":
            cmds.append(f"`invoice-chaser rebind {quote(o['old'])} {quote(c['key'])}`")
    cmd = (" or ".join(cmds) + " if it is the same payment; ") if cmds else "(no candidate passes the safety check, so there is nothing to rebind to) "
    return (f"ORPHANED_DECISION: you {o['decision']} payment {o['old']} for {o['invoice']} ({who}; the payment was {row.get('date')}, "
            f"{row.get('amount')} {row.get('currency')}, payer {row.get('payer') or 'none'}), but that payment id is not in today's exports. "
            f"Similar current payment(s): {cands}. Nothing is re-bound automatically. Resolve it: {cmd}or `invoice-chaser forget {quote(o['old'])}`. "
            f"Until you decide, {holds} {'is' if len(o['hold']) == 1 else 'are'} held in needs review with no reminder.")


def _print_warnings(warnings: list, out: Path) -> None:
    """One summary at the end. Critical and warning notices are printed in full; routine ones are counted. Which is which is decided by the
    code each notice was raised with, never by its text (the text can carry payer names that anyone could word to look routine)."""
    if not warnings:
        return
    notices = out / "notices.json"
    seen_notices: dict = {}
    try:
        seen_notices = json.loads(notices.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    shown, counts = [], {"critical": {}, "warning": {}, "routine": {}}
    for w in warnings:
        code, sev = getattr(w, "code", "NOTE"), getattr(w, "severity", "warning")
        counts[sev][code] = counts[sev].get(code, 0) + 1
        if sev != "routine":
            shown.append((sev, code, str(w)))
    for sev, code, text in shown:
        print(f"  {'CRITICAL' if sev == 'critical' else 'WARNING'} [{code}]: {text}", file=sys.stderr)
    routine = counts["routine"]
    if routine:
        print(f"  NOTICES: {sum(routine.values())} routine ({', '.join(f'{c} x{n}' for c, n in routine.items())}); details in review.html", file=sys.stderr)

    def part(sev):
        d = counts[sev]
        return f"{sum(d.values())} {sev}" + (f" ({', '.join(f'{c} x{n}' for c, n in d.items())})" if d else "")
    print(f"  Summary: {part('critical')}, {part('warning')}, {part('routine')}; everything is listed in review.html", file=sys.stderr)
    try:
        atomic_write_text(notices, json.dumps(seen_notices))
    except OSError:
        pass


def _age_and_stick(results, out: Path, cfg, today: date, warnings: list[str]) -> None:
    """Remember when each invoice first started waiting for your decision (out/seen.json, keyed by INVOICE, not by
    payment, so a changed payment id or a flip between "suggested" and "needs review" does not restart the clock).
    The earliest date wins and is never later than the real date on this computer, so running with a future --today
    cannot poison it. Waiting longer than stuck_after_days, or being more than stuck_overdue_days overdue with a
    suggestion still waiting, makes it STUCK: a loud banner, a CLI line and an internal note to you."""
    real_today = date.today()
    seen_file = out / "seen.json"
    history: dict = {}
    if seen_file.exists():
        try:
            history = json.loads(seen_file.read_text(encoding="utf-8-sig"))
            if not isinstance(history, dict) or history.get("schema") != SEEN_SCHEMA or not isinstance(history.get("invoices"), dict):
                raise ValueError("no schema, or a different one")
            history = history["invoices"]
        except (OSError, ValueError):
            backup = seen_file.with_name(f"seen.json.corrupt-{real_today.isoformat()}")
            try:
                seen_file.replace(backup)
            except OSError:
                pass
            history = {}
            _warn(warnings, "STATE_UNREADABLE", f"seen.json could not be read (damaged, or from a pre-release build), so I do not know how long invoices have been waiting. The old file was kept as "
                            f"{backup.name}. Waiting times restart today: check the oldest suggestions and reviews yourself.")
    now: dict[str, dict] = {}
    for r in results:
        if r.status not in ("suggested_paid", "partial_suggested", "needs_review"):
            continue
        key = r.invoice.id.strip().lower()
        first = min(real_today, today)
        entry = history.get(key)
        if isinstance(entry, dict):
            try:
                first = min(first, date.fromisoformat(entry["first_seen"]))
            except (KeyError, ValueError, TypeError):
                pass
        now[key] = {"first_seen": first.isoformat()}
        r.waiting_days = max((today - first).days, 0)
        r.stuck = r.waiting_days > cfg.stuck_after_days or r.days_overdue > cfg.stuck_overdue_days
    out.mkdir(parents=True, exist_ok=True)
    atomic_write_text(seen_file, json.dumps({"schema": SEEN_SCHEMA, "invoices": now}, indent=2, sort_keys=True))


def _resolve_commands(r, orphans: list[dict], paid_marks: dict, received_marks: dict, flag: str) -> list[str]:
    """The exact commands that resolve a held invoice, for the OVERDUE_HELD line (1.1.13)."""
    qid = quote(r.invoice.id)
    key = r.invoice.id.strip().lower()
    cmds: list[str] = []
    if r.status != "needs_review":  # a suggested payment: confirm it, or say it is not this invoice's
        cmds.append(f"invoice-chaser confirm {qid}{flag}")
        cmds += [f"invoice-chaser reject {qid} {quote(m.payment_id)}{flag}" for m in r.matches]
    else:  # a payment to check: you checked it yourself, or it is not this invoice's
        cmds.append(f"invoice-chaser mark-paid {qid}{flag}")
        cmds += [f"invoice-chaser reject {qid} {quote(m.payment_id)}{flag}" for m in r.suggestions]
    entry = paid_marks.get(key) or received_marks.get(key) or {}
    if "PAYMENT_DOUBLE_BOUND" in (r.note or ""):  # your confirmation names a payment that also backs another invoice
        cmds += [f"invoice-chaser unbind {qid} {quote(k)}{flag}" for k in entry.get("payments", [])]
    for o in orphans:
        if key in {h.strip().lower() for h in o["hold"]}:
            if o["kind"] == "invoice":
                cmds.append(f"invoice-chaser rebind {quote(o['old'])} {quote(o['new'])}{flag}")
            else:
                cmds += [f"invoice-chaser rebind {quote(o['old'])} {quote(c['key'])}{flag}" for c in o["candidates"] if c["verdict"] == "ok"]
            cmds.append(f"invoice-chaser forget {quote(o['old'])}{flag}")
    return list(dict.fromkeys(cmds))


def _build_holds(results, orphans: list[dict], double_held: dict, invoices, paid_marks: dict, received_marks: dict, evidence: dict):
    """Every reason an invoice waits for you, as Hold records, plus the confirmations the gate reads. Built once per run, after every
    status is final. client_draft_allowed decides from these; nothing else decides whether a client may be written to."""
    holds: list[Hold] = []
    for o in orphans:
        holds.append(Hold("orphan", "", frozenset(h.strip().lower() for h in o["hold"]), _orphan_text(o)))
    for inv, reason in double_held.items():
        holds.append(Hold("double_bound", inv, frozenset({inv}),
                          "a payment backs two invoices (PAYMENT_DOUBLE_BOUND): the money may have arrived"))
    for r in results:
        key = r.invoice.id.strip().lower()
        if r.status in HELD:
            holds.append(Hold("review", key, frozenset({key}), r.why_not_paid or r.note))
        cands = evidence.get(key, [])  # before any decision cleared it: an orphan hold must not hide a possible double count
        if any(DOUBLE_COUNT.search(m.suspect or "") for m in cands):
            holds.append(Hold("double_count", key, frozenset({key}),
                              "a possible double count of another payment: the money may have arrived"))
        if any(REFUND_NAME in (m.blocker or "") for m in cands):
            holds.append(Hold("refund_name", key, frozenset({key}),
                              "its client name contains a refund word, so the payment may be a refund: the money may have arrived"))
    invoice_ids = {i.id.strip().lower() for i in invoices}
    gate_ledger: dict[str, dict] = {}
    for kind, marks in (("paid", paid_marks), ("received", received_marks)):
        for key, e in marks.items():
            gate_ledger.setdefault(key, {"kind": kind, "date": e.get("date", ""), "snapshot": e.get("snapshot") or {},
                                         "gone": key not in invoice_ids})
    return holds, gate_ledger


def _possible_payment(r, orphans: list[dict]) -> str:
    """'<payment id>, <date>, <amount>, payer <payer>' for the payment that may have arrived."""
    cands = r.matches or r.suggestions
    if cands:
        return "; ".join(f"{m.payment_id}, {m.payment_date.isoformat()}, {m.amount:,.2f} {m.currency}, payer {m.payer or '(none)'}" for m in cands)
    found = [c for o in orphans if r.invoice.id.lower() in {h.lower() for h in o["hold"]} for c in o["candidates"]]
    if found:
        return "; ".join(f"{c['id']}, {c['date']}, {c['amount']} {c['currency']}, payer {c['payer'] or '(none)'}" for c in found)
    return "a payment you decided about, see review.html"


def _check_first_item(r, cfg, args, templates_dir, llm_keys: bool, entries, orphans: list[dict], warnings: list, holds, gate_ledger) -> dict | None:
    """The normal client reminder for the invoice's stage, marked CHECK FIRST. Same gate and send-blockers as an ordinary draft."""
    stage = stage_for(r.days_overdue, cfg.stage_days)
    if stage is None:
        r.draft_status = "no check-first draft: not yet due for a reminder"
        return None
    return _client_item(r, stage, cfg, args, templates_dir, llm_keys, entries, orphans, warnings, holds, gate_ledger, check_first=True)


def _client_item(r, stage, cfg, args, templates_dir, llm_keys: bool, entries, orphans: list[dict], warnings: list, holds, gate_ledger,
                 check_first: bool) -> dict | None:
    """THE one place a client-addressed draft is built (ordinary and CHECK FIRST). The gate runs first, every time."""
    inv = r.invoice
    allowed, reason = client_draft_allowed(inv, gate_ledger, holds)
    if not allowed:
        r.draft_status = f"no client draft: {reason}"
        return None
    if not inv.email or not valid_email(inv.email):
        problem = "missing email" if not inv.email else "invalid email"
        r.draft_status = problem
        if check_first:
            _warn(warnings, "EMAIL_MISSING", f"{inv.id} ({inv.client}): {problem}, no check-first draft written")
        else:
            _warn(warnings, "EMAIL_MISSING", f"{inv.id} ({inv.client}): {problem}, no draft written for the {stage} reminder")
        return None
    hs = highest_sent(entries)
    if hs and hs[0] >= STAGES.index(stage):
        if check_first:
            r.draft_status = f"no check-first draft: {hs[1]} already sent {hs[2]}"
        else:
            nxt = STAGES.index(stage) + 1
            when = f"; next stage {STAGES[nxt]} in {cfg.stage_days[nxt] - r.days_overdue} day(s)" if nxt < len(STAGES) else ""
            r.draft_status = f"no draft: {hs[1]} already sent {hs[2]}{when}"
        return None
    line = ""
    if args.llm and llm_keys:
        v = variables_for(r, cfg.sender_name, cfg.payment_details)
        line = personal_line(v["first_name"], v["outstanding"], r.days_overdue, inv.id) or ""
    v = variables_for(r, cfg.sender_name, cfg.payment_details, line, reply_days=cfg.reply_within_days, last_reminder=r.last_reminder)
    found = r.matches or r.suggestions
    if check_first and r.status == "partial_suggested" and found:  # 1.1.14: the part-payment found is named, and still waits for you
        amounts = [money(m.amount, m.currency) for m in found]
        said = (f"a payment of {amounts[0]} was found and is waiting" if len(amounts) == 1 else
                f"payments of {', '.join(amounts[:-1])} and {amounts[-1]} were found and are waiting")
        v["outstanding"] = f"{v['outstanding']} ({said} for your check)"
    flags = flags_for(stage, cfg.previous_reminders, r.last_reminder, cfg.pause_work_on_final)
    subject, body = render(load_template(stage, cfg.tone, templates_dir), v, flags)
    base = {"invoice_id": inv.id, "stage": stage, "to": inv.email, "from_name": cfg.sender_name, "from_email": cfg.sender_email}
    if not check_first:
        r.draft_status = "drafted"
        return {**base, "subject": subject, "body": body}
    head = f"CHECK FIRST: a possible payment was found ({_possible_payment(r, orphans)}). Verify it before sending this."
    r.draft_status = f"CHECK FIRST reminder drafted in {CHECK_FIRST_DIR}/ (verify the possible payment first)"
    return {**base, "subject": "[CHECK FIRST] " + subject, "body": head + "\n\n" + body, "check_first": True}


def _check_item(r, cfg, out_hint: str) -> dict:
    inv = r.invoice
    qid = quote(inv.id)
    flag = "" if out_hint == "out" else f" --out {quote(out_hint)}"
    if r.confirmed_on:  # you already confirmed it: say so, and offer only the two ways to resolve it
        head = f"You confirmed this paid on {r.confirmed_on}, but the invoice changed: re-confirm or unmark."
        lines = [f"Invoice {inv.id} ({inv.client}, {inv.amount:,.2f} {inv.currency}, due {inv.due.isoformat()}) is {r.days_overdue} days overdue. "
                 "No reminder goes to the client while it is held.", "", head, f"Why: {r.why_not_paid}", "",
                 "Decide with one of:",
                 f"  invoice-chaser unmark-paid {qid}{flag}   (it is not paid: run again for a fresh suggestion)",
                 f"  invoice-chaser mark-paid {qid}{flag}     (you re-checked it against the changed invoice: it is paid)",
                 "", "This note is for you. It was not sent to the client."]
        return {"invoice_id": inv.id, "stage": "check", "internal": True, "to": cfg.sender_email or "set-sender-email@example.invalid",
                "from_name": "invoice-chaser", "from_email": cfg.sender_email,
                "subject": f"Check {inv.id}: confirmed paid on {r.confirmed_on}, invoice changed, {r.days_overdue} days overdue",
                "body": "\n".join(lines) + "\n"}
    waited = "first seen today" if r.waiting_days == 0 else f"waiting for your decision for {r.waiting_days} day(s)"
    lines = [f"Invoice {inv.id} ({inv.client}, {inv.amount:,.2f} {inv.currency}, due {inv.due.isoformat()}) is {r.days_overdue} days overdue "
             f"and its payment is {waited}. No reminder goes to the client while it waits.", "",
             f"Status: {r.status}", f"Why: {r.why_not_paid}", ""]
    for m in (r.matches if r.status != "needs_review" else r.suggestions):
        lines.append(f"Payment {m.payment_id} ({m.source}), {m.payment_date.isoformat()}, {m.amount:,.2f} {m.currency}, payer: {m.payer or '(none)'}")
        lines.append(f"  text: {m.payment_description}")
    lines += ["", "Decide with one of:",
              f"  invoice-chaser confirm {qid}{flag}      (the payment is right)" if r.status != "needs_review" else
              f"  invoice-chaser mark-paid {qid}{flag}    (you checked: it is paid)",
              f"  invoice-chaser reject {qid} <payment id>{flag}   (that payment is not this invoice's: the client gets chased)",
              "", "This note is for you. It was not sent to the client."]
    return {"invoice_id": inv.id, "stage": "check", "internal": True, "to": cfg.sender_email or "set-sender-email@example.invalid",
            "from_name": "invoice-chaser", "from_email": cfg.sender_email,
            "subject": f"Check {inv.id}: " + ("first seen today" if r.waiting_days == 0 else f"waiting for your decision ({r.waiting_days} days)")
                       + f", {r.days_overdue} days overdue",
            "body": "\n".join(lines) + "\n"}


# ---------------------------------------------------------------- commands that record YOUR decisions

def _status_ids(out: Path):
    status = out / "status.csv"
    if not status.exists():
        return None
    with open(status, newline="", encoding="utf-8") as fh:
        return {canon_id(row["invoice_id"]).lower(): canon_id(row["invoice_id"]) for row in csv.DictReader(fh)}


def _refuse_duplicate_ids(out: Path, wanted: str) -> None:
    status = out / "status.csv"
    if not status.exists():
        return
    with open(status, newline="", encoding="utf-8") as fh:
        ids = [row["invoice_id"] for row in csv.DictReader(fh)]
    clash = sorted({canon_id(i) for i in ids if canon_id(i).casefold() == canon_id(wanted).casefold()})
    if len(clash) > 1:
        raise ValueError(f"invoice ids are not unique ({' / '.join(clash)}): fix invoices.csv first, then run again")


def _resolve_invoice(args) -> tuple[str, date]:
    """Validate the id and date shared by the commands that record a decision.

    Ids match ignoring case and spaces, against out/status.csv. An unknown id, or a missing
    status.csv, is refused unless --force. Dates in the future are refused."""
    out = Path(args.out)
    wanted = canon_id(args.invoice)  # 1.1.15: "INV–1103" is typed as "INV-1103"
    _refuse_duplicate_ids(out, wanted)
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


def _load_suggestions(out: Path) -> tuple[list[dict], dict]:
    f = out / "suggestions.json"
    if not f.exists():
        raise ValueError(f"{f.name} not found in {out}: run `invoice-chaser run` first")
    try:
        meta = json.loads(f.read_text(encoding="utf-8-sig"))
        return meta["suggestions"], meta
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError(f"{f.name} is not readable ({e}); run `invoice-chaser run` again") from e


def _local_state_fallback(meta: dict) -> dict:
    raise ValueError("local-state.json not found next to the ledger: run `invoice-chaser run` again, then confirm")


def _check_still_valid(entries: list[dict], meta: dict, lpath: Path, real_today: date) -> None:
    """A suggestion is only a suggestion about the exports it was made from. Refuse to confirm it if the day, the
    exports or the ledger's view of that invoice (a reject, a confirm, a mark-paid) changed since the last run."""
    generated = meta.get("generated_on")
    if generated != real_today.isoformat():
        raise ValueError(f"the suggestions are from {generated or 'an unknown day'}, not today ({real_today.isoformat()}): "
                         f"run `invoice-chaser run` again, look at the result, then confirm")
    try:
        local = json.loads((lpath.parent / "local-state.json").read_text(encoding="utf-8-sig")) if (lpath.parent / "local-state.json").exists() else None
    except ValueError:
        local = None
    if local is None:
        local = _local_state_fallback(meta)
    for f in local.get("inputs", []):
        try:
            changed = _file_hash(f["path"]) != f["sha256"]
        except OSError:
            raise ValueError(f"{f['name']} is gone since the last run: run `invoice-chaser run` again") from None
        if changed:
            raise ValueError(f"{f['name']} changed since the last run: run `invoice-chaser run` again, then confirm")
    paid, received, rejected = ci(load_paid(lpath)), ci(load_received(lpath)), ci(load_rejected(lpath))
    for e in entries:
        if e.get("ledger_state") and e["ledger_state"] != _ledger_state(paid, received, rejected, e["invoice_id"]):
            raise ValueError(f"your ledger changed for {e['invoice_id']} since the last run (a reject, confirm or mark-paid): "
                             f"run `invoice-chaser run` again, then confirm")


def _repeat_problems(entry: dict, rows: list[dict]) -> list[str]:
    msgs = []
    for p in entry["payments"]:
        for r in rows:
            if r["key"] == p["key"] and not row_matches(p, r, 0):
                msgs.append(f"{entry['invoice_id']}: payment {p['id']} has the same id as a payment you confirmed for {r['invoice']}, but a different "
                            f"date, amount or payer. It is held to be safe. Next step: compare both on your statement; if this one is really "
                            f"paid, record the invoice yourself with `invoice-chaser mark-paid {quote(entry['invoice_id'])}`.")
        hit = repeat_of(p, [r for r in rows if r["key"] != p["key"]], p.get("named", ()))
        if hit:
            msgs.append(f"{entry['invoice_id']}: payment {p['id']} looks like a repeat of a payment you already confirmed for {hit['invoice']} "
                        f"(same date, amount and payer). It is held to be safe: counting one payment twice could mark an invoice paid that is "
                        f"not. If you have checked and it really is a separate payment, record the invoice yourself with "
                        f"`invoice-chaser mark-paid {quote(entry['invoice_id'])}`.")
    return msgs


def _repeat_guard(entry: dict, lpath: Path) -> None:
    """A payment that looks like one you already confirmed is never confirmed again."""
    problems = _repeat_problems(entry, load_payment_rows(lpath))
    if problems:
        raise ValueError(problems[0])


def _confirm_op(entry: dict, when: date, rows_all: list[dict]) -> tuple[dict, str]:
    previous_rows = [{k: v for k, v in r.items() if k != "invoice"} for r in rows_all
                     if r["invoice"].strip().lower() == entry["invoice_id"].strip().lower()]
    rows = previous_rows + [payment_row(p) for p in entry["payments"]]
    keys = list(entry["previous_payments"]) + [p["key"] for p in entry["payments"]]
    got = Decimal(entry["previous_received"]) + sum((Decimal(p["allocated"]) for p in entry["payments"]), Decimal(0))
    amount = Decimal(entry["amount"])
    weak = {}
    if entry.get("suggested_kind") == "review":  # 1.1.19: a held candidate, confirmed because you named its payment
        weak = {"confirmed_weak": True, "reason": entry["reason"], "score": entry["confidence"]}
    if entry["kind"] == "paid" or got >= amount - Decimal(entry["tolerance"]):
        op = {"invoice_id": entry["invoice_id"], "kind": "paid", "when": when, "snapshot": entry["snapshot"], "keys": keys, "rows": rows,
              "source": "confirm", **weak}
        return op, (f"{entry['invoice_id']}: paid ({entry['currency']} {got:,.2f} of {amount:,.2f}, "
                    f"payments {', '.join(p['id'] for p in entry['payments'])})")
    op = {"invoice_id": entry["invoice_id"], "kind": "received", "when": when, "snapshot": entry["snapshot"], "keys": keys, "rows": rows,
          "amount": str(got), **weak}
    return op, (f"{entry['invoice_id']}: part-payment {entry['currency']} {got:,.2f} of {amount:,.2f} recorded; "
                f"{amount - got:,.2f} stays outstanding and will be chased")


def _refuse_bound_keys(entries: list[dict], data: dict) -> None:
    """I1: a payment you confirm must not already back an invoice (the same one or another): a payment backs one invoice only."""
    for e in entries:
        for p in e["payments"]:
            owners = bound_to(data, p["key"])
            if owners:
                raise ValueError(f"refused: payment {p['key']} already backs {', '.join(owners)} in your ledger, and a payment can back one "
                                 f"invoice only. Nothing was recorded. If that link is wrong: `invoice-chaser unbind {quote(owners[0])} {quote(p['key'])}`")


def _confirm_all(entries: list[dict], when: date, lpath: Path) -> tuple[list[str], bool]:
    """All or nothing: every check for every invoice runs first and a single problem stops the whole batch with the
    full list; only then is the ledger written, once, atomically. Returns (summaries, changed)."""
    _refuse_bound_keys(entries, read_ledger(lpath))
    rows = load_payment_rows(lpath)
    problems = [m for e in entries for m in _repeat_problems(e, rows)]
    if problems:
        raise ValueError("nothing was recorded, because " + ("one confirmation is" if len(problems) == 1 else f"{len(problems)} confirmations are")
                         + " held back:\n  " + "\n  ".join(problems))
    built = [_confirm_op(e, when, rows) for e in entries]
    changed = apply_confirmations(lpath, [op for op, _ in built])
    return [summary for _, summary in built], changed


def _entry_with_payments(entries: list[dict], invoice: str, wanted: list[str]) -> dict:
    """`confirm INV --payment ID`: you name the payment(s). Works on any suggestion, also a held one (a possible same payment in two
    exports): only the payments you name are counted, and never more than what the invoice still needs."""
    want = invoice.strip().lower()
    entry = next((e for e in entries if e["invoice_id"].strip().lower() == want), None)
    if entry is None:
        raise ValueError(f"no suggestion for {invoice} in the last run (use `invoice-chaser mark-paid` if you checked yourself)")
    by_name: dict[str, list] = {}
    for p in entry["payments"]:
        for name in (p["id"].lower(), p["key"].lower()):
            if p not in by_name.setdefault(name, []):
                by_name[name].append(p)
    chosen = []
    for w in wanted:
        hits = by_name.get(w.strip().lower(), [])
        if len(hits) > 1:
            raise ValueError(f"{w!r} matches {len(hits)} payments from different exports: say which, with the source in front: "
                             f"{', '.join(p['key'] for p in hits)}")
        p = hits[0] if hits else None
        if p is None:
            raise ValueError(f"{w!r} is not a payment suggested for {entry['invoice_id']} in the last run "
                             f"(known: {', '.join(sorted(p['key'] for p in entry['payments']))})")
        if p["currency"] != entry["currency"]:
            raise ValueError(f"payment {p['id']} is in {p['currency']}, the invoice in {entry['currency']}: use `invoice-chaser mark-paid` once you have checked")
        if p not in chosen:
            chosen.append(p)
    amount, prev = Decimal(entry["amount"]), Decimal(entry["previous_received"])
    remaining, picked = amount - prev, []
    for p in chosen:
        alloc = min(Decimal(p["amount"]), remaining)
        remaining -= alloc
        picked.append({**p, "allocated": str(alloc)})
    total = prev + sum((Decimal(p["allocated"]) for p in picked), Decimal(0))
    kind = "paid" if total >= amount - Decimal(entry["tolerance"]) else "partial"
    return {**entry, "kind": kind, "suggested_kind": entry["kind"], "payments": picked,
            "left_over": [p for p in entry["payments"] if p not in chosen]}


def confirm(args) -> int:
    out = Path(args.out)
    lpath = _ledger_path(args)
    entries, meta = _load_suggestions(out)
    real_today = date.fromisoformat(args.today) if getattr(args, "today", None) else date.today()
    when = date.fromisoformat(args.date) if args.date else real_today
    if when > real_today:
        raise ValueError(f"--date {when} is in the future")
    if args.all_strong and getattr(args, "payment", None):
        raise ValueError("--payment names payments for ONE invoice: it cannot be combined with --all-strong")
    if args.all_strong:
        todo = [e for e in entries if e["kind"] == "paid"]
        if not todo:
            print("nothing to confirm: no strong suggested-paid invoices in the last run")
            return 0
        _check_still_valid(todo, meta, lpath, real_today)
        problems = [m for e in todo for m in _repeat_problems(e, load_payment_rows(lpath))]
        if problems:  # before anything is shown or asked: the whole batch is refused with the full list
            raise ValueError("nothing was recorded, because " + ("one confirmation is" if len(problems) == 1 else f"{len(problems)} confirmations are")
                             + " held back:\n  " + "\n  ".join(problems))
        _refuse_bound_keys(todo, read_ledger(lpath))
        print("These invoices will be recorded as PAID in the ledger. Read the payer and the text of each payment:")
        for e in todo:
            print(f"  {e['invoice_id']}  {visible(e['client'])}  {e['currency']} {Decimal(e['amount']):,.2f}")
            for p in e["payments"]:
                print(f"      payment {p['id']} ({p['source']}) {p['date']}  {Decimal(p['amount']):,.2f} {p['currency']}")
                print(f"        payer: {visible(p['payer']) or '(none)'}")
                print(f"        text:  {visible(p['description']) or '(none)'}")
        if not args.yes:
            try:
                answer = input(f"Confirm {len(todo)} invoice(s) as paid? [y/N] ").strip().lower()
            except EOFError:
                print()
                answer = ""
            if answer not in ("y", "yes"):
                print("nothing recorded")
                return 1
        summaries, changed = _confirm_all(todo, when, lpath)
        if not changed:
            print("nothing changed: these confirmations are already in the ledger")
            return 1
        for line in summaries:
            print("  " + line)
        print(f"recorded {len(todo)} confirmation(s) in one step -> {lpath.name}\n  re-run `invoice-chaser run` to update the report")
        return 0
    if not args.invoice:
        raise ValueError("give an invoice id, or use --all-strong")
    _refuse_duplicate_ids(out, args.invoice.strip())
    if getattr(args, "payment", None):
        entry = _entry_with_payments(entries, args.invoice, args.payment)
        for p in entry["payments"]:  # a payment whose text names a different known invoice: refused unless --force, then warned
            clash = _name_clash(p["id"], entry["invoice_id"], p.get("named", ()))
            if clash and not args.force:
                raise ValueError(f"{clash}: pass --force if you're sure")
            if clash:
                print(f"WARNING: {clash}: recorded because of --force", file=sys.stderr)
        _check_still_valid([entry], meta, lpath, real_today)
        if entry["suggested_kind"] == "review":  # 1.1.19: say what was held, before anything is recorded
            print(f"weak match (score {entry['confidence']:.2f}, held: {entry['reason']}). "
                  f"Recording because you named this payment explicitly.")
        summaries, changed = _confirm_all([entry], when, lpath)
        if not changed:
            print(f"nothing changed: {entry['invoice_id']} already has this confirmation in the ledger")
            return 1
        print(summaries[0])
        for q in entry.get("left_over", []):  # the other half of a pair: not counted, and not left to chance
            iid = quote(entry["invoice_id"])
            print(f"  held: payment {q['key']} ({q['date']}, {Decimal(q['amount']):,.2f} {q['currency']}, payer {q['payer'] or 'none'}) was NOT counted. "
                  f"If it is the same payment you just confirmed: `invoice-chaser reject {iid} {quote(q['key'])}`. "
                  f"If it is a second real payment: `invoice-chaser confirm {iid} --payment {quote(q['key'])}`.")
        print(f"recorded -> {lpath.name}\n  re-run `invoice-chaser run` to update the report")
        return 0
    want = args.invoice.strip().lower()
    entry = next((e for e in entries if e["invoice_id"].strip().lower() == want and e["kind"] in ("paid", "partial")), None)
    if entry is None:
        weak = any(e["invoice_id"].strip().lower() == want for e in entries)
        raise ValueError(f"no strong suggestion for {args.invoice} in the last run"
                         + (" (it only has weak candidates: name the payment you checked with `invoice-chaser confirm "
                            f"{quote(args.invoice)} --payment <id>`, or use `invoice-chaser mark-paid`)" if weak
                            else " (use `invoice-chaser mark-paid` if you checked yourself)"))
    _check_still_valid([entry], meta, lpath, real_today)
    summaries, changed = _confirm_all([entry], when, lpath)
    if not changed:
        print(f"nothing changed: {entry['invoice_id']} already has this confirmation in the ledger")
        return 1
    print(summaries[0])
    print(f"recorded -> {lpath.name}\n  re-run `invoice-chaser run` to update the report")
    return 0


def reject(args) -> int:
    out = Path(args.out)
    invoice_id, _ = _resolve_invoice(argparse.Namespace(invoice=args.invoice, out=args.out, force=args.force, today=None, date=None))
    entries, _ = _load_suggestions(out)
    want = args.payment.strip()
    keys: dict[str, set] = {}
    rows = {}
    for e in entries:
        if e["invoice_id"].lower() == invoice_id.lower():
            for p in e["payments"]:
                keys.setdefault(p["id"].lower(), set()).add(p["key"])
                keys.setdefault(p["key"].lower(), set()).add(p["key"])
                rows[p["key"]] = {k: v for k, v in payment_row(p).items() if k != "key"}
    hits = sorted(keys.get(want.lower(), set()))
    if len(hits) > 1:
        raise ValueError(f"{want!r} matches {len(hits)} payments from different exports: say which, with the source in front: {', '.join(hits)}")
    key = hits[0] if hits else None
    if key is None:
        if ":" in want and args.force:
            key = want
        else:
            raise ValueError(f"{want!r} is not a payment suggested for {invoice_id} in the last run "
                             f"(known: {', '.join(sorted({k for ks in keys.values() for k in ks})) or 'none'})")
    if not record_rejected(_ledger_path(args), invoice_id, key, rows.get(key)):
        print(f"nothing changed: {key} was already rejected for {invoice_id}")
        return 1
    print(f"rejected: {key} will never be suggested for {invoice_id} again\n  re-run `invoice-chaser run` to update the report")
    return 0


def mark_sent(args) -> int:
    out = Path(args.out)
    invoice_id, when = _resolve_invoice(args)
    stage = args.stage
    try:
        drafted = json.loads((out / "drafts.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        drafted = []
    mine = [d for d in drafted if str(d.get("invoice_id", "")).lower() == invoice_id.lower() and not d.get("internal")]
    if not stage:
        stage = next((d["stage"] for d in mine), None)
        if not stage:
            raise ValueError(f"no --stage given and no draft for {invoice_id} in {out / 'drafts.json'}")
    if stage not in STAGES:
        raise ValueError(f"--stage must be one of {', '.join(STAGES)}")
    if any(d.get("check_first") and d.get("stage") == stage for d in mine) and not getattr(args, "checked", False):
        raise ValueError(f"the {stage} reminder for {invoice_id} is a CHECK FIRST draft (a possible payment was found). Verify that payment, "
                         f"then run mark-sent again with --checked")
    path = _ledger_path(args)
    if not record_sent(path, invoice_id, stage, when):
        print(f"nothing changed: the {stage} reminder for {invoice_id} on {when} is already recorded")
        return 1
    print(f"recorded: {invoice_id} {stage} reminder sent {when} -> {path}")
    return 0


def _same_payment_backers(path: Path, invoice_id: str, snapshot: dict) -> list[tuple[str, dict]]:
    """A confirmed payment with this invoice's client and amount that already backs ANOTHER invoice: (other invoice, payment row)."""
    client = str(snapshot.get("client", "")).strip().casefold()
    if not client or not snapshot.get("amount"):
        return []
    me = invoice_id.strip().lower()
    snaps: dict[str, dict] = {}
    for marks in (load_paid(path), load_received(path)):
        for inv, e in marks.items():
            snaps.setdefault(inv.strip().lower(), e.get("snapshot") or {})
    out = []
    for row in load_payment_rows(path):
        other = row["invoice"].strip().lower()
        s = snaps.get(other, {})
        if other == me or str(s.get("client", "")).strip().casefold() != client:
            continue
        if same_money(row.get("amount"), snapshot["amount"]) and str(row.get("currency", "")) == str(snapshot.get("currency", "")):
            out.append((row["invoice"], row))
    return out


def mark_paid(args) -> int:
    """You checked yourself (no suggested payment needed). Stores a snapshot of the invoice as in status.csv."""
    invoice_id, when = _resolve_invoice(args)
    path = _ledger_path(args)
    existing = ci(load_paid(path)).get(invoice_id.strip().lower())
    if existing:
        print(f"nothing changed: {invoice_id} is already marked paid (on {existing['date']}). "
              f"Run `invoice-chaser unmark-paid {invoice_id}` first to change it.")
        return 1
    snapshot = {}
    status = Path(args.out) / "status.csv"
    if status.exists():
        with open(status, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row["invoice_id"].lower() == invoice_id.lower():
                    snapshot = {"client": row["client"], "amount": row["amount"], "currency": row["currency"], "due": row["due"]}
    backers = _same_payment_backers(path, invoice_id, snapshot)
    if backers and not args.force:  # 1.1.13: refused, not just warned; --force says it is a second payment
        other = backers[0][0]
        print(f"REFUSED: {invoice_id} not marked paid: a payment of this client and amount already backs {other}; "
              f"pass --force if this is a second payment", file=sys.stderr)
        return 2
    for other, row in backers:
        print(f"WARNING: payment {row.get('key')} ({row.get('amount')} {row.get('currency')}, {row.get('date')}, payer {row.get('payer') or 'none'}) "
              f"already backs {other}, for the same client and amount as {invoice_id}. A payment backs one invoice. "
              f"(`invoice-chaser unmark-paid` undoes a mark-paid.)")
    if not record_paid(path, invoice_id, when, args.note or "", snapshot, (), source="mark-paid"):
        print(f"nothing changed: {invoice_id} is already marked paid")
        return 1
    print(f"recorded: {invoice_id} confirmed paid on {when} -> {path}")
    print("  re-run `invoice-chaser run` to update the report")
    return 0


def unmark_paid(args) -> int:
    path = _ledger_path(args)
    if not unrecord_paid(path, args.invoice):
        raise ValueError(f"nothing changed: {args.invoice} is not marked paid in {path}")
    print(f"removed: {args.invoice} is no longer marked paid -> {path}")
    print("  re-run `invoice-chaser run` to update the report")
    return 0


def unbind(args) -> int:
    """`invoice-chaser unbind INV PAYMENT`: the payment does not back this invoice. Its confirmation is removed (the invoice goes back
    to a fresh suggestion); the other invoice that the payment backs, if any, keeps its confirmation."""
    lpath = _ledger_path(args)
    data = read_ledger(lpath)
    want = args.invoice.strip().lower()
    entry = next((e for section in ("paid", "received") for k, e in data[section].items() if k.strip().lower() == want), None)
    bound = list(entry.get("payments", [])) if entry else []
    named = args.payment.strip().lower()
    hits = [k for k in bound if k.lower() == named or k.split(":", 1)[-1].lower() == named]
    if not hits:
        print(f"nothing changed: {args.invoice} is not confirmed with payment {args.payment} "
              f"(it is backed by: {', '.join(bound) or 'no payment'})")
        return 1
    if len(hits) > 1:
        raise ValueError(f"{args.payment!r} matches {len(hits)} payments for {args.invoice}: give the source in front: {', '.join(hits)}")
    if not unbind_payment(lpath, args.invoice, hits[0]):
        print(f"nothing changed: {args.invoice} is not confirmed with payment {hits[0]}")
        return 1
    print(f"unbound: payment {hits[0]} no longer backs {args.invoice}; its confirmation was removed, so it goes back to a fresh suggestion\n"
          f"  re-run `invoice-chaser run` to update the report")
    return 0


def ledger_reset(args) -> int:
    path = _ledger_path(args)
    kept = reset_ledger(path, args.backup)
    print(f"ledger reset -> {path.name} (the old one is kept as {kept.name}, byte for byte)")
    return 0


def _current_orphans(out: Path, lpath: Path) -> list[dict]:
    """The orphaned decisions as the exports are NOW: the last run's inputs are re-read and matched against the ledger at run time.
    Nothing is taken from a saved orphans.json, so a candidate that has since gone away is never offered."""
    try:
        recorded = json.loads((out / "local-state.json").read_text(encoding="utf-8-sig"))["args"]
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError(f"no record of a run in {out} (local-state.json): run `invoice-chaser run` first") from None
    ns = argparse.Namespace(**recorded)
    cfg = load_config(ns.config)
    invoices, payments, _ = _load_exports(ns, cfg)
    id_index = IdIndex((i.id for i in invoices), cfg.ref_prefixes)
    return _find_orphans(lpath, payments, invoices, id_index, {_pkey(p) for p in payments})


def _invoice_orphans(orphans: list[dict], old: str) -> list[dict]:
    """Orphans are keyed by kind + old + new: identical invoices renamed X1 and X2 give two rows for the same old id."""
    return [o for o in orphans if o["kind"] == "invoice" and o["old"].strip().lower() == old.strip().lower()]


def _payment_orphans(orphans: list[dict], old: str) -> list[dict]:
    want = old.strip().lower()
    hits = [o for o in orphans if o["kind"] == "payment" and (o["old"].lower() == want or o["old"].split(":", 1)[-1].lower() == want)]
    olds = sorted({o["old"] for o in hits})
    if len(olds) > 1:
        raise ValueError(f"{old!r} matches several orphaned decisions: {', '.join(olds)}: give the source in front")
    return hits


def orphans_cmd(args) -> int:
    """List the pending orphaned decisions, found in the exports as they are now, with the exact commands that resolve each."""
    found = _current_orphans(Path(args.out), _ledger_path(args))
    if not found:
        print("no pending orphaned decisions: nothing to rebind or forget")
        return 0
    for o in found:
        print("  " + _orphan_text(o))
    print(f"  {len(found)} pending. Nothing is re-bound automatically: run one of the commands above (then `invoice-chaser run`)")
    return 0


def _already_rebound(lpath: Path, old: str, new: str) -> bool:
    """True when `new` now carries your decisions and `old` carries none: an earlier rebind already resolved this pair."""
    names = {row["key"] for row in load_payment_rows(lpath)}
    for rows in load_rejected_rows(lpath).values():
        names.update(rows)
    names |= set(load_paid(lpath)) | set(load_received(lpath))
    names = {n.strip().lower() for n in names}

    def has(x: str) -> bool:
        x = x.strip().lower()
        return any(n == x or n.endswith(":" + x) for n in names)
    return has(new) and not has(old)


def rebind(args) -> int:
    """The explicit, checked way to attach a decision to a payment (or an invoice) that now has another id. Candidates come from the
    exports as they are now; a target that already has a decision is refused (nothing is overwritten)."""
    lpath = _ledger_path(args)
    orphans = _current_orphans(Path(args.out), lpath)
    inv = _invoice_orphans(orphans, args.old)
    if inv:
        pick = [o for o in inv if o["new"].strip().lower() == args.new.strip().lower()]
        if not pick:
            raise ValueError(f"refused: the invoice with the same client, amount and due date as {inv[0]['old']} is "
                             f"{' or '.join(o['new'] for o in inv)}, not {args.new!r}")
        o = pick[0]
        if not rename_invoice(lpath, o["old"], o["new"]):
            print(f"nothing changed: no decisions about {o['old']} are in the ledger")
            return 1
        print(f"rebound: your decisions about invoice {o['old']} now belong to {o['new']}\n  re-run `invoice-chaser run` to update the report")
        return 0
    pay = _payment_orphans(orphans, args.old)
    if not pay:
        if _already_rebound(lpath, args.old, args.new):
            print(f"nothing changed: {args.old} is already rebound to {args.new}; no decision about {args.old} is left to move")
            return 1
        raise ValueError(f"{args.old!r} is not an orphaned decision in the current exports (`invoice-chaser orphans` lists them)")
    cands = {c["key"]: c for o in pay for c in o["candidates"]}
    want = args.new.strip().lower()
    hits = [c for c in cands.values() if c["key"].lower() == want or c["id"].lower() == want]
    if len(hits) > 1:
        raise ValueError(f"{args.new!r} matches several current payments: {', '.join(c['key'] for c in hits)}: give the source in front")
    if not hits:
        raise ValueError(f"{args.new!r} is not one of the look-alike payments found for {pay[0]['old']} "
                         f"({', '.join(cands) or 'none'})")
    c = hits[0]
    if c["verdict"] != "ok" and not (args.force and c["verdict"] == _name_clash("it", pay[0]["invoice"], c["named"])):
        raise ValueError(f"refused: {c['verdict']}. {c['key']} is not safe to treat as the payment you decided about. "
                         f"If you are sure it is wrong to keep the old decision: `invoice-chaser forget {quote(pay[0]['old'])}`; "
                         f"or pass --force to attach it anyway")
    if c["verdict"] != "ok":
        print(f"WARNING: {c['key']}: {c['verdict']}; attached because of --force", file=sys.stderr)
    old_key = pay[0]["old"]
    if not rebind_keys(lpath, {old_key: c["key"]}):
        print(f"nothing changed: {old_key} already points at {c['key']}")
        return 1
    print(f"rebound: your {pay[0]['decision']} decision about {old_key} now applies to {c['key']}\n  re-run `invoice-chaser run` to update the report")
    return 0


def forget(args) -> int:
    out = Path(args.out)
    lpath = _ledger_path(args)
    old = args.old.strip()
    try:
        orphans = _current_orphans(out, lpath)
    except ValueError:
        if not args.force:
            raise
        orphans = []
    inv = _invoice_orphans(orphans, old)
    if inv:
        name, done = inv[0]["old"], forget_invoice(lpath, inv[0]["old"])
    else:
        pay = _payment_orphans(orphans, old)
        if pay:
            name, done = pay[0]["old"], forget_key(lpath, pay[0]["old"])
        elif not args.force:
            raise ValueError(f"{old!r} is not an orphaned decision in the current exports (`invoice-chaser orphans` lists them); "
                             f"use --force to forget it anyway")
        else:
            name = old
            done = forget_key(lpath, old) if ":" in old else forget_invoice(lpath, old)
    if not done:
        print(f"nothing changed: nothing about {name} is in the ledger")
        return 1
    print(f"forgotten: {name} is no longer in your ledger\n  re-run `invoice-chaser run` to update the report")
    return 0


def demo(args) -> int:
    d = DEMO_DIR
    keep = getattr(args, "keep_ledger", False)
    with tempfile.TemporaryDirectory(prefix="invoice-chaser-demo-") as tmp:
        a = argparse.Namespace(
            invoices=str(d / "invoices.csv"), wise=[str(d / "wise-statement.csv")],
            payoneer=[str(d / "payoneer-transactions.csv")], bank=[str(d / "bank-export.csv")],
            mapping=str(d / "bank-mapping.json"), config=str(d / "invoice-chaser.json"),
            templates=None, out=args.out, today=args.today or DEMO_TODAY.isoformat(),
            tone=args.tone, sender_name=None, llm=False, ledger=None if keep else str(Path(tmp) / "sent.json"),
            date_format=None, strict=False, decimal_comma=False)
        code = run(a)
    if code == 0 and not keep:
        print("  (demo starts from a fresh ledger every run; add --keep-ledger to keep your confirm / mark decisions between runs)")
    return code


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
    c.add_argument("--payment", action="append", help="count only this payment (repeatable); also works on a held suggestion")
    c.add_argument("--force", action="store_true", help="with --payment: count a payment whose text names a different invoice")
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
        sp.add_argument("--force", action="store_true", help="record an id that is not in <out>/status.csv; mark-paid: also a second payment "
                                                            "of the same client and amount")
        common(sp)
    m.add_argument("--checked", action="store_true", help="required for a CHECK FIRST reminder: you verified the possible payment first")
    m.set_defaults(fn=mark_sent)
    mp.set_defaults(fn=mark_paid)
    um = sub.add_parser("unmark-paid", help="undo a confirm / mark-paid")
    common(um)
    um.set_defaults(fn=unmark_paid)

    ub = sub.add_parser("unbind", help="a payment does NOT back this invoice: remove that confirmation (the other invoice keeps its own)")
    ub.add_argument("invoice")
    ub.add_argument("payment", help="payment key (wise:ID) or id, as shown in the PAYMENT_DOUBLE_BOUND warning")
    common(ub, invoice=False)
    ub.set_defaults(fn=unbind)

    orp = sub.add_parser("orphans", help="list pending orphaned decisions (found in today's exports) with the exact commands")
    common(orp, invoice=False)
    orp.set_defaults(fn=orphans_cmd)

    rb = sub.add_parser("rebind", help="attach a decision to the payment (or invoice) that now has another id, after the safety checks")
    rb.add_argument("old")
    rb.add_argument("new")
    rb.add_argument("--today", help=argparse.SUPPRESS)
    rb.add_argument("--force", action="store_true", help="attach to a payment whose text names a different invoice (warned, recorded)")
    common(rb, invoice=False)
    rb.set_defaults(fn=rebind)

    fg = sub.add_parser("forget", help="drop an orphaned decision (a confirmed or rejected payment, or a confirmed invoice id)")
    fg.add_argument("old")
    fg.add_argument("--force", action="store_true", help="also forget something that is not listed as orphaned")
    common(fg, invoice=False)
    fg.set_defaults(fn=forget)

    lg = sub.add_parser("ledger", help="manage out/sent.json")
    lgs = lg.add_subparsers(dest="ledger_cmd", required=True)
    rs = lgs.add_parser("reset", help="start a fresh ledger (use after a pre-release build); --backup keeps a copy")
    rs.add_argument("--backup", action="store_true", help="keep a copy of the old ledger as <name>.pre-release-<time>")
    common(rs, invoice=False)
    rs.set_defaults(fn=ledger_reset)

    d = sub.add_parser("demo", help="run on the bundled synthetic samples")
    d.add_argument("--out", default="out")
    d.add_argument("--today")
    d.add_argument("--tone", choices=["friendly", "neutral", "firm"])
    d.add_argument("--keep-ledger", action="store_true", help="use <out>/sent.json (your confirm / mark decisions) instead of a fresh ledger")
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
