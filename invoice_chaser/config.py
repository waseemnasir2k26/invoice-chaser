from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path


@dataclass
class Config:
    sender_name: str = "Your Name"
    sender_email: str = ""  # empty = not configured: drafts get a placeholder From and a loud warning
    payment_details: str = ""
    tone: str = "friendly"  # friendly | neutral | firm
    fx_tolerance_pct: float = 2.0  # accepted shortfall (fees / FX), percent of invoice amount
    date_window_days: int = 120  # payment may arrive up to N days after the invoice was issued
    auto_threshold: float = 0.70  # >= this: counted as matched
    review_threshold: float = 0.40  # >= this and < auto: "needs review", never counted as paid
    stage_days: list[int] = field(default_factory=lambda: [1, 7, 14, 30])
    fx_rates: dict[str, str] = field(default_factory=dict)  # {"EUR/USD": "1.08"} payment->invoice currency
    bank_mapping: dict[str, str] = field(default_factory=dict)  # generic bank CSV column mapping
    previous_reminders: bool = False  # True: wording may say "following my earlier reminder"
    pause_work_on_final: bool = False  # True: final/escalation drafts include the pause-work sentence
    reply_within_days: int = 7  # deadline wording in final/escalation drafts
    ref_prefixes: list[str] = field(default_factory=lambda: ["INV", "INVOICE"])  # ids that start with one of these (INV-1) count as ids; all-digit ids need one too
    decimal_comma: bool | None = None  # None: read only unambiguous amounts; True: 1.234,56; False: 1,234.56
    stuck_overdue_days: int = 30  # ...or this many days overdue with a suggestion still waiting
    stuck_after_days: int = 7  # a suggestion / review waiting longer than this is flagged STUCK (internal note + banner)
    default_currency: str = ""  # what a bare "$" in your files means, e.g. "USD"


def _num(name: str, v, lo: float, hi: float, integer: bool = False) -> None:
    ok = isinstance(v, int) if integer else isinstance(v, (int, float))
    if isinstance(v, bool) or not ok:
        raise ValueError(f"config {name} must be {'an integer' if integer else 'a number'}, got {v!r}")
    if not lo <= v <= hi:
        raise ValueError(f"config {name} must be between {lo:g} and {hi:g}, got {v}")


def validate(cfg: Config) -> Config:
    if cfg.tone not in ("friendly", "neutral", "firm"):
        raise ValueError("config tone must be friendly, neutral or firm")
    _num("fx_tolerance_pct", cfg.fx_tolerance_pct, 0, 20)
    _num("auto_threshold", cfg.auto_threshold, 0, 1)
    _num("review_threshold", cfg.review_threshold, 0, 1)
    if cfg.review_threshold > cfg.auto_threshold:
        raise ValueError("config review_threshold must not exceed auto_threshold")
    _num("date_window_days", cfg.date_window_days, 1, 3650, integer=True)
    _num("reply_within_days", cfg.reply_within_days, 1, 90, integer=True)
    _num("stuck_after_days", cfg.stuck_after_days, 1, 365, integer=True)
    _num("stuck_overdue_days", cfg.stuck_overdue_days, 1, 3650, integer=True)
    if (not isinstance(cfg.stage_days, list) or len(cfg.stage_days) != 4
            or not all(isinstance(x, int) and not isinstance(x, bool) and x >= 1 for x in cfg.stage_days)
            or sorted(set(cfg.stage_days)) != list(cfg.stage_days)):
        raise ValueError("config stage_days must be 4 strictly ascending integers >= 1, e.g. [1, 7, 14, 30]")
    if (not isinstance(cfg.ref_prefixes, list)
            or not all(isinstance(x, str) and re.fullmatch(r"[A-Za-z]{2,20}", x) for x in cfg.ref_prefixes)):
        raise ValueError("config ref_prefixes must be a list of letter-only prefixes like [\"INV\"]")
    if cfg.decimal_comma is not None and not isinstance(cfg.decimal_comma, bool):
        raise ValueError("config decimal_comma must be true, false or null")
    for name in ("previous_reminders", "pause_work_on_final"):
        if not isinstance(getattr(cfg, name), bool):
            raise ValueError(f"config {name} must be true or false")
    if not isinstance(cfg.default_currency, str) or (cfg.default_currency and not re.fullmatch(r"[A-Za-z]{3}", cfg.default_currency)):
        raise ValueError("config default_currency must be a 3-letter code like USD")
    cfg.default_currency = cfg.default_currency.upper()
    for name in ("sender_name", "sender_email", "payment_details"):
        if not isinstance(getattr(cfg, name), str):
            raise ValueError(f"config {name} must be text")
    if not isinstance(cfg.bank_mapping, dict):
        raise ValueError("config bank_mapping must be an object")
    if not isinstance(cfg.fx_rates, dict):
        raise ValueError("config fx_rates must be an object like {\"EUR/USD\": \"1.08\"}")
    rates = {}
    for k, v in cfg.fx_rates.items():
        key = str(k).upper()
        if not re.fullmatch(r"[A-Z]{3}/[A-Z]{3}", key):
            raise ValueError(f"config fx_rates key {k!r} must look like EUR/USD (payment currency / invoice currency)")
        try:
            d = Decimal(str(v))
        except InvalidOperation:
            raise ValueError(f"config fx_rates[{k!r}] must be a number, got {v!r}") from None
        if not d.is_finite() or d <= 0:
            raise ValueError(f"config fx_rates[{k!r}] must be a positive number, got {v!r}")
        rates[key] = str(d)
    cfg.fx_rates = rates
    return cfg


def load_config(path: str | Path | None) -> Config:
    cfg = Config()
    if not path:
        return validate(cfg)
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("config file must be a JSON object")
    known = {f.name for f in fields(Config)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    for k, v in data.items():
        setattr(cfg, k, v)
    return validate(cfg)
