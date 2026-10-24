# Changelog

## 1.1.3 - 2026-10-25

- **Two real payments are not "repeats".** A payment is only held as a possible repeat of a confirmed one when nothing tells them apart: two different bank transaction ids, or a text that names a different invoice, now mean two payments. Same-day equal payments for two invoices can both be confirmed. A part-payment remainder equal to the deposit with no distinguishing reference is still held (it could be a re-export), and the message says so and offers `mark-paid`; it never suggests `reject` as the remedy.
- `confirm --all-strong` runs every check for every invoice first, then writes the ledger once, atomically: all or nothing, with the full list of what held it back.
- **A renamed payer** (ACME LTD to Acme Limited) no longer changes a payment's id (the payer is left out of the hash; it only breaks ties between rows that are otherwise identical) and is recognised as the same company when comparing a repeat. A fully-paid suggestion that only adds up because of an earlier confirmed part-payment is flagged.
- **Durable files.** The ledger is flushed to disk before it replaces the old one, keeps a `.bak` of the previous version, and cleans temp files left by a killed run. `seen.json` is written the same way; a damaged one is kept as `seen.json.corrupt-<date>` with a warning instead of silently restarting every waiting time.
- **Hidden refund words.** The refund and failure check strips invisible characters (zero-width space, soft hyphen, word joiner), folds accents and look-alike Cyrillic and Greek letters, and joins spaced-out letters ("r e f u n d"). Invisible characters are shown as <ZWSP> in the confirm display and in `review.html`.
- README: a first screen of one hook line, a "what it will not do" table, three commands and the loop strip (larger text); a note that timestamps ignore the UTC offset.

## 1.1.2 - 2026-10-25

- **The re-export trap** (new README section). A confirmed payment is remembered by date, amount, currency and payer as well as by id. A payment that looks like it after a cosmetic re-export is held in needs review as a possible repeat, never counted again, and `confirm` refuses it. Payment ids ignore punctuation, case and spacing.
- Identical rows in two exports (or the same file twice) are kept as separate payments and flagged for review instead of being merged into one; identical rows inside one file stay two payments.
- A client whose own name contains a refund or failure word (Refund Ltd, Pending Ventures, Chargebacks911) is never matched automatically: "client name contains a refund word: please check". The client's name is no longer cut out of the text, which fixes short names (Re, Un, Ac) mangling words.
- Stuck aging is keyed by invoice and keeps the earliest date, clamped to the real date on this computer, so a future `--today` cannot poison it and a flip between "suggested" and "needs review" keeps the clock running. New `stuck_overdue_days` (default 30). The note to you says "first seen today" instead of "0 day(s)" and shows the days overdue.
- A migration that drops row-numbered payment references now rolls back the confirmed part-payment tied to them and marks the invoice "re-confirm after upgrade" (never chased for the full amount meanwhile), so a payment cannot be counted twice. `unmark-paid` clears the flag.
- Nothing shareable names a folder on your computer: `review.html`, `status.csv`, `suggestions.json` and the drafts carry file names only. Full paths live in `out/local-state.json`, for `confirm` on this computer. A test scans every output file for absolute paths.
- Copy-paste commands quote ids for your shell (PowerShell single quotes on Windows, POSIX quotes elsewhere); ids with control characters get a harmless placeholder.
- README: the loop as a four-panel strip (`docs/flow.png`, generated from real output by `scripts/make_strip.py`).

## 1.1.1 - 2026-10-25

Fixes from the first review of 1.1.0.

