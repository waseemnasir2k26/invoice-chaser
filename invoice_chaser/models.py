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


@dataclass
class Payment:
    id: str
    date: date
    amount: Decimal
    currency: str
    description: str
    payer: str
    source: str


@dataclass
class Match:
    invoice_id: str
    payment_id: str
    payment_date: date
    payment_description: str
    confidence: float
    reason: str
    allocated: Decimal = Decimal(0)


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
