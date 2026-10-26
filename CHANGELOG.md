# Changelog

## 1.1.12 - 2026-10-26

- `confirm INV --payment ID` and `rebind` refuse a payment whose text names a different known invoice id, unless `--force` (then recorded with a warning).

## 1.1.11 - 2026-10-26

Two blockers from the judge review: a renamed-and-edited invoice and a double payment across two exports of one source no longer slip through; notes to you live apart from client drafts.

- **Renamed and edited is still an orphan (blocker).** When a confirmed invoice id is gone from invoices.csv, every current invoice that may be it is held as an `ORPHANED_DECISION` candidate (CRITICAL, `run` exits 2, no client draft, and the exact `rebind` / `forget` commands). "May be it" is any one of: the same client (`company_words`: punctuation, capitals, spacing, a leading "The" and the legal suffix ignored); the same amount and currency; an id within two edits of the old id (`edit_distance`). The gate uses the same test (`gate.renamed_match`), so `Renamed Co` to `Renamed Co.`, `Renamed  Co`, or 350 to 400 get no client draft. Deliberately wide: short ids such as `INV-A` and `INV-N` are one edit apart, so one orphan can hold a few similar invoices.
- **A payment hold is never hidden by an orphan hold (fix).** The double-count and refund-name holds were built from the matches after the orphan branch had cleared them. They are now built from the evidence captured before any decision clears it, so STUCK_HOLD still fires for them.
- **Same payment in two files of one source (blocker).** Two Wise statements (or two bank exports) of one payment are now a possible double count, held in needs review: (a) the same transfer id with identical data in two different files is kept on both rows instead of silently dropping one; (b) identical amount and currency, within a day, the same payer company, in two different files of one source is flagged. Rows inside ONE file are counted separately as before, and a repeated real id inside one file is still dropped with its warning. Payer matching across files uses the same looser company words as the gate. `Payment.file_no` records which file a row came from.
- **Partial remainders (ruled).** A remainder reminder after a partial confirmation is correct and stays. The README says so.
- **Internal notes in `drafts/internal/` (minor).** The `-check.eml` notes to you are written to `out/drafts/internal/`, not `out/drafts/`, so they cannot be mistaken for client drafts. The README, the review page, `mark-sent` and the tests use the new folder. An unedited 1.1.10 note in `drafts/` is removed on the next run.

## 1.1.10 - 2026-10-26

One gate for every client draft, and the stuck-hold cases that were still silent.

- **One gate (`client_draft_allowed`, `invoice_chaser/gate.py`).** Every client-addressed draft, normal reminders and CHECK FIRST alike, is built by one function that asks the gate first. The gate refuses when: the invoice id (any capitals) has a confirmation or mark-paid while held; an orphaned decision names the invoice; the invoice is held as PAYMENT_DOUBLE_BOUND, a possible double count, or a refund-looking client name; or a confirmed invoice whose old id is gone has the same client, amount and currency. The 1.1.9 blocker is closed: INV-A confirmed on 2026-09-20 and renamed INV-A2 gets no client draft (normal or CHECK FIRST) when run on 2027-06-30. A structural test fails if a new function builds a client draft without asking the gate, and a monkeypatched gate writes zero client drafts across a scenario with every hold type.
- **A part-payment is not blocked.** A confirmed part-payment still gets its normal reminder, unless the invoice is held.
- **Renamed confirmed invoices are matched by snapshot only when the old id is gone.** A live invoice with an identical twin is never blocked.
- **STUCK_HOLD (critical).** A held invoice whose hold is a possible double count or a refund-looking client name, past the stuck window, gets no client reminder and prints `CRITICAL [STUCK_HOLD]` naming the invoice and days overdue on every run; `run` exits 2.
- **mark-paid warns.** If a confirmed payment with the same client and amount already backs another invoice, `mark-paid` prints a warning first. It does not block; the override is allowed.
- **Verifiable count.** `pytest -s` on the adversarial README test prints the measured case count.

## 1.1.9 - 2026-10-26

Judge review fixes: a confirmed payment is never chased, and held invoices show their real age.

