"""sent.json: a record of what YOU did outside the tool.

The tool never sends and never decides "paid" on a shaky match. Two things are recorded here:

  sent  reminders you actually sent (`invoice-chaser mark-sent INV-1`): the same stage is
        never drafted twice, and the next draft can say "following my reminder of <date>".
  paid  invoices you confirmed as paid (`invoice-chaser mark-paid INV-1`), e.g. after
        reviewing a "needs review" row.

Format: {"sent": {"INV-1": [{"stage": "nudge", "date": "2026-10-08"}]},
         "paid": {"INV-2": {"date": "2026-10-20", "note": "..."}}}
A legacy flat file ({"INV-1": [...]}) is still read as the "sent" section.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from .ladder import STAGES


def _read(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {"sent": {}, "paid": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
        if "sent" not in data and "paid" not in data:  # legacy flat format
            data = {"sent": data, "paid": {}}
        sent, paid = data.get("sent", {}), data.get("paid", {})
        if not isinstance(sent, dict) or not isinstance(paid, dict):
            raise ValueError("'sent' and 'paid' must be objects")
        for entries in sent.values():
            for e in entries:
                if e["stage"] not in STAGES:
                    raise ValueError(f"unknown stage {e['stage']!r}")
                date.fromisoformat(e["date"])
        for e in paid.values():
            date.fromisoformat(e["date"])
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"{p}: not a valid sent ledger ({exc})") from exc
    return {"sent": sent, "paid": paid}


def _write(path: str | Path, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def load_ledger(path: str | Path) -> dict[str, list[dict]]:
    """The 'sent' section: invoice id -> [{stage, date}]."""
    return _read(path)["sent"]


def load_paid(path: str | Path) -> dict[str, dict]:
    return _read(path)["paid"]


def record_sent(path: str | Path, invoice_id: str, stage: str, when: date) -> None:
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {', '.join(STAGES)}")
    data = _read(path)
    entries = data["sent"].setdefault(invoice_id, [])
    entry = {"stage": stage, "date": when.isoformat()}
    if entry not in entries:
        entries.append(entry)
    _write(path, data)


def record_paid(path: str | Path, invoice_id: str, when: date, note: str = "") -> None:
    data = _read(path)
    data["paid"][invoice_id] = {"date": when.isoformat(), "note": note}
    _write(path, data)


def unrecord_paid(path: str | Path, invoice_id: str) -> bool:
    """Remove a mark-paid confirmation. Returns False if there was none."""
    data = _read(path)
    key = next((k for k in data["paid"] if str(k).strip().lower() == invoice_id.strip().lower()), None)
    if key is None:
        return False
    del data["paid"][key]
    _write(path, data)
    return True


def ci(mapping: dict) -> dict:
    """Case-insensitive view of a ledger section (ids are matched ignoring case and spaces)."""
    return {str(k).strip().lower(): v for k, v in mapping.items()}


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
