"""status.csv and the self-contained review.html. Every value is escaped."""
from __future__ import annotations

import csv
from decimal import Decimal
from html import escape
from pathlib import Path

from .models import InvoiceResult, Payment

STATUS_ORDER = [("needs_review", "Needs review"), ("overdue", "Overdue"), ("partial", "Partial"),
                ("open", "Open, not yet due"), ("paid", "Paid"), ("void", "Void")]

CSV_FIELDS = ["invoice_id", "client", "email", "currency", "amount", "paid", "outstanding", "due",
              "status", "days_overdue", "stage", "confidence", "matched_payments", "reason", "draft_status", "draft_file", "last_reminder"]


def _csv_safe(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _reason(r: InvoiceResult) -> str:
    parts = [f"{m.payment_id}: {m.reason}" for m in r.matches] + \
            [f"suggested {m.payment_id} ({m.confidence}): {m.reason}" for m in r.suggestions]
    if r.note:
        parts.append(r.note)
    return " | ".join(parts)


def write_status_csv(results: list[InvoiceResult], drafts: dict[str, str], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_FIELDS)
        for r in results:
            i = r.invoice
            w.writerow([_csv_safe(x) for x in [
                i.id, i.client, i.email, i.currency, i.amount, r.paid, r.outstanding, i.due.isoformat(),
                r.status, r.days_overdue, r.stage or "", f"{r.confidence:.2f}",
                ";".join(m.payment_id for m in r.matches), _reason(r), r.draft_status, drafts.get(i.id, ""),
                r.last_reminder.isoformat() if r.last_reminder else ""]])


def totals_by_currency(results: list[InvoiceResult]) -> dict[str, dict[str, Decimal]]:
    t: dict[str, dict[str, Decimal]] = {}
    for r in results:
        if r.status == "void":
            continue
        row = t.setdefault(r.invoice.currency, {"invoiced": Decimal(0), "paid": Decimal(0),
                                                "overdue": Decimal(0), "not_due": Decimal(0), "review": Decimal(0)})
        row["invoiced"] += r.invoice.amount
        row["paid"] += r.paid
        if r.status == "needs_review":
            row["review"] += r.outstanding
        elif r.status in ("overdue", "partial") and r.days_overdue > 0:
            row["overdue"] += r.outstanding
        elif r.status in ("open", "partial"):
            row["not_due"] += r.outstanding
    return t


def e(v) -> str:
    return escape(str(v), quote=True)


CSS = """
:root{--bg:#fafaf9;--fg:#1c1917;--mut:#57534e;--card:#fff;--line:#e7e5e4;--acc:#1d4ed8}
@media (prefers-color-scheme:dark){:root{--bg:#171717;--fg:#f5f5f4;--mut:#a8a29e;--card:#222;--line:#3a3a3a;--acc:#93c5fd}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px}
h1{font-size:1.5rem;margin:0 0 4px}h2{font-size:1.1rem;margin:28px 0 8px}
p.sub{color:var(--mut);margin:0 0 16px}
.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;min-width:640px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top;font-size:14px}
th{color:var(--mut);font-weight:600}tr:last-child td{border-bottom:0}
td.n{text-align:right;font-variant-numeric:tabular-nums}
.tag{display:inline-block;padding:1px 8px;border-radius:99px;border:1px solid var(--line);font-size:12px}
.small{color:var(--mut);font-size:12px}
.warn{background:var(--card);border:1px solid #b45309;border-left-width:4px;border-radius:8px;padding:10px 14px;margin:0 0 16px}
"""


def _rows(results: list[InvoiceResult], drafts: dict[str, str]) -> str:
    out = []
    for r in results:
        i = r.invoice
        out.append(
            "<tr>"
            f"<td>{e(i.id)}</td><td>{e(i.client)}<div class=small>{e(i.email)}</div></td>"
            f"<td class=n>{e(i.amount)} {e(i.currency)}</td><td class=n>{e(r.paid)}</td><td class=n>{e(r.outstanding)}</td>"
            f"<td>{e(i.due.isoformat())}</td><td class=n>{e(r.days_overdue)}</td>"
            f"<td>{e(r.stage or '')}{'<div class=small>' + e(drafts[i.id]) + '</div>' if i.id in drafts else ''}"
            f"{'<div class=small>' + e(r.draft_status) + '</div>' if r.draft_status and i.id not in drafts else ''}</td>"
            f"<td class=n>{e(f'{r.confidence:.2f}')}</td><td class=small>{e(_reason(r))}</td></tr>")
    return "".join(out)


def render_html(results: list[InvoiceResult], leftovers: list[tuple[Payment, Decimal]],
                drafts: dict[str, str], today, warnings: list[str] | None = None) -> str:
    parts = [f"<!doctype html><html lang=en><head><meta charset=utf-8>"
             f"<meta name=viewport content='width=device-width,initial-scale=1'>"
             f"<title>Invoice review {e(today.isoformat())}</title><style>{CSS}</style></head><body><main>"
             f"<h1>Invoice review</h1><p class=sub>As of {e(today.isoformat())}. Generated locally. "
             f"Nothing was sent: drafts are files for you to read, edit and send yourself.</p>"]
    if warnings:
        parts.append("<div class=warn><strong>Check before using these drafts</strong><ul>"
                     + "".join(f"<li>{e(w)}</li>" for w in warnings) + "</ul></div>")
    tot = totals_by_currency(results)
    parts.append("<h2>Totals by currency</h2><div class=wrap><table><tr><th>Currency</th><th>Invoiced</th><th>Paid</th>"
                 "<th>Overdue outstanding</th><th>Not yet due</th><th>Needs review</th></tr>")
    for cur, t in sorted(tot.items()):
        parts.append(f"<tr><td>{e(cur)}</td>" + "".join(
            f"<td class=n>{e(f'{t[k]:,.2f}')}</td>" for k in ("invoiced", "paid", "overdue", "not_due", "review")) + "</tr>")
    parts.append("</table></div>")
    head = ("<tr><th>Invoice</th><th>Client</th><th>Amount</th><th>Paid</th><th>Outstanding</th><th>Due</th>"
            "<th>Days overdue</th><th>Stage / draft</th><th>Conf.</th><th>Why</th></tr>")
    for key, label in STATUS_ORDER:
        rs = [r for r in results if r.status == key]
        if not rs:
            continue
        parts.append(f"<h2>{e(label)} ({len(rs)})</h2><div class=wrap><table>{head}{_rows(rs, drafts)}</table></div>")
    if leftovers:
        parts.append("<h2>Payments not fully matched</h2><div class=wrap><table><tr><th>Payment</th><th>Date</th>"
                     "<th>Remaining</th><th>Payer</th><th>Description</th></tr>")
        for p, rem in leftovers:
            parts.append(f"<tr><td>{e(p.id)}</td><td>{e(p.date.isoformat())}</td><td class=n>{e(f'{rem:,.2f}')} {e(p.currency)}</td>"
                         f"<td>{e(p.payer)}</td><td>{e(p.description)}</td></tr>")
        parts.append("</table></div>")
    parts.append("</main></body></html>")
    return "".join(parts)
