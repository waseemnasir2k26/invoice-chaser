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
    file_no: int = 0  # which export file of this run the row came from (0 = unknown); two rows of one file are never "two files"


@dataclass
class Match:
    invoice_id: str
    payment_id: str
    payment_date: date
    payment_description: str
    confidence: float
    reason: str
    allocated: Decimal = Decimal(0)
    blocker: str = ""  # why this was not counted as strong evidence
    payer: str = ""
    source: str = ""
    currency: str = ""
    amount: Decimal = Decimal(0)  # the payment row as exported (before conversion)
    named: tuple = ()  # invoice ids the payment text names
    suspect: str = ""  # why this payment is held (possible duplicate, id collision...)


@dataclass
class InvoiceResult:
    invoice: Invoice
    # suggested_paid | partial_suggested | needs_review | overdue | open come from the engine;
    # paid | partial | void only come from invoices.csv or from your own confirm / mark-paid.
    status: str
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
    waiting_days: int = 0  # how long this suggestion / review has been waiting for your decision
    stuck: bool = False
    confirmed_on: str = ""  # date you confirmed or marked this invoice paid in the ledger ("" = never); a held one gets no client draft