- **No client draft for a confirmed invoice that is held.** An invoice you confirmed or marked paid, whose ledger entry no longer matches (`CHANGED_INVOICE`, an orphaned decision, a payment that backs two invoices), gets no client reminder of any kind: no normal draft and no `[CHECK FIRST]` draft. It gets only the internal `-check.eml` note, which now reads "You confirmed this paid on <date>, but the invoice changed: re-confirm or unmark." (A confirmed invoice that is simply part-paid and still owed keeps its normal reminder.)
- **Real days overdue on every held invoice.** A held invoice showed `0` days overdue in `status.csv` when a ledger confirmation or a double-bound payment had put it on hold. It now shows the real days overdue. The hold reason is in a new last column, `hold_reason`, for every invoice waiting for your decision.
- **Speed test tolerant of slow runners.** `test_naming_every_invoice_is_fast` makes one warm-up run before timing and gets twice the budget on Windows and on CI.

## 1.1.8 - 2026-10-26

A stuck invoice is no longer silent to its client forever, and two small fixes from the judge review.

- **CHECK FIRST reminders for stuck invoices.** An invoice held for your decision (suggested, needs review, or an orphaned decision) that has waited longer than `stuck_after_days` AND is more than `stuck_overdue_days` overdue also gets the normal client reminder for its stage. It is written to `out/drafts/check-first/` (not `out/drafts/`), the subject starts with `[CHECK FIRST] `, the first body line says `CHECK FIRST: a possible payment was found (<payment id>, <date>, <amount>, payer <payer>). Verify it before sending this.`, and `drafts.json` marks it `check_first: true`. The same send-blockers as an ordinary draft apply (missing or invalid email, a stage already sent). The internal `-check.eml` note to you is unchanged.
- **No client draft when the money may have arrived.** An invoice held because a payment backs two invoices (`PAYMENT_DOUBLE_BOUND`), or because its possible payment is a double count of another export or a repeat of a confirmed payment, gets no client reminder at all.
- **`mark-sent --checked`.** Recording a CHECK FIRST reminder as sent is refused unless you pass `--checked` (you verified the possible payment first).
- **`review.html`** has a "Check-first reminders" section listing these drafts.
- **A repeat `rebind` says so.** Rebinding a pair that an earlier `rebind` already resolved prints `nothing changed: ... is already rebound` and exits 1, instead of "is not an orphaned decision".
- **Regression: bank amounts with a decimal comma.** `1.200,00` on a 1200 EUR invoice is read as 1200.00 and matched (test added).

## 1.1.7 - 2026-10-26

Ledger invariants, checked on every run and before every write.

- **A payment backs one invoice.** If the ledger ever has one payment key behind two invoices, `run` raises a CRITICAL `PAYMENT_DOUBLE_BOUND` warning naming both, holds both (no reminder, not counted paid, shown as `held:`), and prints `invoice-chaser unbind <invoice> <payment>` for each. New command `unbind` removes one invoice's confirmation for that payment (the invoice goes back to a fresh suggestion; the other keeps its own). `confirm` and `rebind` refuse a payment that already backs any invoice. A write that would add a second binding is refused and writes nothing.
- **Every ledger write is read back.** The file is re-read after the write and must show the change. A command that changes nothing (a repeated `mark-sent`, `reject`, `mark-paid`, `forget` of something absent) prints `nothing changed` and exits 1.
- **`rebind` and `forget` look at today's exports**, not at a saved `orphans.json`: the last run's inputs are re-read at the moment you run the command. Orphans are keyed by kind, old id and new id, so identical invoices renamed `X1` and `X2` can both be candidates: `rebind INV-A X1` takes one and drops the other. New command `orphans` lists the pending ones with their exact commands.
- **Nothing is overwritten.** A rename or rebind whose target already has a decision (a confirmation, rejection, sent reminder, or a payment already bound or rejected) is refused with the reason.
- **Exit code 2 when a CRITICAL notice exists.** `run` returns 2 whenever any critical notice is raised (documented in the README under Exit codes). A held invoice is shown as `held: <reason>` where it used to show `0d`.
- `generated_on` in `suggestions.json` and `orphans.json` follows `--today` (it used the wall clock, so a run dated in the past could not be confirmed).

## 1.1.6 - 2026-10-25

Simplify: automatic repair was a new way to be wrong, so it is gone.

