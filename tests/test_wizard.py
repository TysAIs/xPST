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
    """Simulated piped stdin: run_wizard auto-switches to agent mode.

    Since the JSON-purity fix, stdout carries ONLY the JSON payload —
    human-facing health-check chatter is routed to stderr.
    """

    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    ok = wiz.run_wizard(platforms=["messenger"], json_mode=False)
    out = capsys.readouterr().out
    data = json.loads(out)  # whole stdout must be pure JSON
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
    for key in wiz.GUIDE_PLATFORMS:
        title = wiz.PLATFORM_GUIDES[key].title
        assert f"## {title}" in text
    # TikTok Content Posting API + YouTube publish-to-production coverage
    assert "Content Posting API" in text
    assert "Publish App" in text or "production" in text.lower()
    assert "Instagram" in text


def test_checklist_covers_all_platforms(config):
    checklist = wiz.build_checklist(config)
    assert [c["platform"] for c in checklist] == wiz.GUIDE_PLATFORMS
    for entry in checklist:
        assert entry["action"] is None or entry["action"].startswith("xpst wizard ")
        assert entry["opt_in"] is (entry["platform"] in wiz.OPT_IN_PLATFORMS)


# ── Guide vocabulary: no guide may be unreachable ──────────────

def test_every_registered_guide_is_addressable():
    """A registered guide must appear in every guide-listing vocabulary.

    The Bluesky guide first shipped registered but unreachable: the wizard's
    platform argument was a second, hand-maintained list that never learned the
    new key, so `xpst wizard bluesky` answered "invalid choice" and the export
    silently skipped it.
    """

    assert list(wiz.PLATFORM_GUIDES) == wiz.GUIDE_PLATFORMS
    assert set(wiz.PLATFORM_ORDER) <= set(wiz.GUIDE_PLATFORMS)
    assert set(wiz.OPT_IN_PLATFORMS) <= set(wiz.GUIDE_PLATFORMS)
    assert not set(wiz.OPT_IN_PLATFORMS) & set(wiz.PLATFORM_ORDER), (
        "an opt-in guide must not also be in the connect-everything sweep"
    )


def test_cli_platform_arguments_accept_every_guide():
    """`xpst wizard <p>` and `xpst connect <p>` accept every registered guide."""

    from xpst.cli import connect, wizard

    for command in (wizard, connect):
        platform = next(p for p in command.params if p.name == "platform")
        choices = getattr(platform.type, "choices", None)
        assert choices is not None, command.name
        assert set(wiz.GUIDE_PLATFORMS) <= set(choices), command.name


