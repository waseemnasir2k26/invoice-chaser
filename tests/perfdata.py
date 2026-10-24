"""Deterministic synthetic data for the performance test."""
import random
from datetime import date, timedelta
from decimal import Decimal

from invoice_chaser.models import Invoice, Payment

WORDS = ("acme northwind bluebird kestrel harbor lumen delta orchid summit pioneer cedar falcon granite "
         "meridian oakridge quartz redwood sterling tidal vertex willow zenith aurora beacon cobalt dune "
         "ember fjord glacier horizon indigo juniper krypton lagoon mosaic nimbus onyx prairie quill "
         "raven saffron topaz umber velvet wren xenon yarrow zephyr").split()
SUFFIX = ["studio", "media", "labs", "trading", "consulting", "group", "digital", "works", "partners", "co"]


def make(n_inv=1000, n_pay=5000, seed=7):
    rnd = random.Random(seed)
    base = date(2026, 1, 1)
    invs = []
    for i in range(n_inv):
        issued = base + timedelta(days=rnd.randint(0, 250))
        invs.append(Invoice(f"INV-{1000 + i}", f"{rnd.choice(WORDS).title()} {rnd.choice(SUFFIX).title()}", "c@example.com",
                            Decimal(rnd.randint(10000, 500000)) / 100, rnd.choice(["USD", "USD", "EUR", "GBP"]),
                            issued, issued + timedelta(days=30)))
    pays = []
    for j in range(n_pay):
        if j % 3 == 0:  # a third are real payments for some invoice
            inv = rnd.choice(invs)
            pays.append(Payment(f"P{j}", inv.issued + timedelta(days=rnd.randint(1, 60)), inv.amount, inv.currency,
                                f"Payment {inv.id}" if j % 2 else "Transfer", inv.client, "wise"))
        else:
            pays.append(Payment(f"P{j}", base + timedelta(days=rnd.randint(0, 330)),
                                Decimal(rnd.randint(1000, 600000)) / 100, rnd.choice(["USD", "EUR", "GBP"]),
                                f"Received money from {rnd.choice(WORDS).title()} {rnd.choice(SUFFIX).title()}",
                                f"{rnd.choice(WORDS).title()} {rnd.choice(SUFFIX).title()}", "wise"))
    return invs, pays
