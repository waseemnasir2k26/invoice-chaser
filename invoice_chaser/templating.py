"""Reminder templates: templates/<stage>.<tone>.md

File format: first line "Subject: ...", blank line, body. Variables use {{name}}.
An unknown variable raises, so a typo never ships as literal braces.
"""
from __future__ import annotations

import re
from pathlib import Path

from .models import InvoiceResult

VARIABLES = {
    "first_name", "client", "invoice_id", "amount", "outstanding", "currency",
    "due_date", "days_overdue", "sender_name", "payment_details", "personal_line",
}
TONES = ("friendly", "neutral", "firm")


class TemplateError(ValueError):
    pass


def default_templates_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "templates"


def load_template(stage: str, tone: str, templates_dir: Path | None = None) -> str:
    d = Path(templates_dir) if templates_dir else default_templates_dir()
    for t in (tone, "neutral"):
        p = d / f"{stage}.{t}.md"
        if p.exists():
            return p.read_text(encoding="utf-8")
    raise TemplateError(f"no template for stage={stage} tone={tone} in {d}")


def render(template: str, variables: dict[str, str]) -> tuple[str, str]:
    def sub(m):
        name = m.group(1)
        if name not in VARIABLES:
            raise TemplateError(f"unknown template variable {{{{{name}}}}}")
        return str(variables.get(name, ""))

    text = re.sub(r"\{\{\s*(\w+)\s*\}\}", sub, template.replace("\r\n", "\n"))
    first, _, rest = text.partition("\n")
    if not first.lower().startswith("subject:"):
        raise TemplateError("template must start with 'Subject: ...'")
    subject = first.split(":", 1)[1].strip()
    body = re.sub(r"\n{3,}", "\n\n", rest.strip("\n")).strip() + "\n"
    return subject, body


def first_name(res: InvoiceResult) -> str:
    """First name from the optional `contact` column. Company names are never guessed
    into a first name; without a contact the greeting falls back to "there"."""
    contact = res.invoice.contact.strip()
    return contact.split()[0] if contact else ""


def money(amount, currency: str) -> str:
    return f"{amount:,.2f} {currency}"


def variables_for(res: InvoiceResult, sender_name: str, payment_details: str, personal_line: str = "") -> dict[str, str]:
    inv = res.invoice
    return {
        "first_name": first_name(res) or "there",
        "client": inv.client,
        "invoice_id": inv.id,
        "amount": money(inv.amount, inv.currency),
        "outstanding": money(res.outstanding, inv.currency),
        "currency": inv.currency,
        "due_date": inv.due.isoformat(),
        "days_overdue": str(res.days_overdue),
        "sender_name": sender_name,
        "payment_details": payment_details,
        "personal_line": personal_line,
    }
