"""Optional Gemini-powered fallback for cards the parser couldn't fully resolve.

Imported lazily — the package only needs to be installed if the user supplies
an API key in the UI.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from card import Card


def _client(api_key: str):
    from google import genai  # type: ignore[import]
    return genai.Client(api_key=api_key)


SYSTEM = (
    "You are helping triage Hebrew medical exam recall questions. "
    "Given a question stem and 5 options, output strict JSON: "
    '{"correct": <int 1..5>, "rationale": "<one short sentence in Hebrew>"}.'
)


def validate_key(api_key: str) -> tuple[bool, str]:
    """Cheap key check: list models. Returns (ok, message)."""
    try:
        client = _client(api_key)
        models = list(client.models.list())
        return True, f"Key works ({len(models)} models accessible)"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def suggest_answer(card: Card, api_key: str, model: str = "gemini-2.5-flash") -> Optional[int]:
    """Ask Gemini for the most likely correct option index. Returns 1..5 or None."""
    if not card.options:
        return None
    client = _client(api_key)
    payload = {
        "stem": _strip_html(card.question_html),
        "options": [_strip_html(o) for o in card.options],
    }
    prompt = (
        SYSTEM
        + "\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    resp = client.models.generate_content(model=model, contents=prompt)
    text = resp.text or ""
    m = re.search(r'"correct"\s*:\s*([1-5])', text)
    if m:
        return int(m.group(1))
    return None


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)
