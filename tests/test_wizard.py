"""Tests for the polished first-run wizard (xpst.wizard)."""

from __future__ import annotations

import json

import pytest

from xpst import wizard as wiz


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A minimal XPSTConfig pointed at a temp dir."""

    from xpst.config import XPSTConfig

    cfg = XPSTConfig.load()
    cfg.config_dir = tmp_path / ".xpst"
    return cfg


# ── Progress state ───────────────────────────────────────────

def test_wizard_state_roundtrip(config):
    assert wiz.load_wizard_state(config) == {}
    state = {"platforms": {"youtube": {"status": "connected"}}}
    wiz.save_wizard_state(config, state)
    reloaded = json.loads((config.config_dir / "wizard_state.json").read_text())
    assert reloaded["platforms"]["youtube"]["status"] == "connected"
    assert wiz.load_wizard_state(config)["platforms"]["youtube"]["status"] == "connected"


def test_wizard_state_corrupt_file_is_ignored(config):
    path = config.config_dir / "wizard_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert wiz.load_wizard_state(config) == {}


def test_record_platform_result_updates_status():
    state = {}
    wiz._record_platform_result(state, "x", True)
    assert state["platforms"]["x"]["status"] == "connected"
    wiz._record_platform_result(state, "x", False, detail="bad token")
    assert state["platforms"]["x"]["status"] == "failed"
    assert state["platforms"]["x"]["detail"] == "bad token"


# ── Non-TTY / agent mode (the EOF-crash regression) ─────────

def test_agent_mode_never_prompts(monkeypatch):
    """run_wizard with json_mode=True must not touch stdin at all."""

    def boom(*a, **k):  # any stdin read is a failure
        raise AssertionError("agent mode must not prompt")

    monkeypatch.setattr("builtins.input", boom)
    result = wiz.run_wizard_json(["messenger"])
    data = json.loads(json.dumps(result))
    assert data["mode"] == "agent"
    assert data["interactive"] is False
    assert len(data["checklist"]) == 1
    entry = data["checklist"][0]
    assert entry["platform"] == "messenger"
    assert entry["health"] in ("pass", "fail")
    assert entry["steps"], "checklist must carry click-by-click steps"
    assert data["all_pass"] == all(c["health"] == "pass" for c in data["checklist"])
    if not data["all_pass"]:
        assert data["next_action"].startswith("xpst wizard ")


def test_run_wizard_non_tty_emits_json(monkeypatch, capsys):
    """Simulated piped stdin: run_wizard auto-switches to agent mode."""

    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    ok = wiz.run_wizard(platforms=["messenger"], json_mode=False)
    out = capsys.readouterr().out
    start = out.rindex("\n{") + 1
    data = json.loads(out[start:])
    assert data["mode"] == "agent"
    assert ok == data["all_pass"]


def test_safe_input_raises_clean_error_on_non_tty(monkeypatch):
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(wiz.WizardNonInteractiveError):
        wiz._safe_input("hello: ")
    with pytest.raises(wiz.WizardNonInteractiveError):
        wiz._safe_confirm("continue?", default=True)


# ── Markdown export & checklist content ──────────────────────

def test_export_markdown(tmp_path):
    out = wiz.export_markdown(tmp_path / "guide.md")
    text = out.read_text()
    for key in wiz.PLATFORM_ORDER:
        title = wiz.PLATFORM_GUIDES[key].title
        assert f"## {title}" in text
    # TikTok Content Posting API + YouTube publish-to-production coverage
    assert "Content Posting API" in text
    assert "Publish App" in text or "production" in text.lower()
    assert "Instagram" in text


def test_checklist_covers_all_platforms(config):
    checklist = wiz.build_checklist(config)
    assert [c["platform"] for c in checklist] == wiz.PLATFORM_ORDER
    for entry in checklist:
        assert entry["action"] is None or entry["action"].startswith("xpst wizard ")
