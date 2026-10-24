from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class Invoice:
    id: str
    client: str
    email: str
    amount: Decimal
    currency: str
    issued: date
    due: date
    status: str = ""
    contact: str = ""
    last_reminder: date | None = None


@dataclass
class Payment:
    id: str
    date: date
    amount: Decimal
    currency: str
    description: str
    payer: str
    source: str
    suspect: str = ""  # set when this row may duplicate another: never auto-matched


@dataclass
class Match:
    invoice_id: str
    payment_id: str
    payment_date: date
    payment_description: str
    confidence: float
    reason: str
    allocated: Decimal = Decimal(0)
    blocker: str = ""  # why this was not counted as paid


@dataclass
class InvoiceResult:
    invoice: Invoice
    status: str  # paid | partial | overdue | open | needs_review | void
    paid: Decimal
    outstanding: Decimal
    days_overdue: int
    stage: str | None
    confidence: float
    matches: list[Match] = field(default_factory=list)
    suggestions: list[Match] = field(default_factory=list)
    note: str = ""
    draft_status: str = ""
    last_reminder: date | None = None
    why_not_paid: str = ""
