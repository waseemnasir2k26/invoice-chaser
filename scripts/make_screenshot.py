"""Render the demo's review.html to docs/review.png (2x). Maintainer tool, not a dependency:

    pip install playwright          # uses your installed Chrome, no browser download
    python scripts/make_screenshot.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHROME = os.environ.get("CHROME_PATH", r"C:/Program Files/Google/Chrome/Application/chrome.exe")


def main() -> int:
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as tmp:
        # run from the temp folder with the default --out so the page shows the same commands a user sees
        subprocess.run([sys.executable, "-m", "invoice_chaser", "demo"], check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, cwd=tmp, env={**os.environ, "PYTHONPATH": str(ROOT)})
        html = Path(tmp) / "out" / "review.html"
        dest = ROOT / "docs" / "review.png"
        dest.parent.mkdir(exist_ok=True)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=CHROME, headless=True)
            page = browser.new_context(viewport={"width": 1100, "height": 1180}, device_scale_factor=2).new_page()
            page.goto(html.as_uri())
            page.screenshot(path=str(dest))  # top of the page: totals + the sections that need action
            browser.close()
    print(f"wrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
