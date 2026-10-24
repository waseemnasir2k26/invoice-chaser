# invoice-chaser: match payments to open invoices and draft polite overdue reminders, locally

![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)
![Python 3.10+](https://img.shields.io/badge/python-%3E%3D3.10-blue.svg)
![Dependencies: 0](https://img.shields.io/badge/dependencies-0-blue.svg)

**The client is three weeks late, chasing feels awkward, so you don't, and the money sits.**

You export your Wise or Payoneer statement, point this tool at your invoice list, and it works out which invoices are paid, which are partly paid, and which are overdue. For the overdue ones it writes a reminder draft in the right tone for how late they are. It never sends anything. Your invoices and bank data stay on your machine; there is no account and no SaaS login.

Built with freelancers and small agencies in mind, including Pakistani freelancers paid through Payoneer or Wise, where fees shave a little off every payment and the client's reference is often blank.

## 60-second quickstart

```bash
git clone <this repo>
cd invoice-chaser
pip install -e .

invoice-chaser demo          # or: python -m invoice_chaser demo
open out/review.html         # or just double-click it
```

Then on your own data:

```bash
invoice-chaser run \
  --invoices invoices.csv \
  --wise wise-statement.csv \
  --payoneer payoneer-transactions.csv \
  --bank bank-export.csv --mapping bank-mapping.json \
  --config invoice-chaser.json \
  --tone neutral --out out
```

Requires Python 3.10+. No dependencies. Install from a clone with `pip install .` (or `-e .` to hack on it). The reminder templates ship inside the package; pass `--templates <dir>` to use your own.

## What it does

1. **Reads** `invoices.csv` (`id, client, email, amount, currency, issued, due`, optional `status`, optional `contact` for the first name) and one or more payment exports.
2. **Matches** each payment to an invoice and gives every match a confidence and a plain-English reason.
3. **Classifies** each invoice: paid, partial, overdue, needs review, open (not due yet), void.
4. **Drafts** a reminder for overdue and partly-paid invoices, based on how many days late.
5. **Writes** `out/status.csv`, `out/review.html` and `out/drafts/*.eml`.

### How matching decides

| Signal                                                                                                       | Weight                                   |
| ------------------------------------------------------------------------------------------------------------ | ---------------------------------------- |
| Invoice id found in the payment text (`INV-1001`, `inv 1001`, `INV#1001`; `INV-12` does not match `INV-120`) | +0.60                                    |
| Amount equal, or within the fee/FX tolerance (default 2%)                                                    | +0.45 (lower amount +0.10, higher +0.05) |
| Client name similar to payer or description (`difflib`, company suffixes ignored)                            | up to +0.30                              |

Payments before the issue date, or more than 120 days after it, are ignored. A confidence of 0.70 or more counts as matched. Between 0.40 and 0.70 the invoice becomes **needs review** and is never marked paid; no reminder is drafted for it, so you don't chase someone who may have paid. All thresholds are in the config file.

Partial payments are tracked: a 1,000 payment against a 2,500 invoice leaves 1,500 outstanding, and later payments with the same reference add up.

### The overdue ladder

| Days overdue | Stage        | Intent                                                            |
| ------------ | ------------ | ----------------------------------------------------------------- |
| 1            | `nudge`      | Friendly reminder, assumes it slipped through                     |
| 7            | `firm`       | Asks for a payment date                                           |
| 14           | `final`      | Final reminder plus notice that work will be paused               |
| 30           | `escalation` | Asks for payment or a written plan, says you'll review next steps |

Templates are plain files, `invoice_chaser/templates/<stage>.<tone>.md`, with three tones (`friendly`, `neutral`, `firm`) per stage. Edit them freely. Variables: `{{first_name}} {{client}} {{invoice_id}} {{amount}} {{outstanding}} {{currency}} {{due_date}} {{days_overdue}} {{sender_name}} {{payment_details}} {{personal_line}}`. A misspelled variable stops the run instead of shipping literal braces. The escalation text does not threaten legal action; add that yourself if it is appropriate where you are.

### Optional: one personalised line

`--llm` with `GROQ_API_KEY` or `GEMINI_API_KEY` set asks a free-tier model for a single opening sentence (`{{personal_line}}`). The request contains only the client first name, the outstanding amount, days overdue and the invoice id. No email address, no company name, no other invoices. Without a key, or if the call fails, it is skipped and the template is used as written.

## Sample output

From `invoice-chaser demo` (synthetic data, dated 2026-10-25):

```
invoice-chaser 1.0.0  as of 2026-10-25
  needs_review=1  open=1  overdue=3  paid=3  partial=1
  INV-1002   partial          1500.00 USD   15d  final
  INV-1004   overdue          1800.00 EUR    4d  nudge
  INV-1005   overdue           640.00 GBP   40d  escalation
  INV-1006   needs_review      950.00 USD    7d  -
  INV-1008   overdue           500.00 USD   11d  firm
  4 draft(s) written (nothing sent) -> out/drafts
```

`INV-1003` was paid 786 of 800 through Payoneer (fees) and still counts as paid. `INV-1006` has a 950 payment from a different name and no reference, so it lands in needs review.

One of the drafts (`out/drafts/INV-1002-final.eml`, opens in any mail client as an unsent message):

```
To: accounts@northwind.example
Subject: Final reminder and work pause: invoice INV-1002

Hi Daniel,

Invoice INV-1002 for 1,500.00 USD is now 15 days overdue (due 2026-10-10). I would much rather keep working together.

Unless I hear from you with a payment date, I will need to pause work on current projects until the invoice is settled. I will resume as soon as it is.

Thank you for understanding,
Alex Rivera
```

`out/review.html` is one self-contained page (no scripts, no external requests) with totals per currency and a section each for needs review, overdue, partial, open, paid. Every value is HTML-escaped. `out/status.csv` has the same data for a spreadsheet, with cells starting `= + - @` neutralised.

## Input formats

| Source                    | Flag                   | Notes                                                                                                                                              |
| ------------------------- | ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| Wise statement CSV        | `--wise`               | Uses `Date`, `Amount`, `Currency`, `Description`, `Payment Reference`, `Payer Name`, `TransferWise ID`. Positive amounts only.                     |
| Payoneer transactions CSV | `--payoneer`           | Looks for a date, amount, currency, description, status and id column by alias. Pending, failed and negative rows are skipped.                     |
| Any bank CSV              | `--bank` + `--mapping` | You name the columns in a small JSON file, see `examples/bank-mapping.json`. Supports separate `credit` column, `date_format`, `default_currency`. |

Header matching ignores case, spaces and punctuation. If a required column is missing, the error lists the headers it found.

**Verify your own export.** The Wise column names are written from memory of Wise's statement CSV and were not checked against a fresh export; the Payoneer parser guesses by alias and its sample is synthetic. If your export uses different names, the generic `--bank` mapping works for any CSV and takes a minute to write. Please open an issue with the (redacted) header row if a real file fails.

## n8n route

`examples/n8n-weekly.json`: Schedule, Execute Command, Code, Gmail **Create Draft**. It drafts, it does not send. Setup and caveats in [docs/n8n.md](docs/n8n.md).

## Limits

- **It suggests, you decide.** Matching is heuristic. A wrong "paid" is possible when two invoices share an amount and a client; check `review.html` before you send anything.
- **No bank connection.** You download the statements; nothing logs in for you.
- **Cross-currency needs your rate.** Without an entry like `"EUR/USD": "1.08"` in `fx_rates`, payments in another currency are not matched. The tolerance covers fees, not large exchange swings.
- **Overpayments** are flagged for review unless the client name also matches.
- **Reminders are not legal notices.** The text is a starting point for politeness; it is not legal or financial advice, and late-payment rules differ by country and contract.
- **Drafts are rewritten each run** (`out/drafts/` is cleared first), and weekly runs will draft again for invoices still overdue.
- **No tracking of reminders already sent.** If you want "don't nag twice this week", mark the invoice or move the due date in `invoices.csv`.
- Date columns are parsed by trying common formats; ambiguous `03/04/2026` is read day-first. Set `date_format` in the bank mapping when it matters.
- The LLM call and the n8n workflow were not exercised against live services for this release.

## Files

```
invoice_chaser/   parsers, matching, ladder, templates, llm, drafts, report, cli
invoice_chaser/templates/  <stage>.<tone>.md (12 files)
examples/         synthetic invoices, Wise / Payoneer / bank samples, config, n8n workflow
tests/            pytest suite
```

Run the tests: `pip install -e ".[dev]" && python -m pytest`.

## For you if

- You invoice in USD, EUR or GBP and get paid through Wise, Payoneer or a bank, a few clients at a time.
- You have a spreadsheet of invoices and no accounting package, or you don't want to pay for one just to send reminders.
- You want drafts to read and send yourself, not a bot emailing your clients.

Not for you if you need automatic payment collection, payment links, tax handling or a client portal.

## Config

`invoice-chaser.json` (all keys optional): `sender_name`, `sender_email`, `payment_details`, `tone`, `fx_tolerance_pct`, `date_window_days`, `auto_threshold`, `review_threshold`, `stage_days`, `fx_rates`, `bank_mapping`. See `examples/invoice-chaser.json`.

## License

MIT, 2026 SkynetLabs.

---

Need this wired into your business? SkynetLabs builds voice agents, n8n back offices and sites that convert → https://skynetjoe.com
