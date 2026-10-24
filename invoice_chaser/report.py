"""status.csv and the self-contained review.html. Every value is escaped."""
from __future__ import annotations

import csv
import re
from decimal import Decimal
from html import escape
from pathlib import Path

from .models import InvoiceResult, Payment
from .decision import visible
from .shellquote import quote

STATUS_ORDER = [("suggested_paid", "Suggested paid (waiting for your confirm)"),
                ("partial_suggested", "Part-payment suggested (waiting for your confirm)"),
                ("needs_review", "Needs review"), ("overdue", "Overdue"), ("partial", "Partial (part-payment confirmed)"),
                ("open", "Open, not yet due"), ("paid", "Paid (confirmed by you or marked paid in invoices.csv)"), ("void", "Void")]

CSV_FIELDS = ["invoice_id", "client", "email", "currency", "amount", "paid", "outstanding", "due",
              "status", "days_overdue", "stage", "confidence", "matched_payments", "reason", "why_not_paid", "waiting_days", "stuck", "draft_status", "draft_file", "last_reminder"]


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
                ";".join(m.payment_id for m in r.matches), _reason(r), r.why_not_paid, r.waiting_days, "yes" if r.stuck else "", r.draft_status, drafts.get(i.id, ""),
                r.last_reminder.isoformat() if r.last_reminder else ""]])


