"""Render docs/flow.png: the four steps of the loop, from REAL output of the demo (maintainer tool, not a dependency).

    pip install playwright          # uses your installed Chrome, no browser download
    python scripts/make_strip.py
"""
from __future__ import annotations

import base64
import html
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHROME = os.environ.get("CHROME_PATH", r"C:/Program Files/Google/Chrome/Application/chrome.exe")


def cli(tmp: str, *args: str) -> str:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run([sys.executable, "-m", "invoice_chaser", *args], cwd=tmp, env=env, capture_output=True, text=True, encoding="utf-8")
    return r.stdout


def shorten(text: str, keep: int) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[:keep] + (["  ..."] if len(lines) > keep else []))


def main() -> int:
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        run1 = cli(tmp, "demo", "--keep-ledger")
        browser = pw.chromium.launch(executable_path=CHROME, headless=True)
        page = browser.new_context(viewport={"width": 1100, "height": 900}, device_scale_factor=2).new_page()
        page.goto((Path(tmp) / "out" / "review.html").as_uri())
        box = page.evaluate("""() => { const c = document.querySelector('.confirm'); const w = c.nextElementSibling;
            const a = c.getBoundingClientRect(), b = w.getBoundingClientRect();
            return {x: 0, y: a.top + scrollY - 8, width: 1100, height: b.bottom - a.top + 16}; }""")
        shot = base64.b64encode(page.screenshot(clip=box)).decode()
        confirm = cli(tmp, "confirm", "--all-strong", "--yes")
        run2 = cli(tmp, "demo", "--keep-ledger")

        def term(title: str, body: str) -> str:
            return f"<section><h2>{html.escape(title)}</h2><pre>{html.escape(body)}</pre></section>"

        doc = f"""<!doctype html><meta charset=utf-8><style>
body{{margin:0;padding:20px;background:#f5f5f4;font:18px/1.4 system-ui,sans-serif;color:#1c1917;width:1500px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:22px}}
section{{background:#fff;border:1px solid #d6d3d1;border-radius:10px;padding:14px 16px;overflow:hidden}}
h2{{margin:0 0 10px;font-size:21px}}pre{{margin:0;background:#1c1917;color:#e7e5e4;padding:12px;border-radius:6px;font:18px/1.45 ui-monospace,Consolas,monospace;white-space:pre-wrap}}
img{{width:100%;border:1px solid #d6d3d1;border-radius:6px}}</style>
<div class=grid>
{term("1. invoice-chaser demo   (finds the payments, shows its reasons, drafts nothing for suggestions)", shorten(run1, 9))}
<section><h2>2. out/review.html   ("Confirm these": one command per suggestion)</h2><img src="data:image/png;base64,{shot}"></section>
{term("3. invoice-chaser confirm --all-strong --yes   (only this writes paid)", shorten(confirm, 8))}
{term("4. run again   (confirmed invoices are paid; nothing was marked paid on a guess)", shorten(run2, 9))}
</div>"""
        page2 = browser.new_context(viewport={"width": 1556, "height": 900}, device_scale_factor=2).new_page()
        page2.set_content(doc)
        dest = ROOT / "docs" / "flow.png"
        dest.parent.mkdir(exist_ok=True)
        page2.locator(".grid").screenshot(path=str(dest))
        browser.close()
    print(f"wrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
