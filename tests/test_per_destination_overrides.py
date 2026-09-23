"""Per-destination caption overrides, end to end.

``per_platform_captions`` existed in the preflight service and was never passed:
the engine sent one caption to every destination, the plan reported the shared
caption for each destination as if it were the copy that would be sent, and an
override that a destination cannot take was truncated silently inside the
uploader (X at 280, Instagram/TikTok at 2200).

These tests pin the whole path — contract, engine, preflight/service, MCP — and
the honest refusal that replaces silent truncation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from xpst.content import (
    ContentContractError,
    ContentRequest,
    DestinationOverride,
    parse_destination_texts,
)
from xpst.engine import caption_for_destination
from xpst.platforms.base import UploadResult

VIDEO_BYTES = b"\x00" * 4096


# ── Contract: one way to say "this destination gets different copy" ─────────


def test_text_for_uses_the_override_and_keeps_the_shared_text_elsewhere() -> None:
    request = ContentRequest(
        text=" shared caption ",
        platforms=("youtube", "x", "threads"),
        overrides={"X": DestinationOverride(text="x-only\nwith exact spacing")},
    )

    assert request.text_for("x") == "x-only\nwith exact spacing", "the override is verbatim"
    assert request.text_for("youtube") == " shared caption "
    assert request.text_for("threads") == " shared caption "
    assert request.per_platform_texts() == {"x": "x-only\nwith exact spacing"}


def test_per_platform_texts_omits_destinations_that_keep_the_default() -> None:
    request = ContentRequest(
        text="shared",
        platforms=("youtube", "x"),
        overrides={"x": DestinationOverride(text="")},
    )

    # An explicit empty caption IS an override (the user asked for nothing);
    # only destinations with no override are absent.
    assert request.per_platform_texts() == {"x": ""}
    assert request.per_platform_texts(("youtube",)) == {}


def test_parse_destination_texts_accepts_both_documented_shapes() -> None:
    assert parse_destination_texts({"x": "short copy"}) == {"x": "short copy"}
    assert parse_destination_texts({"X": {"text": "short copy"}}) == {"x": "short copy"}
    assert parse_destination_texts({"threads": {"caption": "500 max"}}) == {"threads": "500 max"}
    assert parse_destination_texts(None) == {}
    assert parse_destination_texts({"x": {"content_type": "video"}}) == {}


def test_parse_destination_texts_rejects_a_payload_it_cannot_honour() -> None:
    with pytest.raises(ContentContractError):
        parse_destination_texts({"x": ["not a caption"]})


def test_request_payload_accepts_the_per_platform_captions_key() -> None:
    request = ContentRequest.from_payload(
        {"media": ["clip.mp4"], "caption": "shared", "platforms": ["x"], "per_platform_captions": {"x": "short"}}
    )
    assert request.per_platform_texts() == {"x": "short"}


def test_caption_for_destination_matches_case_insensitively() -> None:
    assert caption_for_destination("X", "shared", {"x": "short"}) == "short"
    assert caption_for_destination("x", "shared", {"YOUTUBE": "yt"}) == "shared"
    assert caption_for_destination("x", "shared", None) == "shared"


# ── Engine: each uploader receives its own destination's copy ───────────────


def _make_engine(tmp_path: Path) -> Any:
    from xpst.config import XPSTConfig
    from xpst.engine import CrossPostEngine

    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.video.download_dir = str(tmp_path / "downloads")
    (tmp_path / "downloads").mkdir(parents=True, exist_ok=True)
    for name in ("youtube", "x", "instagram", "tiktok", "threads"):
        getattr(config, name).enabled = False
    return CrossPostEngine(config)


def _recording_uploader(platform: str) -> MagicMock:
    uploader = MagicMock()
    uploader.platform_name = platform
    return uploader


class _RecordingUploadService:
    """Records the caption the engine handed each destination's uploader."""

    def __init__(self) -> None:
        self.captions: dict[str, str] = {}
        #: The visibility each upload received (YouTube honours it; the rest
        #: get None) — proof the option reached the upload layer.
        self.visibility_by_platform: dict[str, str | None] = {}

    async def upload_to_platform(
        self, *, uploader: Any, video_path: Any, caption: str, platform_name: str, video_id: str,
        source_platform: str = "", visibility: str | None = None,
    ) -> UploadResult:
        # `visibility` arrives from post_manual (YouTube-only option); the
        # double records it so a test can prove it reached the upload layer.
        self.visibility_by_platform[platform_name] = visibility
        self.captions[platform_name] = caption
        return UploadResult(
            success=True,
            post_id=f"{platform_name}-1",
            post_url=f"https://example.invalid/{platform_name}/1",
            platform=platform_name,
        )

    async def upload_carousel_to_platform(
        self, *, uploader: Any, media_paths: Any, caption: str, platform_name: str, video_id: str, source_platform: str = ""
    ) -> UploadResult:
        self.captions[platform_name] = caption
        return UploadResult(
            success=True,
            post_id=f"{platform_name}-c1",
            post_url=f"https://example.invalid/{platform_name}/c1",
            platform=platform_name,
        )


