from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class Config:
    sender_name: str = "Your Name"
    sender_email: str = "you@example.com"
    payment_details: str = ""
    tone: str = "friendly"  # friendly | neutral | firm
    fx_tolerance_pct: float = 2.0  # accepted shortfall (fees / FX), percent of invoice amount
    date_window_days: int = 120  # payment may arrive up to N days after the invoice was issued
    auto_threshold: float = 0.70  # >= this: counted as matched
    review_threshold: float = 0.40  # >= this and < auto: "needs review", never counted as paid
    stage_days: list[int] = field(default_factory=lambda: [1, 7, 14, 30])
    fx_rates: dict[str, str] = field(default_factory=dict)  # {"EUR/USD": "1.08"} payment->invoice currency
    bank_mapping: dict[str, str] = field(default_factory=dict)  # generic bank CSV column mapping


def load_config(path: str | Path | None) -> Config:
    cfg = Config()
    if not path:
        return cfg
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    known = {f.name for f in fields(Config)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    for k, v in data.items():
        setattr(cfg, k, v)
    if cfg.tone not in ("friendly", "neutral", "firm"):
        raise ValueError("tone must be friendly, neutral or firm")
    if len(cfg.stage_days) != 4 or sorted(cfg.stage_days) != list(cfg.stage_days):
        raise ValueError("stage_days must be 4 ascending integers, e.g. [1, 7, 14, 30]")
    return cfg
