"""The preflight plan must tell the truth about the file it would upload.

Closes the named findings from the 2026-11-12 private-proof readiness report:

* **F1** — ``xpst preflight --transform`` was inert: the flag reached
  ``build_route_plan(include_transform=…)`` and the service computed the
  rendition decision, but ``RouteDestination`` never serialised it, so the
  JSON carried nothing while ``verify-media --plan`` reported the same
  decision correctly. The working path is no longer the only path.
* **F4** — the audio-rate advisory reads like a defect the user must fix even
  though every destination resamples and refuses nothing; one file cannot be
  48 kHz (YouTube) and 44.1 kHz (X/TikTok) at once.
* **F6** — a soft ceiling (4K dimensions, 40 Mbps) only warned, and the
  warning was JSON-only: a human reading "ready" had no way to learn the
  destination will do its own re-encode.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from xpst.config import XPSTConfig
from xpst.media.pipeline import TransformPlan
from xpst.media.specs import verify_media
from xpst.services import post_preflight, route_plan

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _youtube_config(tmp_path: Path) -> XPSTConfig:
    config = XPSTConfig(config_dir=str(tmp_path / "state"))
    for name in ("youtube", "x", "instagram", "tiktok", "threads"):
        getattr(config, name).enabled = name == "youtube"
    token = tmp_path / "youtube-token.json"
    token.write_text("{}")
    config.youtube.token_file = str(token)
    return config


def _media(tmp_path: Path) -> Path:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"not a real video")
    return clip


def test_route_plan_serialises_the_transform_it_was_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: ``include_transform=True`` must reach the plan the caller receives."""

    def fake_transform(self: Any, path: Path, platform: str, config: XPSTConfig) -> TransformPlan:
        return TransformPlan(platform=platform, action="transcode", reasons=["test rendition decision"])

    monkeypatch.setattr(post_preflight.PostPreflightService, "_transform", fake_transform)

    media = _media(tmp_path)
    config = _youtube_config(tmp_path)
    payload = route_plan.build_route_plan(
        config,
        media_paths=[str(media)],
        caption="caption",
        content_type="video",
        requested=["youtube"],
        include_transform=True,
    ).to_dict()

    destination = payload["destinations"][0]
    assert destination["transform"] is not None, "the rendition decision must be serialised"
    assert destination["transform"]["action"] == "transcode"
    assert destination["transform"]["reasons"] == ["test rendition decision"]
    assert destination["transforms"] == []


def test_route_plan_does_not_claim_a_transform_it_did_not_compute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent means "not computed", never "passthrough"."""
    calls: list[str] = []

    def fake_transform(self: Any, path: Path, platform: str, config: XPSTConfig) -> TransformPlan:
        calls.append(platform)
        return TransformPlan(platform=platform, action="passthrough")

    monkeypatch.setattr(post_preflight.PostPreflightService, "_transform", fake_transform)

    media = _media(tmp_path)
    payload = route_plan.build_route_plan(
        _youtube_config(tmp_path),
        media_paths=[str(media)],
        caption="caption",
        content_type="video",
        requested=["youtube"],
        include_transform=False,
    ).to_dict()

    destination = payload["destinations"][0]
    assert destination["transform"] is None
    assert destination["transforms"] == []
    assert calls == [], "the rendition plan must not be computed when it was not requested"


def test_preflight_transform_flag_reaches_the_cli_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1, end to end through the Click surface: the flag is no longer inert."""
    from click.testing import CliRunner

    from xpst.cli import main

    def fake_transform(self: Any, path: Path, platform: str, config: XPSTConfig) -> TransformPlan:
        return TransformPlan(platform=platform, action="remux", reasons=["faststart remux only"])

    monkeypatch.setattr(post_preflight.PostPreflightService, "_transform", fake_transform)

    media = _media(tmp_path)
    config_path = tmp_path / "config.yaml"
    _youtube_config(tmp_path).save(str(config_path))

    runner = CliRunner()
    json_result = runner.invoke(
        main,
        ["--config", str(config_path), "preflight", str(media), "-p", "youtube", "--transform", "--json"],
        obj={},
    )
    assert json_result.exit_code == 0, json_result.output
    payload = json.loads(json_result.output)
    assert payload["destinations"][0]["transform"]["action"] == "remux"


def test_human_route_plan_shows_advisories_and_the_transform(capsys: pytest.CaptureFixture[str]) -> None:
    """F6: the human surface must not hide an advisory the JSON carries.

    ``preflight``/``post --dry-run`` are JSON by default on a non-TTY (which is
    what a script or an agent sees), so this asserts the human printer directly
    instead of going through Click.
    """
    from xpst import cli as cli_module

    cli_module._print_route_plan(
        {
            "destinations": [
                {
                    "platform": "youtube",
                    "state": "posted",
                    "reason": "will publish",
                    "warnings": [
                        {
                            "code": "MEDIA_SPEC_DIMENSIONS",
                            "message": "2160x3840 vs max 1920 — advisory: YouTube re-encodes above its ceiling",
                        }
                    ],
                    "transform": {"action": "remux", "reasons": ["faststart remux only"]},
                }
            ]
        }
    )
    captured = capsys.readouterr().out
    assert "advisory (MEDIA_SPEC_DIMENSIONS)" in captured
    assert "re-encodes above its ceiling" in captured
    assert "transform: remux — faststart remux only" in captured


def test_preflight_cli_human_output_is_used_when_stdout_is_a_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The advisory printer is wired into the ``preflight`` human branch."""

    def fake_transform(self: Any, path: Path, platform: str, config: XPSTConfig) -> TransformPlan:
        return TransformPlan(platform=platform, action="remux", reasons=["faststart remux only"])

    monkeypatch.setattr(post_preflight.PostPreflightService, "_transform", fake_transform)
    monkeypatch.setattr("xpst.cli._stdout_is_tty", lambda: True)

    media = _media(tmp_path)
    config_path = tmp_path / "config.yaml"
    _youtube_config(tmp_path).save(str(config_path))

    from click.testing import CliRunner

    from xpst.cli import main

    result = CliRunner().invoke(
        main,
        ["--config", str(config_path), "preflight", str(media), "-p", "youtube", "--transform"],
        obj={},
    )
    assert result.exit_code == 0, result.output
    assert "transform: remux — faststart remux only" in result.output


def test_soft_ceiling_and_audio_rate_advisories_explain_themselves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4/F6: an advisory says what will happen, and never reads as a refusal."""
    info = {
        "format": {"format_name": "mov,mp4", "duration": "8.0", "bit_rate": "40600000"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
                "width": 2160,
                "height": 3840,
                "avg_frame_rate": "30/1",
                "bit_rate": "40000000",
            },
            {"codec_type": "audio", "codec_name": "aac", "sample_rate": "44100"},
        ],
    }
    monkeypatch.setattr("xpst.media.specs.get_video_info_standalone", lambda path: info)

    report = verify_media(_media(tmp_path), "youtube", check_loudness=False)
    by_name = {check.name: check for check in report.checks}

    assert report.ok, "soft ceilings must never block the upload"
    assert by_name["dimensions"].status == "warn"
    assert "advisory" in by_name["dimensions"].detail
    assert "re-encodes" in by_name["dimensions"].detail
    assert by_name["video_bitrate"].status == "warn"
    assert "re-encodes" in by_name["video_bitrate"].detail
    assert by_name["audio_rate"].status == "warn"
    assert "advisory only" in by_name["audio_rate"].detail
    assert "resamples" in by_name["audio_rate"].detail
