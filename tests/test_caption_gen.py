"""Unit tests for AI caption + post-idea generation (deterministic and CLI).

Covers the lightweight ``xpst generate`` commands:
- ``xpst generate caption --text ...`` (reuses :func:`generate_caption`)
- ``xpst generate ideas --topic ... --count N`` (deterministic templates,
  LLM path when ``XPST_KB_LLM_ENABLED`` is set)
"""

import json

from click.testing import CliRunner

from xpst.caption_gen import (
    generate_caption,
    generate_caption_deterministic,
    generate_ideas,
    generate_ideas_deterministic,
)
from xpst.cli import main

TRANSCRIPT = (
    "We built an AI tool that writes captions for you. "
    "It saves hours every week. Marketing teams love the results. "
    "You should try it today."
)


def _no_llm_env(monkeypatch):
    """Force the no-LLM deterministic path regardless of host env."""
    monkeypatch.delenv("XPST_KB_LLM_ENABLED", raising=False)
    monkeypatch.delenv("XPST_KB_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("XPST_KB_LLM_API_KEY", raising=False)


# ── Deterministic captions ─────────────────────────────────────────────


def test_caption_deterministic_returns_three_variants():
    captions = generate_caption_deterministic(TRANSCRIPT)
    assert len(captions) == 3
    for c in captions:
        assert {"caption", "hashtags", "style"} <= set(c)
        assert c["caption"]
        assert c["style"] in {"direct", "hook", "summary"}


def test_caption_deterministic_empty_text_has_fallback():
    captions = generate_caption_deterministic("")
    assert len(captions) == 3
    assert captions[0]["caption"] == "Check out this video!"


def test_caption_deterministic_respects_platform_char_limit():
    captions = generate_caption_deterministic(TRANSCRIPT, platform="x")
    assert all(len(c["caption"]) <= 280 for c in captions)


def test_caption_falls_back_to_deterministic_without_llm(monkeypatch):
    _no_llm_env(monkeypatch)
    captions = generate_caption(TRANSCRIPT)
    assert len(captions) == 3
    assert all("style" in c for c in captions)


# ── Deterministic ideas ────────────────────────────────────────────────


def test_ideas_deterministic_respects_count_and_topic():
    ideas = generate_ideas_deterministic("marketing", count=5)
    assert len(ideas) == 5
    for idea in ideas:
        assert {"idea", "hook", "source"} <= set(idea)
        assert idea["source"] == "deterministic"
        assert "marketing" in idea["idea"].lower()


def test_ideas_deterministic_clamps_count():
    assert len(generate_ideas_deterministic("tech", count=0)) == 1
    assert len(generate_ideas_deterministic("tech", count=99)) == 10


def test_ideas_deterministic_empty_topic_uses_default():
    ideas = generate_ideas_deterministic("   ", count=3)
    assert len(ideas) == 3
    assert "content creation" in ideas[0]["idea"]


def test_ideas_fallback_to_deterministic_without_llm(monkeypatch):
    _no_llm_env(monkeypatch)
    ideas = generate_ideas("food", count=3)
    assert len(ideas) == 3
    assert all(i["source"] == "deterministic" for i in ideas)


# ── LLM path (XPST_KB_LLM_ENABLED) ─────────────────────────────────────


class _FakeLLMClient:
    def __init__(self, base_url, model, **kwargs):
        self.base_url = base_url
        self.model = model

    def chat_json(self, messages):
        return [
            {"idea": "LLM idea one about marketing", "hook": "list"},
            {"idea": "LLM idea two about marketing", "hook": "tip"},
        ]


def test_ideas_uses_llm_when_enabled(monkeypatch):
    monkeypatch.setenv("XPST_KB_LLM_ENABLED", "1")
    monkeypatch.setenv("XPST_KB_LLM_BASE_URL", "http://localhost:9999/v1")
    monkeypatch.setenv("XPST_KB_LLM_API_KEY", "test")
    monkeypatch.setattr("xpst.knowledge.llm.client.LLMClient", _FakeLLMClient)

    ideas = generate_ideas("marketing", count=2)
    assert len(ideas) == 2
    assert all(i["source"] == "llm" for i in ideas)
    assert ideas[0]["idea"].startswith("LLM idea")


def test_caption_uses_llm_when_enabled(monkeypatch):
    monkeypatch.setenv("XPST_KB_LLM_ENABLED", "1")
    monkeypatch.setenv("XPST_KB_LLM_BASE_URL", "http://localhost:9999/v1")
    monkeypatch.setenv("XPST_KB_LLM_API_KEY", "test")

    class _FakeCaptionClient:
        def __init__(self, base_url, model, **kwargs):
            pass

        def chat_json(self, messages):
            return [
                {"caption": "LLM caption one", "hashtags": "#AI", "style": "direct"},
                {"caption": "LLM caption two", "hashtags": "#AI", "style": "hook"},
            ]

    monkeypatch.setattr("xpst.knowledge.llm.client.LLMClient", _FakeCaptionClient)

    captions = generate_caption(TRANSCRIPT)
    assert len(captions) == 2
    assert captions[0]["caption"] == "LLM caption one"


# ── CLI: xpst generate ─────────────────────────────────────────────────


def test_cli_generate_caption(monkeypatch):
    # CliRunner pipes stdout, so xPST's auto-JSON-on-non-TTY kicks in.
    _no_llm_env(monkeypatch)
    out = CliRunner().invoke(main, ["generate", "caption", "--text", TRANSCRIPT])
    assert out.exit_code == 0, out.output
    data = json.loads(out.output)
    assert data["platform"] == "instagram"
    assert len(data["suggestions"]) == 3


def test_cli_generate_caption_json(monkeypatch):
    _no_llm_env(monkeypatch)
    out = CliRunner().invoke(main, ["generate", "caption", "-t", TRANSCRIPT, "--json"])
    assert out.exit_code == 0, out.output
    data = json.loads(out.output)
    assert data["platform"] == "instagram"
    assert data["char_limit"] == 2200
    assert len(data["suggestions"]) == 3


def test_cli_generate_ideas(monkeypatch):
    # CliRunner pipes stdout, so xPST's auto-JSON-on-non-TTY kicks in.
    _no_llm_env(monkeypatch)
    out = CliRunner().invoke(main, ["generate", "ideas", "--topic", "cooking", "--count", "4"])
    assert out.exit_code == 0, out.output
    data = json.loads(out.output)
    assert data["topic"] == "cooking"
    assert data["count"] == 4
    assert len(data["ideas"]) == 4


def test_cli_generate_ideas_json(monkeypatch):
    _no_llm_env(monkeypatch)
    out = CliRunner().invoke(main, ["generate", "ideas", "-t", "cooking", "-n", "3", "--json"])
    assert out.exit_code == 0, out.output
    data = json.loads(out.output)
    assert data["topic"] == "cooking"
    assert data["count"] == 3
    assert len(data["ideas"]) == 3
    assert all(i["source"] == "deterministic" for i in data["ideas"])


def test_cli_generate_ideas_rejects_out_of_range_count(monkeypatch):
    _no_llm_env(monkeypatch)
    out = CliRunner().invoke(main, ["generate", "ideas", "--topic", "tech", "--count", "99"])
    assert out.exit_code != 0
