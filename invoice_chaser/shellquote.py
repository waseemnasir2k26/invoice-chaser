"""Quote an invoice id for the command a person will paste: PowerShell on Windows, POSIX shells elsewhere."""
from __future__ import annotations

import os
import re
import shlex

_SAFE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def quote(s: str, windows: bool | None = None) -> str:
    """Safe to paste. PowerShell: single quotes, a quote inside doubled. POSIX: shlex.quote. An id with control
    characters cannot be quoted safely, so a harmless placeholder is shown instead (rename it in invoices.csv)."""
    if _CONTROL.search(s):
        return "'<id-with-control-characters-rename-it-in-invoices.csv>'"
    if _SAFE.match(s):
        return s
    windows = os.name == "nt" if windows is None else windows
    if windows:
        return "'" + s.replace("'", "''") + "'"
    return shlex.quote(s)