- **No automatic re-binding.** A confirmation or rejection that points at something missing from today's data (a payment id that changed, an invoice id you renamed) is now a critical `ORPHANED_DECISION` warning when a look-alike exists: what you decided, the invoice snapshot, the candidates with a safety check, and the exact commands. The invoices involved (and any invoice a candidate names) are held in needs review with no reminder, and the candidates are not suggested for anything, until you resolve it with `invoice-chaser rebind <old> <new>` (same checks as the repeat check: a candidate that names another invoice, or has a different bank transaction id, is refused) or `invoice-chaser forget <old>`. This closes the path where a confirmation was attached to a payment naming another invoice and a paid client was chased. A confirmed payment that is merely absent from a shorter export is not an orphan and stays quiet.
- **Warnings have codes.** Each warning is raised with a code and severity (critical / warning / routine); nothing classifies by reading the text, so payer-controlled text such as "Evil (row skipped)" cannot make a critical warning look routine. A dropped invoice row is critical (`INVOICE_ROW_DROPPED`, named by line and id), a dropped payment row is a warning. The summary counts by code.
- After `confirm --payment`, the confirmed payments are consumed (the cross-export hold does not fire again) and the other half of the pair is printed with the exact `reject` and `confirm --payment` commands.
- `confirm --payment` and `reject` refuse an id that exists in two exports and list the source-qualified ids (`wise:X-1`, `bank:X-1`); source-qualified ids are accepted.
- `ledger reset --backup` refuses a path with no ledger, never overwrites an earlier backup (timestamp plus a counter), and compares the copy with the original byte for byte before resetting.
- `mark-paid`, `confirm`, `reject` and `mark-sent` refuse a duplicate invoice id (`INV-6001` / `inv-6001`): fix invoices.csv first.

## 1.1.5 - 2026-10-25

- **A ledger schema, and no pre-release migrations.** No version had been published, so the migration code for unreleased formats is gone. `sent.json` and `seen.json` carry `"schema": 1`; a ledger with no schema, an older or an unknown one is refused with a clear message ("ledger from a pre-release build ...") instead of being guessed at. New command `invoice-chaser ledger reset --backup` starts fresh and keeps a copy. Future versions migrate by fingerprint.
- **Decisions follow their payment.** A confirmation or rejection whose payment id is no longer in your exports, but whose fingerprint (date, amount, currency, payer company) matches a payment that is, is re-bound to it with a warning, and the ledger is updated. A change in how ids are made can no longer silently drop a decision.
- **The same payment in two exports under different payer names** ("Acme Ltd" in Wise, "Jane Okafor" in the bank file): same amount and currency, within two days, same invoice id named in both (or both payers blank): both are held in needs review. `confirm INV --payment ID` names the payment(s) you checked; the invoice is never counted twice.
- **`INV-6001` and `inv-6001`** are no longer merged: both are held in needs review ("duplicate invoice id (case differs)"), no reminder to either client until the ids are unique.
- One warning summary instead of a wall of text: decisions needed are printed in full, routine notices are counted, everything is in `review.html`; the missing `sender_email` warning is said once per output folder. An empty invoice file says "no invoices found" and why.
- ruff safe fixes applied to the package.

## 1.1.4 - 2026-10-25

- **A confirmed payment is only consumed when it really is that payment.** Matching the id was not enough: a bank can reuse a transaction id, and two rows can end up with the same id. A payment is now dropped as "already confirmed" only when its id AND its date, amount, currency and payer (as a company) agree with what the ledger remembered. Any mismatch keeps the payment, holds it in needs review as "id collision with a payment you confirmed for INV-x", and prints a loud warning with the next step. A paid client can no longer be chased because another client's payment was swallowed. The same check applies to rejections (stored with a fingerprint now; an older rejection without one is reported and the payment is suggested again until you reject it once more) and to `confirm`.
- **The id design, once.** A hash id is H(source, date, amount, currency, normalised description, the payer as a company), always, plus an occurrence index counting only rows with exactly the same full key. The id never depends on how many other rows exist, so a lone row keeps its id when a same-day sibling appears (a reject no longer silently comes undone), and `ACME LTD` / `Acme Limited` still get the same id while `Acme Inc` does not.
- Every hold warning now says what to do next.
- README: the exact id rule, a Limits bullet on the deposit-remainder hold with the `mark-paid` advice, and the `review.html` screenshot directly under the quickstart.

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
