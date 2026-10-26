"""Warnings carry a CODE and a SEVERITY, set where the warning is raised. Nothing ever classifies a warning by reading its text:
the text can contain payer names and descriptions from your exports, which anyone could word to look routine
("Evil (row skipped)").

critical  something that could mark a client paid by mistake or chase one who paid, or an invoice row that was dropped.
          Always printed in full, on its own line.
warning   worth reading; printed in full.
routine   expected noise (refund and fee rows, pending rows). Counted, never spelled out in the terminal; all of them are in review.html.
"""
from __future__ import annotations

SEVERITY = {
    # critical
    "ORPHANED_DECISION": "critical",
    "PAYMENT_DOUBLE_BOUND": "critical",
    "ID_COLLISION": "critical",
    "POSSIBLE_DOUBLE_COUNT": "critical",
    "INVOICE_ROW_DROPPED": "critical",
    "DUPLICATE_INVOICE_ID": "critical",
    "CHANGED_INVOICE": "critical",
    "STUCK_HOLD": "critical",
    "OVERDUE_HELD": "critical",
    "NO_SENDER": "critical",
    # warning
    "PAYMENT_ROW_DROPPED": "warning",
    "DUPLICATE_PAYMENT_ROW": "warning",
    "DATE_AMBIGUOUS": "warning",
    "CURRENCY_UNKNOWN": "warning",
    "STATE_UNREADABLE": "warning",
    "DEPENDENT_TOTAL": "warning",
    "EMAIL_MISSING": "warning",
    "DRAFT_STALE": "warning",
    "REJECTION_UNVERIFIED": "warning",
    "NEAR_AMOUNT": "warning",
    "CURRENCY_MISMATCH": "warning",
    "REFUND_SEEN": "warning",
    # routine
    "NEGATIVE_ROWS": "routine",
    "ROWS_SKIPPED_STATUS": "routine",
    "DRAFT_KEPT": "routine",
}


class Notice(str):
    """A warning message that knows its code and severity. It behaves like the message text (so it prints, compares and
    escapes as one), but `code` and `severity` come from where it was raised, never from the text."""

    code: str
    severity: str

    def __new__(cls, message: str, code: str = "NOTE", severity: str | None = None):
        obj = str.__new__(cls, message)
        obj.code = code
        obj.severity = severity or SEVERITY.get(code, "warning")
        return obj
