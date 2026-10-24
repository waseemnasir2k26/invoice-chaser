"""Write reminder drafts as .eml files. This module never sends anything:
no smtplib, no imaplib, no sockets. tests/test_never_send.py enforces that."""
from __future__ import annotations

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


def write_drafts(items: list[dict], out_dir: Path) -> list[Path]:
    """items: dicts with invoice_id, stage, to, from_name, from_email, subject, body."""
    d = Path(out_dir) / "drafts"
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob("*.eml"):
        old.unlink()
    paths = []
    used: set[str] = set()
    for it in items:
        msg = build_eml(it["to"], it["from_name"], it["from_email"], it["subject"], it["body"])
        stem = f"{safe_name(it['invoice_id'])}-{it['stage']}"
        name, n = stem, 2
        while name.lower() in used:  # ids like A/1 and A_1 must not overwrite each other
            name, n = f"{stem}-{n}", n + 1
        used.add(name.lower())
        p = d / f"{name}.eml"
        p.write_bytes(bytes(msg))
        it["file"] = p.name
        paths.append(p)
    (Path(out_dir) / "drafts.json").write_text(
        json.dumps([{k: v for k, v in it.items()} for it in items], indent=2, ensure_ascii=False), encoding="utf-8")
    return paths
