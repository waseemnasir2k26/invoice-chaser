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

from .notices import Notice


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
CHECK_FIRST_DIR = "check-first"  # client reminders drafted for a held invoice: drafts/check-first/, never drafts/


def valid_email(s: str) -> bool:
    return bool(s) and len(s) <= 254 and EMAIL_RE.match(s.strip()) is not None


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_draft_name(n: str) -> bool:
    """A draft we may have written: `<name>.eml` in drafts/, or `check-first/<name>.eml`. Anything else is never touched."""
    parts = n.split("/")
    return (n.endswith(".eml") and "\\" not in n and all(p not in ("", ".", "..") for p in parts)
            and (len(parts) == 1 or (len(parts) == 2 and parts[0] == CHECK_FIRST_DIR)))


def _previous(out_dir: Path) -> dict[str, tuple[str, str]]:
    """{relative file name: (sha256 or "", invoice id)} from the drafts.json this tool wrote last time."""
    try:
        data = json.loads((Path(out_dir) / "drafts.json").read_text(encoding="utf-8-sig"))
        prev = {str(it.get("file", "")): (str(it.get("sha256", "")), str(it.get("invoice_id", ""))) for it in data}
    except (OSError, ValueError, AttributeError, TypeError):
        return {}
    return {n: v for n, v in prev.items() if _is_draft_name(n)}


def _load_kept(out_dir: Path) -> dict[str, str]:
    """{relative file name: invoice id} of drafts you edited. Remembered across runs so the warning repeats."""
    try:
        data = json.loads((Path(out_dir) / "kept.json").read_text(encoding="utf-8-sig"))
        return {str(k): str(v) for k, v in data.items() if _is_draft_name(str(k))}
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


def write_drafts(items: list[dict], out_dir: Path, warnings: list[str] | None = None,
                 settled_ids: set[str] | None = None) -> list[Path]:
    """items: dicts with invoice_id, stage, to, from_name, from_email, subject, body.

    settled_ids: lower-case ids of invoices that are now paid or void. An edited draft of such an
    invoice is moved to drafts/stale/ (never deleted)."""
    warnings = warnings if warnings is not None else []
    settled_ids = settled_ids or set()
    d = Path(out_dir) / "drafts"
    d.mkdir(parents=True, exist_ok=True)
    kept = _load_kept(out_dir)
    for name, (digest, inv_id) in _previous(out_dir).items():
        old = d / name
        if not old.is_file():
            continue
        if digest and _sha(old) == digest:
            old.unlink()  # untouched since we wrote it
        else:
            kept[name] = inv_id  # you changed it (or it predates hashes): never delete it
    edited: set[str] = set()
    for name, inv_id in list(kept.items()):
        f = d / name
        if not f.is_file():
            del kept[name]
        elif inv_id.lower() in settled_ids:
            stale = d / "stale"
            stale.mkdir(exist_ok=True)
            target, n = stale / Path(name).name, 2
            while target.exists():
                target = stale / f"{Path(name).stem}-{n}.eml"
                n += 1
            f.replace(target)
            del kept[name]
            warnings.append(Notice(f"{name}: invoice {inv_id} is now paid, so your edited draft was moved to drafts/stale/ "
                                   f"(not deleted)", "DRAFT_STALE"))
        else:
            edited.add(name.lower())
    reported: set[str] = set()
    paths = []
    used: set[str] = set()
    for it in items:
        msg = build_eml(it["to"], it["from_name"], it["from_email"] or PLACEHOLDER_FROM, it["subject"], it["body"])
        sub = CHECK_FIRST_DIR if it.get("check_first") else ""  # a CHECK FIRST reminder lives in drafts/check-first/
        rel = (lambda nm: f"{sub}/{nm}.eml") if sub else (lambda nm: f"{nm}.eml")
        stem = f"{safe_name(it['invoice_id'])}-{it['stage']}"
        name, n = stem, 2
        if rel(stem).lower() in edited:
            name = f"{stem}.new"
            reported.add(rel(stem).lower())
            warnings.append(Notice(f"{rel(stem)} was edited by you and was kept; the new draft is {rel(name)}", "DRAFT_KEPT"))
        # ids like A/1 and A_1 must not overwrite each other, nor any file we did not write
        base = name
        while rel(name).lower() in used or (d / rel(name)).exists():
            name, n = f"{base}-{n}", n + 1
        used.add(rel(name).lower())
        p = d / rel(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = bytes(msg)
        p.write_bytes(data)
        it["file"] = rel(name)
        it["sha256"] = hashlib.sha256(data).hexdigest()
        paths.append(p)
    for name in sorted(kept):
        if name.lower() not in reported:
            warnings.append(Notice(f"{name} was edited by you and is kept (it is never deleted or overwritten)", "DRAFT_KEPT"))
    (Path(out_dir) / "drafts.json").write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    (Path(out_dir) / "kept.json").write_text(json.dumps(kept, indent=2, ensure_ascii=False), encoding="utf-8")
    return paths
