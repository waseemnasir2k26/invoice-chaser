from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from invoice_chaser.config import Config
from invoice_chaser.models import Invoice, Payment

EX = Path(__file__).resolve().parent.parent / "examples"


def inv(id="INV-1", client="Acme Studio Ltd", amount="1000", cur="USD", issued=date(2026, 9, 1),
        due=date(2026, 10, 1), status="", contact="Sam", email="sam@acme.example"):
    return Invoice(id, client, email, Decimal(amount), cur, issued, due, status, contact)


def pay(id="P1", d=date(2026, 10, 5), amount="1000", cur="USD", desc="", payer="", source="wise"):
    return Payment(id, d, Decimal(amount), cur, desc, payer, source)


@pytest.fixture
def cfg():
    return Config()


@pytest.fixture
def examples():
    return EX


ABSOLUTE_PATH = __import__("re").compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]|(?<![\w.:-])/(?:home|Users|tmp|var|private|mnt|opt|root)/", __import__("re").I)


def assert_no_absolute_paths(text: str, where: str = "") -> None:
    """Nothing shareable may name a folder on this computer: no Windows drive path, no /home, /Users, /tmp..."""
    text = text.replace("&#x27;", "'").replace("&amp;", "&")
    m = ABSOLUTE_PATH.search(text)
    assert m is None, f"absolute path in {where or 'output'}: {text[max(0, m.start() - 30):m.end() + 40]!r}"