- **Stable payment ids.** A payment's id is its real transaction id, or a hash of source, date, amount, currency, description and payer. Never a row number: after a re-export, a row-numbered id could land on another client's payment, mark it "consumed" and earn a paid client a firm reminder. Old row-keyed ledger entries are dropped with a warning that stays visible in `review.html` (a confirmed invoice stays confirmed).
- **No more parking.** A suggestion or review that waits longer than `stuck_after_days` (default 7), or belongs to an invoice more than 30 days overdue, is flagged STUCK: banner at the top of `review.html`, a line in the command-line summary, and an internal "check this one" note drafted to you (never to the client). First-seen dates are kept in `out/seen.json`. After a reject, the reason says so.
- `confirm` re-validates: refused when the suggestions are from another day, when the exports changed, or when the ledger changed for that invoice (a reject, confirm or mark-paid since the last run). `confirm --all-strong` shows each payment's payer and text, and ends cleanly on EOF.
- Refund and failure wording: 231 entries across twenty-plus languages (Korean, Hindi, Indonesian, Swedish, Danish, Norwegian, Vietnamese, Thai, Hebrew, Czech, Romanian, Hungarian, Greek, Finnish, Bengali, Persian, German, Spanish, Portuguese, French, Italian, Dutch, Polish, Russian, Turkish, Arabic, Urdu, Chinese, Japanese and English phrases such as "money back", "on hold", "not paid", "wasn't received", "didn't go through"). Short words match as whole words only (Stornoway, Returnly, Voidstar, Kreditech are fine), and the client's own name is removed from the text first (Pending Ventures, Chargebacks911). The README list is generated from the code.
- The ledger is written under a lock file with unique temp names and an atomic replace: twelve parallel `mark-sent` runs keep all twelve entries. Hand-edited NaN / Infinity / negative received amounts give a clean error.
- The snapshot warning now gives advice that works (`unmark-paid`, run again). `demo` starts from a fresh temporary ledger each run (`--keep-ledger` to keep decisions). `review.html` shows no absolute paths. The README states how the adversarial count is measured.

## 1.1.0 - 2026-10-25

**Product change: the tool no longer marks anything paid by itself.** Review kept finding a new false-paid class each time (non-English refund words, "payment failed", an earlier settled invoice, ids glued across fields, future dates, short ids like B2, ids inside e-mail addresses, a confirmation that outlived a changed invoice). Piling on rules cannot make an automatic "paid" safe on messy bank text, so the decision is split.

- New statuses: `suggested-paid` and `partial-suggested` (strong evidence, shown with the reason, the confidence and the exact payment row), `needs-review`, `overdue`, `open`. `paid` appears only if `invoices.csv` says so or you ran `confirm` / `mark-paid`.
- New commands: `confirm INV` (and `confirm --all-strong`, which prints the list and asks y/N unless `--yes`), `reject INV PAYMENT` (never suggested again). `mark-paid` and `unmark-paid` stay.
- No reminder is ever drafted for an invoice that has a suggested payment: it waits for your confirm or reject. Reminders only for `overdue`, and for the remainder of a part-payment you confirmed.
- The ledger stores a snapshot (client, amount, currency, due) with every confirmation. If `invoices.csv` later differs, the invoice goes back to needs-review with a warning ("invoice changed since you confirmed it").
- `review.html` opens with a "Confirm these" section: one copy-paste command per suggestion. New `out/suggestions.json`.
- The evidence table still ranks suggestions, and the classes found in review are fixed: refund, failure and non-receipt wording in many languages (list generated into the README from the code); description and payer searched separately, never glued; future-dated payments held; ids need 2+ letters and 2+ digits or a configured prefix (`ref_prefixes`, default INV and INVOICE); ids inside e-mail addresses and URLs ignored; an id-like token missing from the invoice list ("INV-9999") holds the payment; settled invoices stay in the ambiguity check for the same client and price; two clients with the same name (different e-mail addresses) are never matched by name.
- Candidates are capped everywhere: a payment naming more than 3 known invoices is never paired (those invoices go to review, "names more than 3 invoices at once"); an invoice named by more than 20 payments, or sharing its exact amount with more than 20, goes to review in bulk. 500 invoices x 2,000 payments that all name every invoice finish in about 1.5 s.
- Tests: a mutation-backed test per class (the scenario is refused by the real rule and accepted when only that rule is switched off), a property test asserting the engine never outputs `paid`, and 3,360 executed adversarial scenarios (counted by running them, not by formula; the README states the number and a test keeps it in sync).

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
