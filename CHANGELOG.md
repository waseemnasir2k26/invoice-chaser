# Changelog

## 1.0.0 - 2026-10-25

First release.

- Parsers for Wise statement CSV, Payoneer transactions CSV, and any bank CSV through a column-mapping file. Tolerant header matching.
- Matching of payments to invoices with a confidence and a reason per match: invoice id in text, exact amount, fee/FX tolerance, fuzzy client name, date window, partial payments, optional FX rates.
- Low-confidence matches are marked "needs review" and are never counted as paid.
- Overdue ladder (day 1, 7, 14, 30) with 12 editable templates (4 stages x 3 tones).
- Optional one-line personalisation through a free Groq or Gemini key; sends only first name, amount, days overdue and invoice id.
- Outputs: `status.csv`, self-contained `review.html`, `drafts/*.eml`, `drafts.json`. Nothing is sent.
- `examples/n8n-weekly.json` and `docs/n8n.md`.
