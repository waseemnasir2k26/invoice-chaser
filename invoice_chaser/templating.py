"""Reminder templates: <stage>.<tone>.md

File format: first line "Subject: ...", blank line, body. Variables use {{name}}.
An unknown variable raises, so a typo never ships as literal braces.

Conditional blocks {{#name}}...{{/name}} are kept only when the flag is on:
  earlier  you told the tool an earlier reminder went out (sent.json, a
           last_reminder column, or previous_reminders: true)
  pause    config pause_work_on_final is true (final and escalation stages)
Without those flags a draft never claims earlier contact or actions.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .models import InvoiceResult

VARIABLES = {
    "first_name", "client", "invoice_id", "amount", "outstanding", "currency",
    "due_date", "days_overdue", "sender_name", "payment_details", "personal_line",
    "earlier_reminder", "reply_days",
}
BLOCKS = {"earlier", "pause"}
TONES = ("friendly", "neutral", "firm")

_BLOCK = re.compile(r"\{\{#(\w+)\}\}(.*?)\{\{/\1\}\}", re.DOTALL)
_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


class TemplateError(ValueError):
    pass


def default_templates_dir() -> Path:
    return Path(__file__).resolve().parent / "templates"


def load_template(stage: str, tone: str, templates_dir: Path | None = None) -> str:
    d = Path(templates_dir) if templates_dir else default_templates_dir()
    for t in (tone, "neutral"):
        p = d / f"{stage}.{t}.md"
        if p.exists():
            return p.read_text(encoding="utf-8")
    raise TemplateError(f"no template for stage={stage} tone={tone} in {d}")


def render(template: str, variables: dict[str, str], flags=frozenset()) -> tuple[str, str]:
    text = template.replace("\r\n", "\n")

    def block(m):
        if m.group(1) not in BLOCKS:
            raise TemplateError(f"unknown template block {{{{#{m.group(1)}}}}}")
        return m.group(2) if m.group(1) in flags else ""

    text = _BLOCK.sub(block, text)
    for m in _VAR.finditer(text):
        if m.group(1) not in VARIABLES:
            raise TemplateError(f"unknown template variable {{{{{m.group(1)}}}}}")
    if "{{" in _VAR.sub("", text):
        raise TemplateError("unbalanced or malformed {{ }} tag in template")
    text = _VAR.sub(lambda m: str(variables.get(m.group(1), "")), text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    first, _, rest = text.partition("\n")
    if not first.lower().startswith("subject:"):
        raise TemplateError("template must start with 'Subject: ...'")
    subject = first.split(":", 1)[1].strip()
    body = re.sub(r"\n{3,}", "\n\n", rest.strip("\n")).strip() + "\n"
    return subject, body


def first_name(res: InvoiceResult) -> str:
    """First name from the optional `contact` column. Company names are never guessed
    into a first name, and an e-mail address is never used as a name: without one the greeting falls back to "there"."""
    contact = res.invoice.contact.strip()
    if not contact or "@" in contact:
        return ""
    return contact.split()[0]


def money(amount, currency: str) -> str:
    return f"{amount:,.2f} {currency}"


def flags_for(stage: str, previous_reminders: bool, last_reminder: date | None, pause_work_on_final: bool) -> frozenset:
    flags = set()
    if stage != "nudge" and (previous_reminders or last_reminder):
        flags.add("earlier")
    if pause_work_on_final and stage in ("final", "escalation"):
        flags.add("pause")
    return frozenset(flags)


def variables_for(res: InvoiceResult, sender_name: str, payment_details: str, personal_line: str = "",
                  *, reply_days: int = 7, last_reminder: date | None = None) -> dict[str, str]:
    inv = res.invoice
    return {
        "first_name": first_name(res) or "there",
        "client": inv.client,
        "invoice_id": inv.id,
        "amount": money(inv.amount, inv.currency),
        "outstanding": money(res.owed if res.owed is not None else res.outstanding, inv.currency),
        "currency": inv.currency,
        "due_date": inv.due.isoformat(),
        "days_overdue": str(res.days_overdue),
        "sender_name": sender_name,
        "payment_details": payment_details,
        "personal_line": personal_line,
        "earlier_reminder": f"my reminder of {last_reminder.isoformat()}" if last_reminder else "my earlier reminder",
        "reply_days": str(reply_days),
    }