class _NoopAnalyticsStore:
    """Keeps the cross-post group write out of the real user config dir."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        pass

    def record_cross_post_group(self, **kwargs: Any) -> None:  # noqa: ARG002
        pass


@pytest.fixture
def isolated_engine(tmp_path: Path, monkeypatch: Any) -> Any:
    monkeypatch.setattr("xpst.analytics_store.AnalyticsStore", _NoopAnalyticsStore)
    engine = _make_engine(tmp_path)
    engine._platforms["youtube"] = _recording_uploader("youtube")
    engine._platforms["x"] = _recording_uploader("x")
    engine.upload_service = _RecordingUploadService()
    return engine


@pytest.mark.asyncio
async def test_post_manual_sends_each_destination_its_own_caption(tmp_path: Path, isolated_engine: Any) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(VIDEO_BYTES)

    result = await isolated_engine.post_manual(
        video, "shared caption", ["youtube", "x"], {"x": "x-only caption"}
    )

    assert isolated_engine.upload_service.captions == {
        "youtube": "shared caption",
        "x": "x-only caption",
    }
    assert result.captions == {"youtube": "shared caption", "x": "x-only caption"}


@pytest.mark.asyncio
async def test_post_manual_keeps_the_shared_caption_when_no_overrides_are_given(
    tmp_path: Path, isolated_engine: Any
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(VIDEO_BYTES)

    await isolated_engine.post_manual(video, "shared caption", ["youtube", "x"])

    assert isolated_engine.upload_service.captions == {
        "youtube": "shared caption",
        "x": "shared caption",
    }


@pytest.mark.asyncio
async def test_post_manual_carousel_sends_each_destination_its_own_caption(
    tmp_path: Path, isolated_engine: Any
) -> None:
    first = tmp_path / "one.mp4"
    second = tmp_path / "two.mp4"
    first.write_bytes(VIDEO_BYTES)
    second.write_bytes(VIDEO_BYTES)

    result = await isolated_engine.post_manual_carousel(
        [first, second], "shared caption", ["youtube", "x"], {"x": "x-only"}
    )

    assert isolated_engine.upload_service.captions == {"youtube": "shared caption", "x": "x-only"}
    assert result.captions["x"] == "x-only"


@pytest.mark.asyncio
async def test_post_request_plumbs_overrides_through_the_typed_contract(
    tmp_path: Path, isolated_engine: Any
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(VIDEO_BYTES)
    request = ContentRequest(
        media=(str(video),),
        text="shared caption",
        platforms=("youtube", "x"),
        overrides={"x": DestinationOverride(text="x-only caption")},
    )

    result = await isolated_engine.post_request(request)

    assert isolated_engine.upload_service.captions == {
        "youtube": "shared caption",
        "x": "x-only caption",
    }
    assert result.captions["x"] == "x-only caption"


# ── Preflight: the copy that is validated is the copy that is sent ──────────


def _preflight_config(tmp_path: Path) -> Any:
    """youtube + threads + x enabled and locally ready, for plan-level tests."""
    from xpst.config import XPSTConfig

    config = XPSTConfig(config_dir=str(tmp_path / "state"))
    for name in ("youtube", "x", "instagram", "tiktok", "threads"):
        getattr(config, name).enabled = name in {"youtube", "x", "threads"}
    token = tmp_path / "youtube-token.json"
    token.write_text("{}")
    config.youtube.token_file = str(token)
    cookies = tmp_path / "x-cookies.json"
    cookies.write_text("{}")
    config.x.cookies_file = str(cookies)
    config.x.auth_mode = "cookies"
    config.threads.graph_access_token = "local-test-token"
    config.threads.threads_user_id = "123"
    return config


def _plan(tmp_path: Path, *, base_caption: str, overrides: dict[str, str], platforms: tuple[str, ...]) -> Any:
    from xpst.services.post_preflight import PostPlanRequest, PostPreflightService

    media = tmp_path / "clip.mp4"
    media.write_bytes(VIDEO_BYTES)
    config = _preflight_config(tmp_path)
    return PostPreflightService(config).plan(
        PostPlanRequest(
            media_paths=(media,),
            target_platforms=platforms,
            base_caption=base_caption,
            per_platform_captions=overrides,
            config=config,
        )
    )


def test_preflight_uses_the_override_for_its_destination_and_the_default_elsewhere(tmp_path: Path) -> None:
    result = _plan(
        tmp_path,
        base_caption="shared caption",
        overrides={"x": "x-only caption"},
        platforms=("youtube", "x"),
    )

    assert result.platforms["x"].effective_caption == "x-only caption"
    assert result.platforms["youtube"].effective_caption == "shared caption"


def test_an_override_over_a_destination_limit_is_refused_with_the_destination_named(tmp_path: Path) -> None:
    result = _plan(
        tmp_path,
        base_caption="shared caption",
        overrides={"threads": "t" * 501},
        platforms=("youtube", "threads"),
    )

    threads_issues = [issue for issue in result.platforms["threads"].hard_blockers if issue.code == "CAPTION_TOO_LONG"]
    assert len(threads_issues) == 1, result.platforms["threads"].hard_blockers
    assert "threads" in threads_issues[0].message
    assert "501" in threads_issues[0].message
    assert result.ready is False
    assert any("threads" in issue["message"] for issue in result.to_dict()["hard_blockers"])

    # The destination that kept the shared caption is untouched by it.
    assert [issue for issue in result.platforms["youtube"].hard_blockers if issue.code == "CAPTION_TOO_LONG"] == []
    assert result.platforms["youtube"].effective_caption == "shared caption"


def test_a_long_shared_caption_is_not_refused_when_the_destination_has_a_short_override(tmp_path: Path) -> None:
    """The override is what gets length-checked — not the shared caption."""
    result = _plan(
        tmp_path,
        base_caption="s" * 501,
        overrides={"threads": "fits"},
        platforms=("threads",),
    )

    assert [issue for issue in result.platforms["threads"].hard_blockers if issue.code == "CAPTION_TOO_LONG"] == []
    assert result.platforms["threads"].effective_caption == "fits"


def test_caption_limits_match_the_uploaders_own_declaration() -> None:
    """One vocabulary: the preflight limit IS the uploader's declared limit."""
    from xpst.config import XPSTConfig
    from xpst.platforms.instagram import InstagramUploader
    from xpst.platforms.threads import ThreadsUploader
    from xpst.platforms.tiktok import TikTokUploader
    from xpst.platforms.x import XUploader
    from xpst.services.post_preflight import _CAPTION_LIMITS

    assert _CAPTION_LIMITS["instagram"] == InstagramUploader.MAX_CAPTION_LENGTH
    assert _CAPTION_LIMITS["tiktok"] == TikTokUploader.MAX_CAPTION_LENGTH
    assert _CAPTION_LIMITS["threads"] == ThreadsUploader.MAX_CAPTION_LENGTH
    assert _CAPTION_LIMITS["x"] == XUploader(XPSTConfig()).manifest.extra["max_caption_length"]


