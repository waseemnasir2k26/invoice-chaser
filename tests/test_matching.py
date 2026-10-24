from datetime import date
from decimal import Decimal

from conftest import inv, pay

from invoice_chaser.matching import match_all, name_similarity, ref_hit

TODAY = date(2026, 10, 25)


def one(invoices, payments, cfg):
    res, left = match_all(invoices, payments, TODAY, cfg)
    return res, left


def test_ref_hit_boundaries():
    assert ref_hit("INV-12", "Payment for inv 12 thanks")
    assert ref_hit("INV-12", "INV#12")
    assert not ref_hit("INV-12", "INV-120")
    assert not ref_hit("INV-12", "XINV-12")


def test_exact_amount_plus_ref_is_paid(cfg):
    (r,), _ = one([inv()], [pay(desc="ref INV-1")], cfg)
    assert r.status == "paid" and r.confidence >= 0.9
    assert "invoice id INV-1" in r.matches[0].reason and "exact amount" in r.matches[0].reason


def test_exact_amount_and_name_is_paid(cfg):
    (r,), _ = one([inv()], [pay(payer="Acme Studio Ltd")], cfg)
    assert r.status == "paid"


def test_amount_only_is_needs_review_never_paid(cfg):
    (r,), left = one([inv(due=date(2026, 10, 1))], [pay(payer="Someone Else", desc="transfer")], cfg)
    assert r.status == "needs_review" and r.paid == 0
    assert r.suggestions and r.suggestions[0].confidence < cfg.auto_threshold
    assert r.stage is None  # no reminder while review is pending
    assert left  # payment still unallocated


def test_nothing_matches_overdue(cfg):
    (r,), _ = one([inv()], [pay(amount="12", payer="Zed")], cfg)
    assert r.status == "overdue" and r.days_overdue == 24 and r.stage == "final"


def test_partial_with_reference(cfg):
    (r,), _ = one([inv(amount="2500")], [pay(amount="1000", desc="INV-1 deposit")], cfg)
    assert r.status == "partial" and r.paid == Decimal("1000.00") and r.outstanding == Decimal("1500.00")


def test_two_partials_complete_the_invoice(cfg):
    ps = [pay("P1", amount="1000", desc="INV-1 part 1"), pay("P2", d=date(2026, 10, 10), amount="1500", desc="INV-1 final part")]
    (r,), _ = one([inv(amount="2500")], ps, cfg)
    assert r.status == "paid" and len(r.matches) == 2


def test_fee_shortfall_within_tolerance(cfg):
    (r,), _ = one([inv(amount="800")], [pay(amount="786", desc="INV-1")], cfg)
    assert r.status == "paid"


def test_shortfall_beyond_tolerance_is_partial(cfg):
    (r,), _ = one([inv(amount="800")], [pay(amount="700", desc="INV-1")], cfg)
    assert r.status == "partial" and r.outstanding == Decimal("100.00")


def test_tolerance_is_configurable(cfg):
    cfg.fx_tolerance_pct = 0
    (r,), _ = one([inv(amount="800")], [pay(amount="786", desc="INV-1")], cfg)
    assert r.status == "partial"


def test_cross_currency_needs_rate(cfg):
    p = pay(amount="920", cur="EUR", desc="INV-1")
    (r,), _ = one([inv()], [p], cfg)
    assert r.status != "paid"
    cfg.fx_rates = {"EUR/USD": "1.09"}  # 920 * 1.09 = 1002.80 -> within 2%
    (r,), _ = one([inv()], [p], cfg)
    assert r.status == "paid" and "converted" in r.matches[0].reason


def test_fuzzy_name(cfg):
    assert name_similarity("Acme Studio Ltd", "ACME STUDIO LIMITED") == 1.0
    assert name_similarity("Acme Studio", "", "Received from Acme Studios") > 0.9
    assert name_similarity("Acme Studio", "Zed Corp") < 0.6
    (r,), _ = one([inv()], [pay(payer="Acme Studios")], cfg)
    assert r.status in ("paid", "needs_review")  # strong fuzzy + exact amount


def test_payment_before_issue_or_outside_window_ignored(cfg):
    (r,), _ = one([inv()], [pay(d=date(2026, 8, 1), desc="INV-1")], cfg)
    assert r.status == "overdue"
    cfg.date_window_days = 10
    (r,), _ = one([inv()], [pay(d=date(2026, 10, 20), desc="INV-1")], cfg)
    assert r.status == "overdue"


def test_one_payment_not_double_counted(cfg):
    a, b = inv("INV-1"), inv("INV-2")
    res, _ = one([a, b], [pay(desc="INV-1 INV-2")], cfg)
    assert sorted(r.status for r in res) != ["paid", "paid"]


def test_overpayment_leaves_remainder(cfg):
    # id alone + a higher amount is only 0.65: flagged, not auto-paid
    (r,), left = one([inv()], [pay(amount="1300", desc="INV-1")], cfg)
    assert r.status == "needs_review" and left[0][1] == Decimal(1300)
    # id + client name: confident, and the surplus is reported as unallocated
    (r,), left = one([inv()], [pay(amount="1300", desc="INV-1", payer="Acme Studio Ltd")], cfg)
    assert r.status == "paid" and left[0][1] == Decimal(300)


def test_manual_status_respected(cfg):
    res, _ = one([inv(status="paid"), inv("INV-2", status="void")], [], cfg)
    assert [r.status for r in res] == ["paid", "void"]


def test_not_due_is_open(cfg):
    (r,), _ = one([inv(due=date(2026, 11, 30))], [], cfg)
    assert r.status == "open" and r.stage is None and r.days_overdue == 0
