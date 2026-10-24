# Changelog

## 1.0.1 - 2026-10-25

Fixes from the first review round.

- `pip install .` then `invoice-chaser demo` works from any folder: demo data now ships inside the package.
- Reminder templates no longer assert history the tool cannot know. Drafts say nothing about earlier reminders or paused work unless you unlock it: `sent.json` ledger, a `last_reminder` column, `previous_reminders: true`, and `pause_work_on_final: true`. New `reply_within_days` setting.
- New `invoice-chaser mark-sent <invoice> [--stage] [--date]` and `sent.json` ledger: a sent stage is never drafted twice, and follow-ups cite the date of your last reminder.
- Matching is indexed (amount band, invoice-id token, name prefix) instead of all-pairs: 1,000 invoices x 5,000 payments in about 2 s (was minutes). The fuzzy name check now needs a shared 4-letter word prefix.
- Quoted multi-line CSV fields are read correctly.
- Missing or invalid client emails produce no draft, a `draft_status` column, a warning, and a banner in `review.html`. A missing `sender_email` warns loudly.
- Config is range-checked with clear errors (tolerance 0-20, thresholds 0-1, `fx_rates` must be numeric); arithmetic errors exit with code 2 instead of a traceback.
- Drafts folder: only files listed in the previous `drafts.json` are removed; other files are never touched or overwritten.
- `--llm`: first name restricted to letters, spaces and hyphens (max 40), other fields stripped of control characters; one-line notice when no key is found.
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
