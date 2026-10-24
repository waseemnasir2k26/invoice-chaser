# Changelog

## 1.0.1 - 2026-10-25

Fixes from the first review round.

- `pip install .` then `invoice-chaser demo` works from any folder: demo data now ships inside the package.
- Reminder templates no longer assert history the tool cannot know. Drafts say nothing about earlier reminders or paused work unless you unlock it: `sent.json` ledger, a `last_reminder` column, `previous_reminders: true`, and `pause_work_on_final: true`. New `reply_within_days` setting.
- New `invoice-chaser mark-sent <invoice> [--stage] [--date]` and `sent.json` ledger: a sent stage is never drafted twice, and follow-ups cite the date of your last reminder.
- **The "paid" decision is now one table, not a score** (README: How matching decides). A payment counts as paid only when (A) exactly one invoice id appears in its text as a strict whole token (`INV-1/2`, `INV-1.5`, `INV 1 2`, `INV-10` do not match INV-1), the amount is within tolerance, there is no refund/reversal/credit-note word, and it names no other invoice; or (B) the amount is exact and the payer IS the client: same words in any order, same legal-suffix kind (Ltd != Inc != none). Bare numbers never count as ids ("Deposit for 5 hours"); all-digit ids need `ref_prefixes`. Everything else is "needs review" with a *why not paid* column and a copy-paste `invoice-chaser mark-paid INV-x`. 1,900 adversarial test scenarios assert zero false paid.
- Shared prices: candidates are capped per invoice and never paired in bulk; with more than 20 same-amount payments and nothing identifying yours, the invoice goes to needs review with one line instead of exhausting memory. A stranger's same-amount payment means needs review, not a reminder.
- Amounts are parsed strictly: ambiguous (`1,234`, `1.500`) or malformed (`1e400`, `1000 (2 of 3)`, `1,23,456`) values skip the row with the reason; `decimal_comma` / `--decimal-comma` says which you mean.
- New `unmark-paid`; a second `mark-paid` warns and changes nothing. An edited draft of an invoice that becomes paid is moved to `drafts/stale/` (never deleted); edited drafts are tracked in `kept.json` so the warning repeats. `drafts.json` keeps the hashes.
- Duplicate rows cannot settle two invoices: the same transfer id twice in one export is dropped with a warning; the same payment in two different exports is flagged and held for review.
- Payments dated before an invoice (or far after it) are no longer dropped silently: they are listed in `review.html` and the invoice is held, not chased. New `--date-format dmy|mdy|ymd`; a warning when every date in a file is ambiguous.
- Currencies are normalised (`usd `, `€`, `£`); a bare `$` needs `default_currency` in the config; non-ISO strings warn. Negative rows (refunds, fees) are warned about and listed. UTF-8 BOM, UTF-16 and Windows-1252 CSVs and BOM'd JSON files are read.
- New `invoice-chaser mark-paid`. `mark-sent` and `mark-paid` match ids ignoring case, refuse unknown ids and a missing `status.csv` unless `--force`, and refuse future dates.
- A bad CSV row is skipped with a warning (listed in `review.html`) instead of aborting the run; `--strict` aborts.
- Matching is indexed (exact amount, invoice-id token, payer equal to the client inside the date window) instead of all-pairs. Measured cold on one Windows 11 laptop (Intel 13th-gen-class CPU, 24 logical cores, Python 3.11.9, single thread, synthetic data): 1,000 invoices x 5,000 payments in 0.4 s, 3,000 x 15,000 in 1.4 s, 6,000 x 30,000 in 2.2 s (was 29 s for 3,000 x 15,000). Trade-off: a lower payment with no invoice id and a fuzzy-only payer name is no longer found.
- Quoted multi-line CSV fields are read correctly.
- Missing or invalid client emails produce no draft, a `draft_status` column, a warning, and a banner in `review.html`. A missing `sender_email` warns loudly.
- Config is range-checked with clear errors (tolerance 0-20, thresholds 0-1, `fx_rates` must be numeric); arithmetic errors exit with code 2 instead of a traceback.
- Your edits are safe: `drafts.json` stores a sha256 per draft. A draft you edited is never deleted or overwritten (the new one is written as `<name>.new.eml` with a warning), and files the tool did not write are never touched.
- `--llm`: first name restricted to letters only, at most 3 words and 30 characters; other fields stripped of control characters; one-line notice when no key is found.
- The demo config leaves `pause_work_on_final` off, so the README samples show the defaults.
- README: screenshot, cross-platform quickstart, sample blocks generated from real output (`scripts/update_readme_samples.py`, checked by a test).

## 1.0.0 - 2026-10-25

First release.

- Parsers for Wise statement CSV, Payoneer transactions CSV, and any bank CSV through a column-mapping file. Tolerant header matching.
- Matching of payments to invoices with a confidence and a reason per match: invoice id in text, exact amount, fee/FX tolerance, fuzzy client name, date window, partial payments, optional FX rates.
- Low-confidence matches are marked "needs review" and are never counted as paid.
- Overdue ladder (day 1, 7, 14, 30) with 12 editable templates (4 stages x 3 tones).
- Optional one-line personalisation through a free Groq or Gemini key; sends only first name, amount, days overdue and invoice id.
- Outputs: `status.csv`, self-contained `review.html`, `drafts/*.eml`, `drafts.json`. Nothing is sent.
- `examples/n8n-weekly.json` and `docs/n8n.md`.
