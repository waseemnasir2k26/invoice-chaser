"""Write reminder drafts as .eml files. This module never sends anything:
no smtplib, no imaplib, no sockets. tests/test_outputs_never_send.py enforces that.

Your edits are safe: drafts.json records a sha256 for every file written. On the next run a
file whose hash still matches is ours and is replaced; a file you edited (hash differs) or
any file we did not write is never deleted or overwritten. The fresh draft is then written
next to it as <name>.new.eml and a warning says so."""
from __future__ import annotations

import hashlib
import json
import re
from email.message import EmailMessage
from pathlib import Path


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("._") or "invoice"


def one_line(s: str) -> str:
    """Header values never carry CR/LF (stops header injection from CSV data)."""
    return re.sub(r"[\r\n]+", " ", str(s)).strip()


def build_eml(to: str, sender_name: str, sender_email: str, subject: str, body: str) -> EmailMessage:
    to, sender_name, sender_email, subject = map(one_line, (to, sender_name, sender_email, subject))
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = f"{sender_name} <{sender_email}>"
    msg["Subject"] = subject
    msg["X-Unsent"] = "1"  # mail clients open this as an unsent draft
    msg.set_content(body)
    return msg


EMAIL_RE = re.compile(r"^[^@\s,;<>()\[\]\"]+@[^@\s,;<>()\[\]\".]+(\.[^@\s,;<>()\[\]\".]+)*\.[^@\s,;<>()\[\]\".]{2,}$")
PLACEHOLDER_FROM = "set-sender-email@example.invalid"


def valid_email(s: str) -> bool:
    return bool(s) and len(s) <= 254 and EMAIL_RE.match(s.strip()) is not None


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _previous(out_dir: Path) -> dict[str, str]:
    """{file name: sha256 or ""} from the drafts.json this tool wrote last time."""
    try:
        data = json.loads((Path(out_dir) / "drafts.json").read_text(encoding="utf-8-sig"))
        prev = {str(it.get("file", "")): str(it.get("sha256", "")) for it in data}
    except (OSError, ValueError, AttributeError, TypeError):
        return {}
    return {n: h for n, h in prev.items() if n.endswith(".eml") and Path(n).name == n}


def write_drafts(items: list[dict], out_dir: Path, warnings: list[str] | None = None) -> list[Path]:
    """items: dicts with invoice_id, stage, to, from_name, from_email, subject, body."""
    warnings = warnings if warnings is not None else []
    d = Path(out_dir) / "drafts"
    d.mkdir(parents=True, exist_ok=True)
    edited: set[str] = set()
    for name, digest in _previous(out_dir).items():
        old = d / name
        if not old.is_file():
            continue
        if digest and _sha(old) == digest:
            old.unlink()  # untouched since we wrote it
        else:
            edited.add(name.lower())  # you changed it (or it predates hashes): leave it alone
    paths = []
    used: set[str] = set()
    for it in items:
        msg = build_eml(it["to"], it["from_name"], it["from_email"] or PLACEHOLDER_FROM, it["subject"], it["body"])
        stem = f"{safe_name(it['invoice_id'])}-{it['stage']}"
        name, n = stem, 2
        if f"{stem}.eml".lower() in edited:
            name = f"{stem}.new"
            warnings.append(f"{stem}.eml was edited by you and was kept; the new draft is {name}.eml")
        # ids like A/1 and A_1 must not overwrite each other, nor any file we did not write
        base = name
        while name.lower() in used or (d / f"{name}.eml").exists():
            name, n = f"{base}-{n}", n + 1
        used.add(name.lower())
        p = d / f"{name}.eml"
        data = bytes(msg)
        p.write_bytes(data)
        it["file"] = p.name
        it["sha256"] = hashlib.sha256(data).hexdigest()
        paths.append(p)
    (Path(out_dir) / "drafts.json").write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    return paths
