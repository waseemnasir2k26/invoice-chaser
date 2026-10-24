# Weekly drafts in n8n

`examples/n8n-weekly.json` runs invoice-chaser every Monday and creates Gmail **drafts**. It has no Send node, and invoice-chaser itself cannot send. You open Gmail, read each draft, and send the ones you want.

```
Schedule (Mon 09:00) -> Execute Command -> Code -> Gmail: Create Draft
```

## Setup

1. Install on the machine that runs n8n: `pip install -e .` inside the cloned repo.
2. Put your exports in a data folder, for example `data/invoices.csv`, `data/wise.csv`, `data/payoneer.csv`. Refresh the payment exports before each run (download the latest statement; nothing logs into Wise or Payoneer for you).
3. Import `examples/n8n-weekly.json` (Workflows -> Import from file).
4. Edit the command in **Run invoice-chaser** so the paths point at your folder.
5. Open **Create Gmail draft** and choose your Gmail OAuth credential.
6. Run it once manually and check the drafts in Gmail before you activate the schedule.

## What each node does

| Node               | Job                                                                                                       |
| ------------------ | --------------------------------------------------------------------------------------------------------- |
| Run invoice-chaser | Runs the CLI, discards its summary, prints `out/drafts.json` to stdout                                    |
| Parse drafts       | Turns that JSON into one n8n item per reminder (`to`, `subject`, `body`)                                  |
| Create Gmail draft | `draft -> create` with the recipient in the To field. There is no send operation anywhere in the workflow |

## Things to know

- **Execute Command may be off.** Recent n8n versions disable the Execute Command node by default. Re-enable it with the `NODES_EXCLUDE` environment variable only if you accept what that allows on your host. The alternative: schedule `invoice-chaser run` with cron or Windows Task Scheduler, and replace the first two nodes with a **Read/Write Files from Disk** node reading `out/drafts.json` plus **Extract From File** (JSON).
- **Not on n8n Cloud.** Cloud has no Execute Command node and no access to your local CSVs. Use the cron route above and a self-hosted or desktop n8n.
- **Duplicates.** Each weekly run drafts a reminder for every invoice still in the chase window. Delete drafts you no longer want, or mark invoices `paid` in `invoices.csv`. The workflow does not check for drafts it created last week.
- **Needs-review invoices get no draft.** They appear in `out/review.html` until you resolve them.
- **Untested on a live instance.** The JSON was written against the n8n node schemas (Schedule Trigger 1.2, Code 2, Gmail 2.1) and has not been imported into a running n8n for this release. If a field is renamed in your version, re-pick it in the node UI.
