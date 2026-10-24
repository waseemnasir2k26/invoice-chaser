"""Optional personalisation line from a free-tier LLM (Groq or Gemini).

Privacy contract: the request contains ONLY the client first name, the amount
string, days overdue and the invoice id. No email, no other client data.
If no key is set, or anything fails, this returns None and the template is
used as-is.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

PROMPT = (
    "Write ONE short, polite sentence (max 25 words) to open a payment reminder email. "
    "Facts: recipient first name {first_name}; invoice {invoice_id}; amount {amount}; "
    "{days_overdue} days overdue. Do not threaten, do not mention legal action, do not invent facts. "
    "Return only the sentence."
)


def _clean_output(text: str) -> str | None:
    line = (text or "").strip().splitlines()[0].strip().strip('"') if (text or "").strip() else ""
    if not line or len(line) > 240 or re.search(r"https?://|www\.|@", line):
        return None
    return line


def clean_first_name(s: str) -> str:
    """Letters only, at most 3 words, max 30 characters. Anything else (control characters,
    quotes, punctuation, digits, hyphens) becomes a word break or is dropped, so CSV data
    cannot steer the prompt."""
    words = "".join(ch if ch.isalpha() else " " for ch in str(s)).split()[:3]
    return " ".join(words)[:30].strip() or "there"


def _clean(text: str, allowed: str, limit: int) -> str:
    return "".join(ch for ch in str(text) if ch.isalnum() or ch in allowed)[:limit].strip()


def payload_facts(first_name: str, amount: str, days_overdue: int, invoice_id: str) -> dict:
    return {
        "first_name": clean_first_name(first_name),
        "amount": _clean(amount, " .,", 30),
        "days_overdue": int(days_overdue),
        "invoice_id": _clean(invoice_id, "-_#./", 40),
    }


def personal_line(first_name: str, amount: str, days_overdue: int, invoice_id: str,
                  env=None, opener=None, timeout: float = 15.0) -> str | None:
    env = os.environ if env is None else env
    opener = opener or urllib.request.urlopen
    prompt = PROMPT.format(**payload_facts(first_name, amount, days_overdue, invoice_id))
    try:
        if env.get("GROQ_API_KEY"):
            req = urllib.request.Request(
                "https://api.groq.com/openai/v1/chat/completions",
                data=json.dumps({
                    "model": env.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 80, "temperature": 0.4,
                }).encode(),
                headers={"content-type": "application/json", "authorization": "Bearer " + env["GROQ_API_KEY"]},
            )
            with opener(req, timeout=timeout) as r:
                return _clean_output(json.load(r)["choices"][0]["message"]["content"])
        if env.get("GEMINI_API_KEY"):
            model = env.get("GEMINI_MODEL", "gemini-2.0-flash")
            req = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                data=json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode(),
                headers={"content-type": "application/json", "x-goog-api-key": env["GEMINI_API_KEY"]},
            )
            with opener(req, timeout=timeout) as r:
                return _clean_output(json.load(r)["candidates"][0]["content"]["parts"][0]["text"])
    except Exception:  # network, quota, bad JSON: skip cleanly
        return None
    return None
