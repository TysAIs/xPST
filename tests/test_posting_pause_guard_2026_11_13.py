"""A paused deployment must not be able to queue a post behind the operator.

Finding F3 of the 2026-11-12 private-proof readiness report: ``xpst post``
without ``--now``, fired outside the 08:00-23:00 anti-bot window, defers to the
schedule store — which CREATES ``~/.xpst/schedule.json``. During the posting
pause that is a schedule change nobody asked for, and the readiness sequence
avoided it only by always passing ``--now``.

The guard lives at the deferral itself (``services/manual_defer.py``), which is
the single place an implicit post becomes a schedule entry, so every surface
that defers inherits it. It refuses: nothing is queued, nothing is uploaded, and
the verdict says so. An explicit ``--now`` still publishes — the pause forbids
silent deferral, not a deliberate manual action.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from xpst.config import XPSTConfig
from xpst.content import PUBLISH_ROUTE_VIDEO, ContentRequest
from xpst.services.manual_defer import defer_manual_post_to_schedule, posting_pause_reason


def _config(tmp_path: Path, *, paused: bool) -> XPSTConfig:
    config = XPSTConfig(config_dir=str(tmp_path / "state"))
    for name in ("youtube", "x", "instagram", "tiktok", "threads"):
        getattr(config, name).enabled = name == "youtube"
    token = tmp_path / "youtube-token.json"
    token.write_text("{}")
    config.youtube.token_file = str(token)
    config.schedule.posting_paused = paused
    return config


def _request(tmp_path: Path) -> ContentRequest:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"not a real video")
    return ContentRequest.from_legacy([clip], "caption", ["youtube"], content_type="video")


def _close_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("xpst.anti_bot.AntiBotProtection.should_post_now", lambda self: False)
    monkeypatch.setattr(
        "xpst.anti_bot.AntiBotProtection.next_posting_time",
        lambda self: datetime.now() + timedelta(hours=8),
    )
    monkeypatch.delenv("XPST_POSTING_PAUSED", raising=False)


def test_paused_deferral_refuses_and_writes_no_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3: the queue stays empty because the file is never created."""
    _close_the_window(monkeypatch)
    config = _config(tmp_path, paused=True)

    verdict = defer_manual_post_to_schedule(config, _request(tmp_path), PUBLISH_ROUTE_VIDEO)

    assert verdict is not None
    assert verdict["paused"] is True
    assert verdict["deferred"] is False
    assert verdict["scheduled"] is None
    assert verdict["reason"] == "posting_paused"
    assert "NOT queued" in verdict["message"]
    assert not (Path(config.config_dir) / "schedule.json").exists(), (
        "a paused deployment must never create the schedule store"
    )


def test_unpaused_deferral_still_queues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard is the pause, not a broken deferral."""
    _close_the_window(monkeypatch)
    config = _config(tmp_path, paused=False)

    verdict = defer_manual_post_to_schedule(config, _request(tmp_path), PUBLISH_ROUTE_VIDEO)

    assert verdict is not None
    assert verdict["deferred"] is True
    assert verdict["scheduled"]["id"]
    assert (Path(config.config_dir) / "schedule.json").exists()


def test_env_var_pauses_without_editing_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A lane-level pause is applied by the runner, not only the config owner."""
    config = _config(tmp_path, paused=False)
    monkeypatch.setenv("XPST_POSTING_PAUSED", "1")
    assert posting_pause_reason(config) == "XPST_POSTING_PAUSED is set"

    monkeypatch.setenv("XPST_POSTING_PAUSED", "off")
    assert posting_pause_reason(config) == ""


def test_posting_paused_round_trips_through_save_and_load(tmp_path: Path) -> None:
    """A pause must survive a config save, or it silently unpauses itself."""
    config = _config(tmp_path, paused=True)
    path = tmp_path / "config.yaml"
    config.save(str(path))

    reloaded = XPSTConfig.load(str(path))
    assert reloaded.schedule.posting_paused is True


def _json_payload(output: str) -> dict:
    """The JSON document a CLI run printed (INFO log lines may precede it)."""
    for line in reversed(output.strip().splitlines()):
        if line.lstrip().startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no JSON payload in output:\n{output}")


def test_cli_post_outside_window_is_refused_not_queued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3 at the surface a human/agent actually runs."""
    from click.testing import CliRunner

    from xpst.cli import main

    _close_the_window(monkeypatch)
    config = _config(tmp_path, paused=True)
    config_path = tmp_path / "config.yaml"
    config.save(str(config_path))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"not a real video")

    result = CliRunner().invoke(
        main,
        [
            "--config", str(config_path), "post", "-v", str(clip),
            "-c", "caption", "-p", "youtube", "--json",
        ],
        obj={},
    )

    assert result.exit_code == 1, result.output
    payload = _json_payload(result.output)
    assert payload["paused"] is True
    assert payload["deferred"] is False
    assert payload["reason"] == "posting_paused"
    assert not (Path(config.config_dir) / "schedule.json").exists()


def test_cli_dry_run_says_refused_instead_of_would_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dry run must not promise a queue the real run refuses."""
    from click.testing import CliRunner

    from xpst.cli import main

    _close_the_window(monkeypatch)
    config = _config(tmp_path, paused=True)
    config_path = tmp_path / "config.yaml"
    config.save(str(config_path))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"not a real video")

    result = CliRunner().invoke(
        main,
        [
            "--config", str(config_path), "post", "-v", str(clip), "-c", "caption",
            "-p", "youtube", "--dry-run", "--json",
        ],
        obj={},
    )

    assert result.exit_code == 0, result.output
    payload = _json_payload(result.output)
    assert payload["deferral"]["paused"] is True
    assert payload["deferral"]["deferred_would_apply"] is False
    assert payload["deferral"]["reason"] == "posting_paused"


@pytest.mark.asyncio
async def test_mcp_post_refuses_when_paused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F3 on the agent surface: refused, and reported as an error result."""
    from unittest.mock import MagicMock

    from xpst.mcp.server import _handle_post, result_is_error

    _close_the_window(monkeypatch)
    monkeypatch.setenv("XPST_MCP_ALLOW_MUTATIONS", "1")

    config = _config(tmp_path, paused=True)
    engine = MagicMock()
    engine._platforms = {"youtube": MagicMock()}
    engine.config = config
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"not a real video")

    result = await _handle_post(
        engine,
        {"video_path": str(clip), "caption": "caption", "platforms": ["youtube"]},
    )

    payload = json.loads(result.content[0].text)
    assert payload["paused"] is True
    assert payload["deferred"] is False
    assert result_is_error(result) is True, (
        "an agent that only checks isError must not read a refused post as a success"
    )
    assert not (Path(config.config_dir) / "schedule.json").exists()