def test_wizard_accepts_bluesky_platform(tmp_path, monkeypatch):
    """`xpst wizard bluesky` is a real invocation, not a usage error."""

    from click.testing import CliRunner

    from xpst.cli import main

    cfg = tmp_path / "config.yaml"
    monkeypatch.setenv("XPST_CONFIG_DIR", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(main, ["--config", str(cfg), "wizard", "bluesky", "--json"])
    assert result.exit_code != 2, result.output
    assert "invalid choice" not in result.output.lower()
    payload = json.loads(result.output[result.output.find("{"):])
    entry = next(c for c in payload["checklist"] if c["platform"] == "bluesky")
    assert entry["opt_in"] is True
    assert entry["steps"] and entry["title"] == "Bluesky"


def test_opt_in_platform_does_not_gate_completion(tmp_path, monkeypatch):
    """An unconnected opt-in destination must not block first-run completion."""

    cfg = _make_isolated_config(tmp_path)
    checklist = _all_pass_checklist(wiz.PLATFORM_ORDER) + [
        {"platform": "bluesky", "health": "fail", "action": "xpst wizard bluesky", "opt_in": True},
    ]
    monkeypatch.setattr(wiz, "build_checklist", lambda config: checklist)

    result = wiz.run_wizard_json(config=cfg)

    assert result["all_pass"] is True
    assert result["completed"] is True
    state = json.loads((tmp_path / "wizard_state.json").read_text())
    assert state["completed"] is True
    assert "bluesky" not in state["platforms"], (
        "a destination that was never connected must not be recorded as verified"
    )


def test_opt_in_only_request_is_its_own_gate(tmp_path, monkeypatch):
    """`wizard bluesky --json` is gated by bluesky itself."""

    cfg = _make_isolated_config(tmp_path)
    checklist = [
        {"platform": "bluesky", "health": "fail", "action": "xpst wizard bluesky", "opt_in": True},
    ]
    monkeypatch.setattr(wiz, "build_checklist", lambda config: checklist)

    result = wiz.run_wizard_json(["bluesky"], config=cfg)

    assert result["all_pass"] is False
    assert result["next_action"] == "xpst wizard bluesky"


def test_interactive_wizard_dispatches_bluesky(tmp_path, monkeypatch):
    """`xpst wizard bluesky` on a TTY reaches connect_bluesky, not a KeyError."""

    import sys

    import xpst.connect as connect_mod

    cfg = _make_isolated_config(tmp_path)
    calls: list[str] = []

    def _fake_connect_bluesky(config):
        calls.append("bluesky")
        return True

    async def _fake_test_connections(config):
        return {"bluesky": True}

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(wiz, "_safe_confirm", lambda *a, **k: True)
    monkeypatch.setattr(wiz.XPSTConfig, "load", staticmethod(lambda *a, **k: cfg))
    monkeypatch.setattr(connect_mod, "connect_bluesky", _fake_connect_bluesky)
    monkeypatch.setattr(connect_mod, "test_connections", _fake_test_connections)

    assert wiz.run_wizard(platforms=["bluesky"]) is True
    assert calls == ["bluesky"]
    state = json.loads((tmp_path / "wizard_state.json").read_text())
    assert state["platforms"]["bluesky"]["status"] == "connected"


# ── First-run completion (server-side state write) ────────────────

def _make_isolated_config(tmp_path):
    """An XPSTConfig whose config_dir + state live entirely under tmp_path."""
    from xpst.config import XPSTConfig

    cfg = XPSTConfig.load()
    cfg.config_dir = str(tmp_path)
    # Hermetic: never inherit the host's live first-run state.
    cfg.first_run_complete = False
    return cfg


def _all_pass_checklist(platforms):
    return [
        {"platform": p, "health": "pass", "action": None}
        for p in platforms
    ]


def _all_fail_checklist(platforms):
    return [
        {"platform": p, "health": "fail", "action": f"xpst wizard {p}"}
        for p in platforms
    ]


def _assert_persisted_state(tmp_path, expected_first_run: bool, completed_state: bool):
    from xpst.config import XPSTConfig

    cfg_path = tmp_path / "config.yaml"
    state_path = tmp_path / "wizard_state.json"
    if expected_first_run is False:
        # An unfinished flow must not have persisted anything.
        assert not cfg_path.exists(), "config must not be written before completion"
        if completed_state is False:
            assert not state_path.exists()
            return
    else:
        assert cfg_path.exists(), "completion must persist config.yaml"
    reloaded = XPSTConfig.load(str(cfg_path))
    assert bool(reloaded.first_run_complete) is expected_first_run
    import json

    state = json.loads(state_path.read_text())
    assert bool(state.get("completed")) is completed_state


def test_agent_mode_all_pass_sets_first_run_complete(tmp_path, monkeypatch):
    """[1] Agent/--json mode completing every requested platform must persist
    first_run_complete=true server-side (config flag + wizard_state.json)."""
    cfg = _make_isolated_config(tmp_path)
    monkeypatch.setattr(wiz, "build_checklist", lambda config: _all_pass_checklist(["youtube", "x"]))

    result = wiz.run_wizard_json(["youtube", "x"], config=cfg)

    assert result["all_pass"] is True
    assert result["completed"] is True
    _assert_persisted_state(tmp_path, expected_first_run=True, completed_state=True)
    assert cfg.first_run_complete is True


def test_agent_mode_partial_run_does_not_complete(tmp_path, monkeypatch):
    """Partial/failing checks must leave first_run_complete untouched."""
    cfg = _make_isolated_config(tmp_path)
    monkeypatch.setattr(wiz, "build_checklist", lambda config: _all_fail_checklist(["youtube", "x"]))

    result = wiz.run_wizard_json(["youtube", "x"], config=cfg)

    assert result["all_pass"] is False
    assert result["completed"] is False
    _assert_persisted_state(tmp_path, expected_first_run=False, completed_state=False)
    assert cfg.first_run_complete is False


def test_agent_mode_full_run_all_pass_completes(tmp_path, monkeypatch):
    """Agent mode over the full checklist (bot finalizing install) persists."""
    cfg = _make_isolated_config(tmp_path)
    monkeypatch.setattr(
        wiz, "build_checklist",
        lambda config: _all_pass_checklist(wiz.PLATFORM_ORDER),
    )

    result = wiz.run_wizard_json(config=cfg)

    assert result["completed"] is True
    _assert_persisted_state(tmp_path, expected_first_run=True, completed_state=True)


def test_onboarding_not_required_once_complete_even_without_wizard_state(tmp_path):
    """[3] Defensive: once first_run_complete=true is persisted, onboarding
    must not be required even when wizard_state.json is absent/stale."""
    from xpst.config import XPSTConfig

    cfg = _make_isolated_config(tmp_path)
    cfg.first_run_complete = True
    cfg.save()
    # No wizard_state.json exists in tmp_path at this point.
    assert not (tmp_path / "wizard_state.json").exists()
    reloaded = XPSTConfig.load(str(tmp_path / "config.yaml"))
    assert wiz.onboarding_required(reloaded) is False


def test_onboarding_required_when_not_complete_regardless_of_state_file(tmp_path):
    """Not-complete must require onboarding whether or not a stale progress
    file exists (the file alone must never suppress onboarding either)."""
    cfg = _make_isolated_config(tmp_path)
    cfg.first_run_complete = False
    cfg.save()
    assert wiz.onboarding_required(cfg) is True
    wiz.save_wizard_state(cfg, {"platforms": {}})
    # Even with a progress file present, an un-completed install keeps
    # requiring onboarding.
    assert wiz.onboarding_required(cfg) is True
    reloaded_state = wiz.load_wizard_state(cfg)
    assert "completed" not in reloaded_state


def test_mark_onboarding_complete_writes_both_state_files(tmp_path):
    """mark_onboarding_complete is the single write path: config flag AND
    wizard_state.json 'completed' marker."""
    cfg = _make_isolated_config(tmp_path)
    state = {"platforms": {}}
    wiz.mark_onboarding_complete(cfg, state)

    import json

    assert cfg.first_run_complete is True
    assert state["completed"] is True
    persisted = json.loads((tmp_path / "wizard_state.json").read_text())
    assert persisted["completed"] is True
    _assert_persisted_state(tmp_path, expected_first_run=True, completed_state=True)


# ── YouTube GCP guide (`xpst connect youtube --guide`) ────────

YOUTUBE_DOCS_URL = (
    "https://github.com/TysAIs/xPST/blob/main/docs/youtube-oauth-production.md"
)


def _yt_guide_text() -> str:
    return "\n".join(s.text for s in wiz.youtube_gcp_steps())


def test_youtube_gcp_guide_golden_urls():
    text = _yt_guide_text()
    assert "https://console.cloud.google.com/projectcreate" in text
    assert "https://console.cloud.google.com/auth/audience" in text
    assert "https://console.cloud.google.com/auth/clients" in text
    # Legacy credentials UI kept as a fallback path
    assert "https://console.cloud.google.com/apis/credentials" in text


def test_youtube_gcp_guide_publish_to_production_warning():
    text = _yt_guide_text()
    assert "Publish App" in text
    assert "7 DAYS" in text
    assert "already published to production" in text
    assert "docs/youtube-oauth-production.md" in text


def test_youtube_gcp_guide_desktop_client_and_secrets_path():
    text = _yt_guide_text()
    assert "Desktop app" in text
    assert (
        wiz.YOUTUBE_SECRETS_PATH
        == "~/.xpst/credentials/youtube_client_secrets.json"
    )
    assert wiz.YOUTUBE_SECRETS_PATH in text


def test_youtube_guide_single_source_of_truth():
    """PLATFORM_GUIDES, the JSON payload and the markdown renderer must all
    come from the same generator so they cannot drift apart."""
    guide = wiz.PLATFORM_GUIDES["youtube"]
    generated = wiz.youtube_gcp_guide()
    assert guide.title == generated.title
    assert [s.text for s in guide.steps] == [s.text for s in generated.steps]

    payload = wiz.youtube_guide_payload()
    assert payload["steps"] == [s.text for s in guide.steps]
    assert payload["app_status"] == "in_production"
    assert payload["docs_url"] == YOUTUBE_DOCS_URL
    assert payload["docs_file"] == "docs/youtube-oauth-production.md"
    assert payload["client_secrets_path"] == wiz.YOUTUBE_SECRETS_PATH
    assert payload["next_action"] == "xpst connect youtube"


def test_youtube_guide_markdown_reuses_wizard_renderer(tmp_path):
    md = wiz.render_youtube_guide_markdown()
    assert md.startswith("## YouTube Shorts")
    assert f"More details: {YOUTUBE_DOCS_URL}" in md
    assert "https://console.cloud.google.com/projectcreate" in md

    # `xpst wizard --export-md` renders the same youtube section
    out = wiz.export_markdown(tmp_path / "guide.md")
    text = out.read_text()
    assert "https://console.cloud.google.com/projectcreate" in text
    assert YOUTUBE_DOCS_URL in text
