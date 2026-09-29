"""Regression tests for the 2026-09-28 socials-QA residual defect set (D2–D7).

Every test here reproduces one defect logged against the deployed CLI at
main@3282207 (log: missions/work/xpst-redeploy-2026-09-28.md) and pins the
fixed behaviour. No test touches the network or a real account: destination
readiness is local-configuration-only, exactly what the defects were about.

* D2 — ``post --dry-run`` promised ``ready: true`` for a destination that
  live would refuse with TIKTOK_NOT_CONFIGURED (exit 3). The dry run now runs
  the same canonical preflight the live plan and MCP already ran, so a missing
  credential blocks the plan.
* D3 — ``bio`` printed the dashboard API token in plaintext. Masked by
  default, ``--reveal`` for the usable URL.
* D6 — DLQ entries carried no lifecycle fields, aged invisibly, and stored
  errors truncated mid-word. Entries now carry ``status``/``resolved``/
  ``age_hours``, and stored errors truncate at a word boundary with a
  ``… [+N chars]`` marker.
* D7 — the CLI analytics surface had no ``--live``/``--recorded`` while MCP
  exposed ``live=``, and the messenger group had no ``status``.
* D5 (remainder) — MCP ``serverInfo`` reported the mcp library's version,
  not xPST's.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from xpst import __version__ as xpst_version
from xpst.cli import main
from xpst.config import XPSTConfig
from xpst.state import StateManager

if TYPE_CHECKING:
    from pathlib import Path


def _clip(tmp_path: Path) -> Path:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 2048)
    return clip


@pytest.fixture
def config_env(tmp_path: Path) -> tuple[XPSTConfig, Path]:
    config_dir = tmp_path / "state"
    config_path = tmp_path / "config.yaml"
    cfg = XPSTConfig(config_dir=str(config_dir))
    cfg.save(str(config_path))
    return cfg, config_path


def _invoke(config_path: Path, argv: list[str]) -> Any:
    return CliRunner().invoke(main, ["--config", str(config_path), *argv, "--json"], obj={})


def _payload(result: Any) -> dict:
    text = result.output
    start = text.find("{")
    assert start >= 0, f"no JSON in output: {text[:300]!r}"
    return json.loads(text[start:])


# ── D2: a dry run against an under-configured destination is not "ready" ───


def test_post_dry_run_blocks_tiktok_without_credentials(config_env, tmp_path) -> None:
    """The exact probe: `post --video clip -p tiktok --dry-run` promised ready.

    Live fails TIKTOK_NOT_CONFIGURED (exit 3); the plan must say so up front,
    in the blockers list, and report ready:false.
    """
    _, config_path = config_env
    clip = _clip(tmp_path)
    # A shipped config file carries accounts.tiktok.enabled: false; make sure
    # we exercise the *credential* blocker, not the disabled flag, by naming
    # tiktok explicitly and enabling it without a token.
    cfg = XPSTConfig.load(str(config_path))
    cfg.tiktok.enabled = True
    cfg.tiktok.access_token = ""
    cfg.save(str(config_path))

    result = _invoke(config_path, ["post", "--video", str(clip), "--caption", "x", "-p", "tiktok", "--dry-run"])
    payload = _payload(result)
    assert payload["dry_run"] is True
    assert payload["ready"] is False, payload["blockers"]
    assert payload["blockers"], "a destination live would refuse must block the plan"


def test_post_dry_run_ready_when_configured(config_env, tmp_path) -> None:
    """The positive control: a configured, enabled destination is still ready.

    Without this, the D2 fix could "pass" by always saying not-ready.
    """
    _, config_path = config_env
    clip = _clip(tmp_path)
    token_file = tmp_path / "yt_token.json"
    token_file.write_text("{}")
    cfg = XPSTConfig.load(str(config_path))
    cfg.youtube.enabled = True
    cfg.youtube.token_file = str(token_file)
    cfg.save(str(config_path))

    result = _invoke(config_path, ["post", "--video", str(clip), "--caption", "hello", "-p", "youtube", "--dry-run"])
    payload = _payload(result)
    assert payload["ready"] is True, payload["blockers"]


def test_post_dry_run_blocks_disabled_destination(config_env, tmp_path) -> None:
    """An explicitly-named disabled destination is a blocker, not a silent ok."""
    _, config_path = config_env
    clip = _clip(tmp_path)
    cfg = XPSTConfig.load(str(config_path))
    cfg.threads.enabled = False
    cfg.save(str(config_path))

    result = _invoke(config_path, ["post", "--video", str(clip), "--caption", "x", "-p", "threads", "--dry-run"])
    payload = _payload(result)
    assert payload["ready"] is False
    assert any("disabled" in blocker for blocker in payload["blockers"]), payload["blockers"]


# ── D3: bio masks the dashboard API token unless --reveal ──────────────────


def test_bio_masks_token_by_default_and_reveals_on_flag(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    token = "tonu_SECRETishValue42_9FEic"
    with patch("xpst.dashboard.auth.ensure_api_token", return_value=token):
        masked = CliRunner().invoke(main, ["bio", "--json"], obj={}, env={"HOME": str(home)})
        revealed = CliRunner().invoke(main, ["bio", "--reveal", "--json"], obj={}, env={"HOME": str(home)})
    masked_payload = _payload(masked)
    revealed_payload = _payload(revealed)

    assert token not in masked.stdout, "default output leaked the token"
    assert "token=" in masked_payload["edit_url"], "the masked URL must still show a preview"
    assert token in revealed_payload["edit_url"], "--reveal must produce a usable URL"
    assert revealed_payload["edit_url_masked"] == masked_payload["edit_url"]


# ── D6: DLQ lifecycle fields + non-mid-word error truncation ───────────────


def test_dlq_entries_carry_status_resolved_and_age(tmp_path) -> None:
    sm = StateManager(str(tmp_path))
    sm.mark_video_failed("vid-open", "x", "X_UPLOAD_ERROR: boom")
    sm.mark_video_posted("vid-fixed", "youtube", post_id="posted-123")
    sm.mark_video_failed("vid-fixed", "youtube", "transient")
    sm.save()
    fresh = StateManager(str(tmp_path))
    dlq = {entry["video_id"]: entry for entry in fresh.get_dead_letter_queue()}

    assert set(dlq) == {"vid-open", "vid-fixed"}
    open_entry = dlq["vid-open"]
    assert open_entry["status"] == "open"
    assert open_entry["resolved"] is False
    assert open_entry["age_hours"] is not None and open_entry["age_hours"] >= 0

    # A later success on the same platform resolves the failure (history kept).
    fixed = dlq["vid-fixed"]
    assert fixed["status"] == "resolved"
    assert fixed["resolved"] is True


def test_dlq_age_is_unknown_not_zero_for_bad_timestamps(tmp_path) -> None:
    """An unreadable timestamp must read as age=None, never a fabricated 0."""
    from xpst.state_manager import _age_hours

    assert _age_hours("not-a-date") is None
    assert _age_hours(None) is None
    recent = (datetime.now(timezone.utc) - timedelta(hours=3)).replace(tzinfo=None).isoformat()
    age = _age_hours(recent)
    assert age is not None and 2.5 < age < 3.5


def test_truncate_error_never_ends_mid_word_and_marks_loss() -> None:
    from xpst.utils.errors import truncate_error

    message = (
        "X_UPLOAD_ERROR: {'code': 226, 'name': 'AuthorizationError', "
        "'message': 'Authorization failed for media upload'}"
    )
    cut = truncate_error(message, 60)
    assert len(cut) <= len(message)
    assert cut.endswith("]") and "chars]" in cut, "truncation must announce itself"
    # The cut lands on a word boundary: the last kept word is whole.
    kept = cut.split("…")[0]
    assert message.startswith(kept)
    tail = kept.rstrip()
    assert tail[-1].isalnum() is False or message[len(tail)] == " ", "cut mid-word"
    assert truncate_error("short", 600) == "short"


def test_failures_clear_removes_entries(tmp_path) -> None:
    sm = StateManager(str(tmp_path))
    sm.mark_video_failed("vid-a", "x", "boom")
    sm.mark_video_failed("vid-b", "instagram", "bang")
    sm.save()
    with patch("xpst.cli.load_config") as cfg_mock:
        cfg_mock.return_value.config_dir = str(tmp_path)
        one = CliRunner().invoke(main, ["failures", "clear", "vid-a", "--json"], obj={})
        rest = CliRunner().invoke(main, ["failures", "clear", "--json"], obj={})
    assert _payload(one)["cleared"] == 1
    assert _payload(rest)["cleared"] == 1
    assert StateManager(str(tmp_path)).get_dead_letter_queue() == []


# ── D7: CLI parity for the analytics live flag; messenger status ───────────


def test_analytics_live_and_recorded_flags_exist() -> None:
    """The unknown-option defect was `analytics --live` → exit 2."""
    from xpst.cli import analytics as analytics_cmd

    names = {param.name for param in analytics_cmd.params}
    assert {"live", "recorded", "refresh", "cross_post"} <= names


def test_analytics_recorded_makes_no_network_and_reports_live_false(tmp_path) -> None:
    with patch("xpst.cli.load_config") as cfg_mock, patch("xpst.analytics.AnalyticsCollector.collect_all") as collect:
        cfg_mock.return_value.config_dir = str(tmp_path)
        result = CliRunner().invoke(main, ["analytics", "--recorded", "--json"], obj={})
    assert result.exit_code == 0, result.output
    collect.assert_not_called()
    assert _payload(result)["live"] is False


def test_analytics_rejects_live_and_recorded_together(tmp_path) -> None:
    with patch("xpst.cli.load_config") as cfg_mock:
        cfg_mock.return_value.config_dir = str(tmp_path)
        result = CliRunner().invoke(main, ["analytics", "--live", "--recorded", "--json"], obj={})
    assert result.exit_code != 0
    assert "opposites" in result.output


def _config_with_dir(tmp_path: Path, **messenger_overrides) -> XPSTConfig:
    cfg = XPSTConfig(config_dir=str(tmp_path))
    for name, value in messenger_overrides.items():
        setattr(cfg.messenger, name, value)
    return cfg


def test_messenger_status_reports_readiness_without_credentials(tmp_path) -> None:
    with patch("xpst.cli.load_config", return_value=_config_with_dir(tmp_path, enabled=False)):
        result = CliRunner().invoke(main, ["messenger", "status", "--json"], obj={})
    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["enabled"] is False
    assert payload["token_configured"] is False
    assert payload["token_preview"] is None
    assert payload["webhook_path"]


def test_messenger_status_masks_page_token(tmp_path) -> None:
    secret = "EAAH_secretpagtoken_zzz9FEic"
    with patch("xpst.cli.load_config", return_value=_config_with_dir(tmp_path, enabled=True)), \
         patch("xpst.utils.credentials.CredentialStore") as store_cls:
        store_cls.return_value.retrieve.return_value = secret
        result = CliRunner().invoke(main, ["messenger", "status", "--json"], obj={})
    assert secret not in result.output, "status leaked the page token"
    payload = _payload(result)
    assert payload["token_configured"] is True
    assert secret[:8] in payload["token_preview"] and secret[-5:] in payload["token_preview"]


# ── D5 remainder: MCP serverInfo identifies xPST, not the mcp library ──────


def test_mcp_server_info_reports_xpst_version() -> None:
    pytest.importorskip("mcp")
    from xpst.mcp.server import app

    options = app.create_initialization_options()
    assert options.server_version == xpst_version
    assert options.server_name == "xpst-mcp"