def totals_by_currency(results: list[InvoiceResult]) -> dict[str, dict[str, Decimal]]:
    t: dict[str, dict[str, Decimal]] = {}
    for r in results:
        if r.status == "void":
            continue
        row = t.setdefault(r.invoice.currency, {"invoiced": Decimal(0), "paid": Decimal(0), "suggested": Decimal(0),
                                                "overdue": Decimal(0), "not_due": Decimal(0), "review": Decimal(0)})
        row["invoiced"] += r.invoice.amount
        if r.status in ("suggested_paid", "partial_suggested"):
            row["suggested"] += r.paid
            row["review"] += r.outstanding
        elif r.status == "needs_review":
            row["review"] += r.outstanding
        else:
            row["paid"] += r.paid
            if r.status in ("overdue", "partial") and r.days_overdue > 0:
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
.cmd{user-select:all;background:var(--bg);border:1px solid var(--line);border-radius:4px;padding:1px 6px;font-size:12px}
.confirm{border:1px solid #15803d;border-left-width:4px;border-radius:8px;padding:10px 14px;margin:0 0 16px;background:var(--card)}
.stuck{border:1px solid #b91c1c;border-left-width:6px;border-radius:8px;padding:10px 14px;margin:0 0 16px;background:var(--card)}
.warn{background:var(--card);border:1px solid #b45309;border-left-width:4px;border-radius:8px;padding:10px 14px;margin:0 0 16px}
"""


def _out_arg(out_hint: str) -> str:
    return f' --out "{out_hint}"' if (" " in out_hint) else (f" --out {out_hint}" if out_hint != "out" else "")


def _cmd(text: str) -> str:
    return f"<code class=cmd>{e(text)}</code>"


def _confirm_section(suggestions: list[dict], out_hint: str) -> str:
    """The top of the page: one copy-paste command per suggestion. Nothing is paid until you run one."""
    strong = [s for s in suggestions if s["kind"] in ("paid", "partial")]
    if not strong:
        return ""
    out = _out_arg(out_hint)
    rows = []
    for s in strong:
        pays = ""
        for p in s["payments"]:
            amt = f"{Decimal(p['amount']):,.2f}"
            pays += (f"<div class=small><strong>{e(p['id'])}</strong> ({e(p['source'])}) {e(p['date'])}: {e(amt)} {e(p['currency'])}"
                     f" from {e(visible(p['payer']) or '(no payer name)')}<br>{e(visible(p['description']))}</div>")
        iid = shlex_quote(s["invoice_id"])
        cmds = f"<div>{_cmd('invoice-chaser confirm ' + iid + out)}</div>"
        for p in s["payments"]:
            cmds += f"<div class=small>not this one: {_cmd('invoice-chaser reject ' + iid + ' ' + shlex_quote(p['id']) + out)}</div>"
        label = "paid in full" if s["kind"] == "paid" else "part-payment"
        total = f"{Decimal(s['amount']):,.2f}"
        conf = f"{s['confidence']:.2f}"
        rows.append(f"<tr><td>{e(s['invoice_id'])}</td><td>{e(s['client'])}</td>"
                    f"<td class=n>{e(total)} {e(s['currency'])}</td><td>{e(label)}</td><td>{pays}</td>"
                    f"<td class=n>{e(conf)}</td><td class=small>{e(s['reason'])}</td><td>{cmds}</td></tr>")
    bulk = ""
    if any(s["kind"] == "paid" for s in strong):
        bulk = (f"<p>Reviewed them all? {_cmd('invoice-chaser confirm --all-strong' + out)} prints the list and asks "
                f"y/N before it records anything.</p>")
    return ("<div class=confirm><strong>Confirm these</strong> <span class=small>The tool found these payments but never marks an "
            "invoice paid by itself. No reminder is drafted for them until you confirm or reject.</span></div>"
            "<div class=wrap><table><tr><th>Invoice</th><th>Client</th><th>Amount</th><th>Suggestion</th><th>Payment found</th>"
            "<th>Conf.</th><th>Why</th><th>Your decision</th></tr>" + "".join(rows) + "</table></div>" + bulk)


def _stuck_section(results: list[InvoiceResult], out_hint: str) -> str:
    """Invoices that have waited for your decision too long. Loud on purpose: a wrong suggestion must not
    park an unpaid invoice forever."""
    stuck = sorted((r for r in results if r.stuck), key=lambda r: -r.days_overdue)
    if not stuck:
        return ""
    out = _out_arg(out_hint)
    rows = "".join(
        f"<tr><td>{e(r.invoice.id)}</td><td>{e(r.invoice.client)}</td><td class=n>{e(f'{r.outstanding or r.invoice.amount:,.2f}')} {e(r.invoice.currency)}</td>"
        f"<td class=n>{e(r.days_overdue)}</td><td class=n>{e(r.waiting_days)}</td><td class=small>{e(r.why_not_paid)}</td>"
        f"<td>{_cmd('invoice-chaser confirm ' + shlex_quote(r.invoice.id) + out) if r.status != 'needs_review' else _cmd('invoice-chaser mark-paid ' + shlex_quote(r.invoice.id) + out)}</td></tr>"
        for r in stuck)
    oldest = stuck[0]
    return (f"<div class=stuck><strong>{len(stuck)} invoice{'s' if len(stuck) != 1 else ''} stuck waiting for your decision</strong> "
            f"<span class=small>oldest: {e(oldest.invoice.id)}, {e(oldest.days_overdue)} days overdue. No reminder goes out while a payment is "
            f"suggested, so decide: confirm it, reject it, or mark it paid. A \"check this one\" note to you is in drafts/.</span></div>"
            "<div class=wrap><table><tr><th>Invoice</th><th>Client</th><th>Outstanding</th><th>Days overdue</th><th>Waiting (days)</th>"
            "<th>Why</th><th>Your decision</th></tr>" + rows + "</table></div>")


def _markpaid(invoice_id: str, out_hint: str) -> str:
    out = _out_arg(out_hint)
    return ("<div>If you checked and it is paid, run: "
            f"<code class=cmd>invoice-chaser mark-paid {e(shlex_quote(invoice_id))}{e(out)}</code></div>")


def shlex_quote(s: str) -> str:
    return quote(s)


def _rows(results: list[InvoiceResult], drafts: dict[str, str], out_hint: str = "out") -> str:
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
            f"<td class=n>{e(f'{r.confidence:.2f}')}</td><td class=small>{e(_reason(r))}"
            f"{'<div><strong>Why not paid:</strong> ' + e(r.why_not_paid) + '</div>' if r.why_not_paid and r.status not in ('open', 'suggested_paid', 'partial_suggested') else ''}"
            f"{_markpaid(i.id, out_hint) if r.status == 'needs_review' else ''}</td></tr>")
    return "".join(out)


def render_html(results: list[InvoiceResult], leftovers: list[tuple[Payment, Decimal]],
                drafts: dict[str, str], today, warnings: list[str] | None = None,
                early=None, ignored: list[Payment] | None = None, out_hint: str = "out",
                suggestions: list[dict] | None = None) -> str:
    parts = [f"<!doctype html><html lang=en><head><meta charset=utf-8>"
             f"<meta name=viewport content='width=device-width,initial-scale=1'>"
             f"<title>Invoice review {e(today.isoformat())}</title><style>{CSS}</style></head><body><main>"
             f"<h1>Invoice review</h1><p class=sub>As of {e(today.isoformat())}. Generated locally. "
             f"Nothing was sent: drafts are files for you to read, edit and send yourself.</p>"]
    parts.append(_stuck_section(results, out_hint))
    parts.append(_confirm_section(suggestions or [], out_hint))
    if warnings:
        parts.append("<div class=warn><strong>Check before using these drafts</strong><ul>"
                     + "".join(f"<li>{e(w)}</li>" for w in warnings) + "</ul></div>")
    tot = totals_by_currency(results)
    parts.append("<h2>Totals by currency</h2><div class=wrap><table><tr><th>Currency</th><th>Invoiced</th><th>Confirmed paid</th>"
                 "<th>Suggested paid</th><th>Overdue outstanding</th><th>Not yet due</th><th>Needs review / waiting</th></tr>")
    for cur, t in sorted(tot.items()):
        parts.append(f"<tr><td>{e(cur)}</td>" + "".join(
            f"<td class=n>{e(f'{t[k]:,.2f}')}</td>" for k in ("invoiced", "paid", "suggested", "overdue", "not_due", "review")) + "</tr>")
    parts.append("</table></div>")
    head = ("<tr><th>Invoice</th><th>Client</th><th>Amount</th><th>Paid</th><th>Outstanding</th><th>Due</th>"
            "<th>Days overdue</th><th>Stage / draft</th><th>Conf.</th><th>Why</th></tr>")
    for key, label in STATUS_ORDER:
        rs = [r for r in results if r.status == key]
        if not rs:
            continue
        parts.append(f"<h2>{e(label)} ({len(rs)})</h2><div class=wrap><table>{head}{_rows(rs, drafts, out_hint)}</table></div>")
    if leftovers:
        parts.append("<h2>Payments not fully matched</h2><div class=wrap><table><tr><th>Payment</th><th>Date</th>"
                     "<th>Remaining</th><th>Payer</th><th>Description</th></tr>")
        for p, rem in leftovers:
            parts.append(f"<tr><td>{e(p.id)}</td><td>{e(p.date.isoformat())}</td><td class=n>{e(f'{rem:,.2f}')} {e(p.currency)}</td>"
                         f"<td>{e(p.payer)}</td><td>{e(p.description)}</td></tr>")
        parts.append("</table></div>")
    if early:
        parts.append("<h2>Payments outside the invoice date window (check date format)</h2><p class=small>These look like "
                     "payments for an invoice but are dated before it was issued or far after. A day/month mix-up is the usual "
                     "cause. The invoices involved are held in needs review; no reminder is drafted for them.</p>"
                     "<div class=wrap><table><tr><th>Payment</th><th>Date</th><th>Amount</th><th>Possible invoice</th>"
                     "<th>Conf.</th><th>Problem</th></tr>")
        for x in early:
            p = x.payment
            parts.append(f"<tr><td>{e(p.id)}</td><td>{e(p.date.isoformat())}</td><td class=n>{e(f'{p.amount:,.2f}')} {e(p.currency)}</td>"
                         f"<td>{e(x.invoice.id)} ({e(x.invoice.client)})</td><td class=n>{e(f'{x.confidence:.2f}')}</td>"
                         f"<td class=small>{e(x.problem)}</td></tr>")
        parts.append("</table></div>")
    if ignored:
        shown = ignored[:50]
        parts.append(f"<h2>Negative rows ignored ({len(ignored)})</h2><p class=small>Refunds, fees and withdrawals. "
                     "They are not netted against any invoice.</p><div class=wrap><table><tr><th>Source</th><th>Date</th>"
                     "<th>Amount</th><th>Description</th></tr>")
        for p in shown:
            parts.append(f"<tr><td class=small>{e(p.source)}</td><td>{e(p.date.isoformat())}</td>"
                         f"<td class=n>{e(f'{p.amount:,.2f}')} {e(p.currency)}</td><td>{e(p.description or p.payer)}</td></tr>")
        if len(ignored) > len(shown):
            parts.append(f"<tr><td colspan=4 class=small>and {len(ignored) - len(shown)} more</td></tr>")
        parts.append("</table></div>")
    parts.append("</main></body></html>")
    return "".join(parts)
