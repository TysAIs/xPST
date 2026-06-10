import json

import pytest

from xpst.knowledge.llm.client import LLMClient


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHttpxClient:
    """Captures the POST and returns a canned OpenAI-style chat response."""

    def __init__(self, content, capture):
        self._content = content
        self._capture = capture

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):
        self._capture["url"] = url
        self._capture["json"] = json
        self._capture["headers"] = headers
        return _FakeResponse({
            "choices": [{"message": {"content": self._content}}]
        })


def test_chat_json_parses_assistant_content(monkeypatch):
    capture = {}
    content = json.dumps({"answer": 42})

    import xpst.knowledge.llm.client as mod

    monkeypatch.setattr(
        mod, "_httpx_client",
        lambda *a, **k: _FakeHttpxClient(content, capture),
    )
    client = LLMClient(base_url="http://x/v1", model="m", api_key="key")
    out = client.chat_json([{"role": "user", "content": "hi"}])
    assert out == {"answer": 42}
    # hits the OpenAI-compatible chat completions path with the model+messages
    assert capture["url"].endswith("/chat/completions")
    assert capture["json"]["model"] == "m"
    assert capture["json"]["messages"][0]["content"] == "hi"
    assert capture["headers"]["Authorization"] == "Bearer key"


def test_chat_json_strips_code_fences(monkeypatch):
    capture = {}
    fenced = "```json\n{\"k\": 1}\n```"

    import xpst.knowledge.llm.client as mod

    monkeypatch.setattr(
        mod, "_httpx_client",
        lambda *a, **k: _FakeHttpxClient(fenced, capture),
    )
    client = LLMClient(base_url="http://x/v1", model="m")
    assert client.chat_json([{"role": "user", "content": "hi"}]) == {"k": 1}


def test_chat_json_no_auth_header_without_key(monkeypatch):
    capture = {}

    import xpst.knowledge.llm.client as mod

    monkeypatch.setattr(
        mod, "_httpx_client",
        lambda *a, **k: _FakeHttpxClient('{"ok": true}', capture),
    )
    client = LLMClient(base_url="http://x/v1", model="m")
    client.chat_json([{"role": "user", "content": "hi"}])
    assert "Authorization" not in capture["headers"]


def test_chat_json_raises_on_unparseable(monkeypatch):
    capture = {}

    import xpst.knowledge.llm.client as mod

    monkeypatch.setattr(
        mod, "_httpx_client",
        lambda *a, **k: _FakeHttpxClient("not json at all", capture),
    )
    client = LLMClient(base_url="http://x/v1", model="m")
    with pytest.raises(ValueError):
        client.chat_json([{"role": "user", "content": "hi"}])
