"""Keep the sample output in README.md identical to what the demo really prints.

    python scripts/update_readme_samples.py          # rewrite the sample blocks
    python scripts/update_readme_samples.py --check  # exit 1 if README has drifted (tests do this)

README blocks look like:
    <!-- sample:NAME -->
    ```
    ...generated...
    ```
    <!-- /sample -->
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import sys
import tempfile
from email import message_from_bytes, policy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
BLOCK = re.compile(r"(<!-- sample:(\w[\w-]*) -->\s*```[a-z]*\n)(.*?)(```\s*<!-- /sample -->)", re.DOTALL)


def build_samples() -> dict[str, str]:
    sys.path.insert(0, str(ROOT))
    from invoice_chaser.cli import main

    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                assert main(["demo", "--out", "out"]) == 0
            msg = message_from_bytes((Path(tmp) / "out" / "drafts" / "INV-1002-final.eml").read_bytes(), policy=policy.default)
            head = "".join(f"{k}: {msg[k]}\n" for k in ("To", "From", "Subject", "X-Unsent"))
            eml = head + "\n" + msg.get_content()
        finally:
            os.chdir(cwd)
    return {"demo-output": buf.getvalue().replace("\\", "/"), "eml": eml}


def apply(text: str, samples: dict[str, str]) -> str:
    def sub(m):
        name = m.group(2)
        if name not in samples:
            raise SystemExit(f"README has unknown sample block {name!r}")
        return m.group(1) + samples[name].rstrip("\n") + "\n" + m.group(4)

    return BLOCK.sub(sub, text)


def check() -> bool:
    text = README.read_text(encoding="utf-8")
    return apply(text, build_samples()) == text


def main() -> int:
    text = README.read_text(encoding="utf-8")
    new = apply(text, build_samples())
    if "--check" in sys.argv:
        print("README samples are current" if new == text else "README samples have drifted: run scripts/update_readme_samples.py")
        return 0 if new == text else 1
    README.write_text(new, encoding="utf-8")
    print("README samples updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
