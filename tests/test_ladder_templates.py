from datetime import date
from decimal import Decimal

import pytest
from conftest import inv

from invoice_chaser.ladder import days_overdue, stage_for
from invoice_chaser.llm import personal_line
from invoice_chaser.models import InvoiceResult
from invoice_chaser.templating import (
    TemplateError,
    load_template,
    render,
    variables_for,
)

DUE = date(2026, 10, 1)


@pytest.mark.parametrize("today,expected", [
    (date(2026, 10, 1), None),    # due today: not overdue
    (date(2026, 10, 2), "nudge"),  # day 1
    (date(2026, 10, 7), "nudge"),  # day 6
    (date(2026, 10, 8), "firm"),   # day 7
    (date(2026, 10, 14), "firm"),
    (date(2026, 10, 15), "final"),  # day 14
    (date(2026, 10, 30), "final"),
    (date(2026, 10, 31), "escalation"),  # day 30
    (date(2027, 3, 1), "escalation"),
    (date(2026, 9, 1), None),
])
def test_ladder_fixed_dates(today, expected):
    assert stage_for(days_overdue(DUE, today)) == expected


def test_ladder_custom_days():
    assert stage_for(3, (2, 3, 4, 5)) == "firm"


def _res(**kw):
    i = inv(**kw)
    return InvoiceResult(i, "overdue", Decimal("0.00"), i.amount, 15, "final", 0.0)


@pytest.mark.parametrize("stage", ["nudge", "firm", "final", "escalation"])
@pytest.mark.parametrize("tone", ["friendly", "neutral", "firm"])
def test_all_shipped_templates_render(stage, tone):
    subject, body = render(load_template(stage, tone), variables_for(_res(), "Alex", "Pay by Wise."))
    assert "{{" not in subject + body and "INV-1" in subject + body
    assert "Hi Sam," in body and body.rstrip().endswith("Alex")


def test_final_stage_has_pause_work_notice():
    _, body = render(load_template("final", "neutral"), variables_for(_res(), "Alex", ""))
    assert "pause work" in body


def test_empty_optional_lines_collapse():
    _, body = render(load_template("nudge", "friendly"), variables_for(_res(), "Alex", ""))
    assert "\n\n\n" not in body


def test_unknown_variable_raises():
    with pytest.raises(TemplateError):
        render("Subject: {{nope}}\n\nbody", {})


def test_missing_subject_raises():
    with pytest.raises(TemplateError):
        render("no subject here", {})


def test_no_contact_greets_there():
    v = variables_for(_res(contact=""), "Alex", "")
    assert v["first_name"] == "there"


def test_custom_template_dir_and_tone_fallback(tmp_path):
    (tmp_path / "nudge.neutral.md").write_text("Subject: hi {{invoice_id}}\n\nx", encoding="utf-8")
    assert "neutral" not in load_template("nudge", "firm", tmp_path) and load_template("nudge", "firm", tmp_path).startswith("Subject")


# ---- LLM: skipped without key, and sends only the allowed facts

def test_llm_skips_without_key():
    assert personal_line("Sam", "10 USD", 3, "INV-1", env={}) is None


def test_llm_sends_only_allowed_facts():
    import io
    import json
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout=0):
        seen["body"] = req.data.decode()
        seen["url"] = req.full_url
        return Resp(json.dumps({"choices": [{"message": {"content": "Hope your week is going well.\nextra"}}]}).encode())

    out = personal_line("Sam", "1,500.00 USD", 15, "INV-1", env={"GROQ_API_KEY": "k"}, opener=opener)
    assert out == "Hope your week is going well."
    assert "Sam" in seen["body"] and "INV-1" in seen["body"] and "1,500.00 USD" in seen["body"]
    assert "sam@acme" not in seen["body"] and "Acme" not in seen["body"]
    assert "Bearer" not in seen["body"]  # key is a header, never in the body


def test_llm_failure_and_bad_output_return_none():
    def boom(req, timeout=0):
        raise OSError("down")
    assert personal_line("Sam", "1 USD", 1, "I", env={"GEMINI_API_KEY": "k"}, opener=boom) is None
