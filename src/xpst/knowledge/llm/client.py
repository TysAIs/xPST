"""Thin OpenAI-compatible chat client. ``httpx`` is a core xPST dependency but
is imported lazily so importing the knowledge package never pulls it in until a
request is actually made (keeps the wall and import cost low)."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence


def _httpx_client(timeout: float):
    """Indirection point so tests can monkeypatch the transport."""
    import httpx  # core dep, lazy

    return httpx.Client(timeout=timeout)


def _strip_code_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        # drop the opening fence (optionally ```json) and the closing fence
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.rstrip().endswith("```"):
            s = s.rstrip()[: -3]
    return s.strip()


def _extract_json(text: str) -> dict:
    """Parse JSON from an assistant message, tolerating markdown code fences
    and surrounding prose."""
    candidate = _strip_code_fences(text)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    # Fall back to the first balanced {...} span.
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(candidate[start: end + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError(f"LLM response was not valid JSON: {text[:200]!r}")


class LLMClient:
    """Calls ``POST {base_url}/chat/completions`` and returns parsed JSON from
    the assistant message. Works against any OpenAI-compatible endpoint."""

    def __init__(self, base_url: str, model: str,
                 api_key: str | None = None, *, timeout: float = 120.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def chat_json(self, messages: Sequence[dict[str, Any]]) -> dict:
        payload = {
            "model": self._model,
            "messages": list(messages),
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }
        url = f"{self._base_url}/chat/completions"
        with _httpx_client(self._timeout) as http:
            resp = http.post(url, json=payload, headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return _extract_json(content)
