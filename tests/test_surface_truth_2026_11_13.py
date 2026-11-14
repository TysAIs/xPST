"""Surfaces must not imply a capability or a currency they do not have.

Closes two named findings from the 2026-11-12 private-proof readiness report:

* **F2** — ``--visibility private`` was accepted for every destination and the
  plan reported ``ready: true``, while only YouTube forwards a visibility at
  upload time. "Nothing gets posted publicly" was therefore a scope decision,
  not a flag, and nothing in the output said so.
* **F7** — ``xpst status``'s ``platform_health`` is local post history, not a
  live check: TikTok carried 195 "failures" from a retired API path while
  ``xpst doctor`` reported it ready. The payload and the human output now say
  which one is current.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from xpst.cli import _visibility_advisories
from xpst.config import XPSTConfig
from xpst.state_manager import StateManager

if TYPE_CHECKING:
    from pathlib import Path


def _config(tmp_path: Path) -> XPSTConfig:
    config = XPSTConfig(config_dir=str(tmp_path / "state"))
    for name in ("youtube", "x", "instagram", "tiktok", "threads"):
        getattr(config, name).enabled = False
    return config


def _local_media(tmp_path: Path) -> Path:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"not a real video")
    return clip


def test_visibility_advisories_name_the_destinations_that_ignore_it() -> None:
    """Only YouTube honours the flag, so X/TikTok must be named, not assumed."""
    assert _visibility_advisories(["youtube"], "private") == []
    assert _visibility_advisories(["youtube", "x"], "public") == []

    notes = _visibility_advisories(["youtube", "x", "tiktok"], "private")
    assert len(notes) == 1
    assert "x, tiktok cannot honour --visibility private" in notes[0]
    assert "PUBLIC" in notes[0]
    assert "youtube honours the flag" in notes[0]


def test_post_dry_run_reports_visibility_it_cannot_honour(tmp_path: Path) -> None:
    """F2: the dry-run payload carries the advisory instead of a bare ready:true."""
    from click.testing import CliRunner

    from xpst.cli import main

    config = _config(tmp_path)
    config.youtube.enabled = True
    config.x.enabled = True
    token = tmp_path / "youtube-token.json"
    token.write_text("{}")
    config.youtube.token_file = str(token)
    cookies = tmp_path / "x-cookies.json"
    cookies.write_text("{}")
    config.x.cookies_file = str(cookies)
    config_path = tmp_path / "config.yaml"
    config.save(str(config_path))
    media = _local_media(tmp_path)

    runner = CliRunner()
    refused = runner.invoke(
        main,
        [
            "--config", str(config_path), "post", "-v", str(media), "-c", "caption",
            "-p", "youtube,x", "--visibility", "private", "--dry-run", "--json",
        ],
        obj={},
    )
    assert refused.exit_code == 0, refused.output
    payload = json.loads(refused.output)
    assert payload["visibility_warnings"], "X cannot honour --visibility private"
    assert "x cannot honour --visibility private" in payload["visibility_warnings"][0]

    honoured = runner.invoke(
        main,
        [
            "--config", str(config_path), "post", "-v", str(media), "-c", "caption",
            "-p", "youtube", "--visibility", "private", "--dry-run", "--json",
        ],
        obj={},
    )
    assert honoured.exit_code == 0, honoured.output
    assert "visibility_warnings" not in json.loads(honoured.output), (
        "YouTube honours --visibility, so there is nothing to warn about — and the "
        "payload shape for the default path must stay unchanged"
    )


def test_status_platform_health_is_labelled_historical(tmp_path: Path) -> None:
    """F7: the counters are history; the payload says so and points at doctor."""
    state = StateManager(str(tmp_path / "state"))
    state.update_platform_health("tiktok", False)

    stats = state.get_statistics()

    assert stats["platform_health"]["tiktok"]["historical"] is True
    assert stats["platform_health"]["tiktok"]["source"] == "state_history"
    assert "xpst doctor" in stats["platform_health_note"]


def test_status_human_output_says_which_surface_is_current(tmp_path: Path) -> None:
    """The human surface must not present history as live readiness."""
    from click.testing import CliRunner

    from xpst import cli as cli_module
    from xpst.cli import main

    config = _config(tmp_path)
    config_path = tmp_path / "config.yaml"
    config.save(str(config_path))
    original = cli_module._stdout_is_tty
    cli_module._stdout_is_tty = lambda: True
    try:
        result = CliRunner().invoke(main, ["--config", str(config_path), "status"], obj={})
    finally:
        cli_module._stdout_is_tty = original

    assert result.exit_code == 0, result.output
    assert "Platform Health (historical" in result.output
    assert "xpst doctor" in result.output