# ── PostService: the HTTP/UI/MCP dry-run surface ────────────────────────────


def _service_config_dir(tmp_path: Path) -> str:
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    (media / "clip.mp4").write_bytes(VIDEO_BYTES)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(exist_ok=True)
    token = tmp_path / "youtube-token.json"
    token.write_text("{}")
    cookies = tmp_path / "x-cookies.json"
    cookies.write_text("{}")
    config = {
        "version": 4,
        "accounts": {
            "local": {"path": str(media)},
            "youtube": {"enabled": True, "token_file": str(token)},
            "x": {"enabled": True, "auth_mode": "cookies", "cookies_file": str(cookies)},
            "threads": {"enabled": True, "graph_access_token": "fixture", "threads_user_id": "123"},
        },
        "video": {"download_dir": str(tmp_path / "downloads")},
        "monitoring": {},
    }
    (cfg_dir / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return str(cfg_dir)


def _post_service(tmp_path: Path, engine: Any = None) -> Any:
    from xpst.config import XPSTConfig
    from xpst.services.post_service import PostService

    config_dir = _service_config_dir(tmp_path)
    config = XPSTConfig.load(str(Path(config_dir) / "config.yaml"))
    return PostService(config, config_dir, engine_factory=lambda _cfg: engine)


class _FakeEngine:
    """Engine double that records the per-destination copy it was asked for."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def _result(self, caption: str, platforms: list[str], overrides: dict[str, str]) -> Any:
        from xpst.engine import CrossPostResult

        return CrossPostResult(
            video_id="vid-1",
            caption=caption,
            results={
                platform: UploadResult(
                    success=True,
                    post_id=f"{platform}-1",
                    post_url=f"https://example.invalid/{platform}/1",
                    platform=platform,
                )
                for platform in platforms
            },
            captions={platform: overrides.get(platform, caption) for platform in platforms},
        )

    async def post_manual(
        self, video_path: Any, caption: str, platforms: list[str] | None = None, per_platform_captions: Any = None
    ) -> Any:
        overrides = dict(per_platform_captions or {})
        self.calls.append({"caption": caption, "overrides": overrides})
        return self._result(caption, list(platforms or []), overrides)

    async def post_manual_carousel(
        self, media_paths: Any, caption: str, platforms: list[str] | None = None, per_platform_captions: Any = None
    ) -> Any:
        return await self.post_manual(media_paths[0], caption, platforms, per_platform_captions)


def test_service_preflight_reports_the_override_and_refuses_an_over_limit_one(tmp_path: Path) -> None:
    from xpst.services.post_service import PostRequest

    service = _post_service(tmp_path)
    media = str(tmp_path / "media" / "clip.mp4")
    verdict = service.preflight(
        PostRequest.from_payload(
            {
                "media_paths": [media],
                "caption": "shared caption",
                "platforms": ["youtube", "threads"],
                "overrides": {"threads": "t" * 501},
            }
        )
    )

    assert verdict["ready"] is False
    assert any("threads" in blocker and "501" in blocker for blocker in verdict["blockers"]), verdict["blockers"]
    assert verdict["captions"] == {"youtube": "shared caption", "threads": "t" * 501}
    plan = verdict["plan"]["platforms"]
    assert plan["threads"]["effective_caption"] == "t" * 501
    assert plan["threads"]["constraints"]["caption"]["max_characters"] == 500
    assert plan["youtube"]["effective_caption"] == "shared caption"


def test_service_execute_hands_each_destination_its_own_caption(tmp_path: Path) -> None:
    from xpst.services.post_service import PostRequest

    engine = _FakeEngine()
    service = _post_service(tmp_path, engine)
    media = str(tmp_path / "media" / "clip.mp4")

    envelope = service.execute(
        PostRequest.from_payload(
            {
                "media_paths": [media],
                "caption": "shared caption",
                "platforms": ["youtube", "x"],
                "overrides": {"x": {"text": "x-only caption"}},
            }
        )
    )

    assert envelope["ok"] is True, envelope["blockers"]
    assert engine.calls[0]["overrides"] == {"x": "x-only caption"}
    assert envelope["captions"] == {"youtube": "shared caption", "x": "x-only caption"}
    assert [row["caption"] for row in envelope["destinations"]] == ["shared caption", "x-only caption"]


def test_service_refuses_an_override_aimed_at_a_destination_that_was_not_requested(tmp_path: Path) -> None:
    from xpst.services.post_service import PostRequest

    service = _post_service(tmp_path)
    media = str(tmp_path / "media" / "clip.mp4")
    verdict = service.preflight(
        PostRequest.from_payload(
            {
                "media_paths": [media],
                "caption": "shared caption",
                "platforms": ["youtube"],
                "overrides": {"x": "x-only caption"},
            }
        )
    )

    assert verdict["ready"] is False
    assert any("x" in blocker and "not a requested destination" in blocker for blocker in verdict["blockers"])


# ── MCP: same shape, same refusal ───────────────────────────────────────────


def _mcp_engine(tmp_path: Path) -> Any:
    from xpst.config import XPSTConfig

    engine = MagicMock()
    engine._platforms = {"youtube": MagicMock(), "threads": MagicMock()}
    engine.post_manual = AsyncMock()
    engine.post_manual_carousel = AsyncMock()
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    engine.config = config
    return engine


@pytest.mark.asyncio
async def test_mcp_post_dry_run_accepts_per_destination_overrides(tmp_path: Path, monkeypatch: Any) -> None:
    from xpst.mcp.server import _handle_post

    monkeypatch.setenv("XPST_MCP_ALLOW_MUTATIONS", "1")
    engine = _mcp_engine(tmp_path)

    result = await _handle_post(
        engine,
        {
            "video_path": str(tmp_path / "clip.mp4"),
            "caption": "shared caption",
            "platforms": ["youtube", "threads"],
            "overrides": {"threads": {"text": "t" * 501}},
            "dry_run": True,
        },
    )
    payload = json.loads(result.content[0].text)

    assert payload["captions"] == {"youtube": "shared caption", "threads": "t" * 501}
    assert payload["ready"] is False
    assert any("threads" in blocker and "501" in blocker for blocker in payload["blockers"]), payload["blockers"]
    assert payload["network_calls"] is False


@pytest.mark.asyncio
async def test_mcp_post_dry_run_keeps_the_shared_caption_for_destinations_without_one(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from xpst.mcp.server import _handle_post

    monkeypatch.setenv("XPST_MCP_ALLOW_MUTATIONS", "1")
    engine = _mcp_engine(tmp_path)

    result = await _handle_post(
        engine,
        {
            "video_path": str(tmp_path / "clip.mp4"),
            "caption": "shared caption",
            "platforms": ["youtube", "threads"],
            "overrides": {"threads": "threads-only"},
            "dry_run": True,
        },
    )
    payload = json.loads(result.content[0].text)

    assert payload["captions"] == {"youtube": "shared caption", "threads": "threads-only"}
    assert payload["plan"]["platforms"]["threads"]["effective_caption"] == "threads-only"


@pytest.mark.asyncio
async def test_mcp_post_passes_the_overrides_to_the_engine(tmp_path: Path, monkeypatch: Any) -> None:
    from xpst.mcp.server import _handle_post

    monkeypatch.setenv("XPST_MCP_ALLOW_MUTATIONS", "1")
    engine = _mcp_engine(tmp_path)
    from xpst.engine import CrossPostResult

    engine.post_manual.return_value = CrossPostResult(
        video_id="vid-1",
        caption="shared caption",
        results={"x": UploadResult(success=True, post_id="1", post_url="https://example.invalid/x/1", platform="x")},
        captions={"x": "x-only"},
    )

    result = await _handle_post(
        engine,
        {
            "video_path": str(tmp_path / "clip.mp4"),
            "caption": "shared caption",
            "platforms": ["x"],
            "overrides": {"x": "x-only"},
        },
    )
    payload = json.loads(result.content[0].text)

    assert engine.post_manual.await_args.kwargs["per_platform_captions"] == {"x": "x-only"}
    assert payload["captions"] == {"x": "x-only"}


@pytest.mark.asyncio
async def test_mcp_post_reports_a_malformed_overrides_payload(tmp_path: Path, monkeypatch: Any) -> None:
    from xpst.mcp.server import _handle_post

    monkeypatch.setenv("XPST_MCP_ALLOW_MUTATIONS", "1")
    engine = _mcp_engine(tmp_path)

    result = await _handle_post(
        engine,
        {
            "video_path": str(tmp_path / "clip.mp4"),
            "caption": "shared caption",
            "platforms": ["x"],
            "overrides": {"x": ["not a caption"]},
        },
    )
    payload = json.loads(result.content[0].text)

    assert payload["ok"] is False
    assert "Invalid overrides payload" in payload["error"]
    assert engine.post_manual.await_count == 0, "a malformed payload must not post"


@pytest.mark.asyncio
async def test_mcp_preflight_uses_and_reports_the_per_destination_copy(tmp_path: Path) -> None:
    from xpst.mcp.server import _handle_preflight

    config = _preflight_config(tmp_path)
    media = tmp_path / "clip.mp4"
    media.write_bytes(VIDEO_BYTES)

    result = await _handle_preflight(
        config,
        {
            "media_path": str(media),
            "caption": "shared caption",
            "platforms": ["youtube", "threads"],
            "overrides": {"threads": "threads-only"},
        },
    )
    payload = json.loads(result.content[0].text)

    assert payload["captions"] == {"youtube": "shared caption", "threads": "threads-only"}
    assert payload["plan"]["platforms"]["threads"]["effective_caption"] == "threads-only"
    assert payload["network_calls"] is False


def test_mcp_tool_schemas_declare_overrides() -> None:
    from xpst.mcp.server import TOOLS

    for name in ("xpst_post", "xpst_preflight"):
        tool = next(item for item in TOOLS if item.name == name)
        assert "overrides" in tool.inputSchema["properties"], f"{name} does not accept per-destination copy"
