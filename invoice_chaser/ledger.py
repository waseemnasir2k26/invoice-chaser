"""sent.json: a record of reminders you actually sent.

The tool never sends. After you send a draft yourself, run
`invoice-chaser mark-sent <invoice-id>` so the next run does not draft the
same stage again and can word the next reminder as a follow-up.

Format: {"INV-1001": [{"stage": "nudge", "date": "2026-10-08"}, ...]}
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from .ladder import STAGES


def load_ledger(path: str | Path) -> dict[str, list[dict]]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
        for inv, entries in data.items():
            for e in entries:
                if e["stage"] not in STAGES:
                    raise ValueError(f"unknown stage {e['stage']!r}")
                date.fromisoformat(e["date"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"{p}: not a valid sent ledger ({exc})") from exc
    return data


def record_sent(path: str | Path, invoice_id: str, stage: str, when: date) -> None:
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {', '.join(STAGES)}")
    p = Path(path)
    data = load_ledger(p)
    entries = data.setdefault(invoice_id, [])
    entry = {"stage": stage, "date": when.isoformat()}
    if entry not in entries:
        entries.append(entry)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def highest_sent(entries: list[dict] | None) -> tuple[int, str, str] | None:
    """(stage index, stage, date) of the most advanced stage already sent, or None."""
    if not entries:
        return None
    best = max(entries, key=lambda e: (STAGES.index(e["stage"]), e["date"]))
    return STAGES.index(best["stage"]), best["stage"], best["date"]


def last_sent_date(entries: list[dict] | None) -> date | None:
    if not entries:
        return None
    return max(date.fromisoformat(e["date"]) for e in entries)
