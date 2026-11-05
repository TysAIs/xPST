"""Format envelopes: one registry, explicit YouTube modes, Rumble handoff.

These tests falsify the three defects this work exists to fix:

1. YouTube forced every video into Shorts (``#shorts`` appended, /shorts/ URL)
   with no long-form path at all.
2. X's video ceiling was a bare ``140`` in three unrelated places, so the
   capability document, the media preflight and the upload guard could drift.
3. Rumble was absent from the whole tree.

They are deliberately transport-free: the YouTube client and the Rumble
transport are both seams, so nothing here touches the network or an account.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from xpst.config import XPSTConfig
from xpst.content import (
    VIDEO_ENVELOPES,
    ContentType,
    IntegrationStatus,
    VideoMode,
    capability_document,
    classify_video_mode,
    coerce_video_mode,
    content_profile,
    envelope_violation,
    video_duration_limit,
    video_envelope,
    video_envelope_violation,
)
from xpst.media.specs import PLATFORM_SPECS
from xpst.platforms.rumble import RUMBLE_HANDOFF_CODE, RumbleUploader
from xpst.platforms.youtube import YouTubeUploader

# ── the registry is the only source ────────────────────────────────────────


def test_media_preflight_duration_caps_come_from_the_registry() -> None:
    """The preflight table cannot enforce a different number than capabilities."""
    for platform in ("youtube", "x", "instagram", "tiktok", "threads", "facebook", "rumble"):
        spec = PLATFORM_SPECS[platform]
        assert spec.duration_cap_s == video_duration_limit(platform), platform


def test_x_uploader_manifest_reads_the_registry_number() -> None:
    uploader = YouTubeUploader(XPSTConfig())  # noqa: F841 - keeps the import honest
    from xpst.platforms.x import XUploader

    manifest = XUploader(XPSTConfig()).manifest
    assert manifest.extra["max_video_duration_seconds"] == video_duration_limit("x") == 140


def test_x_config_can_carry_the_real_account_ceiling() -> None:
    """A verified Premium ceiling is used instead of the documented 140 s."""
    from xpst.platforms.x import XUploader

    config = XPSTConfig()
    config.x.max_video_duration_seconds = 600
    uploader = XUploader(config)
    assert uploader.video_duration_cap == 600
    assert uploader.manifest.extra["max_video_duration_seconds"] == 600


def test_unknown_platform_has_no_envelope_and_is_not_refused() -> None:
    assert video_envelope("myspace") is None
    assert video_envelope_violation("myspace", duration_s=10_000) is None


# ── refusals carry the number ──────────────────────────────────────────────


def test_x_refuses_over_envelope_with_the_number() -> None:
    issue = video_envelope_violation("x", duration_s=200)
    assert issue is not None
    assert issue.code == "video_envelope.duration"
    assert "140 s" in issue.message
    assert "200 s" in issue.message
    assert "truncated" in issue.message.lower()


def test_youtube_shorts_ceiling_is_enforced_for_the_shorts_mode() -> None:
    issue = video_envelope_violation("youtube", duration_s=90, mode=VideoMode.SHORTS)
    assert issue is not None
    assert "60 s" in issue.message


def test_youtube_long_form_accepts_a_long_video() -> None:
    assert video_envelope_violation("youtube", duration_s=3600, mode="long") is None


def test_x_has_no_long_form_path_and_says_so() -> None:
    issue = video_envelope_violation("x", duration_s=60, mode="long")
    assert issue is not None
    assert issue.code == "video_envelope.mode"
    assert "no long-form path" in issue.message


def test_rumble_refuses_nothing_it_cannot_verify() -> None:
    envelope = video_envelope("rumble")
    assert envelope is not None
    assert envelope.max_duration_seconds is None
    assert video_envelope_violation("rumble", duration_s=7 * 3600) is None


def test_envelope_override_is_honoured_by_one_shared_check() -> None:
    """The account-specific path speaks the same vocabulary, with its number."""
    from dataclasses import replace

    envelope = replace(VIDEO_ENVELOPES["x"], max_duration_seconds=600)
    assert envelope_violation(envelope, duration_s=500) is None
    issue = envelope_violation(envelope, duration_s=601)
    assert issue is not None
    assert "600 s" in issue.message


def test_classification_thresholds_are_stated_and_ambiguous_is_none() -> None:
    assert classify_video_mode(45, 1080, 1920) is VideoMode.SHORTS
    assert classify_video_mode(45, 1920, 1080) is VideoMode.LONG
    assert classify_video_mode(200, 1080, 1920) is VideoMode.LONG
    assert classify_video_mode(90, 1080, 1920) is None, "90 s portrait must be asked about, not guessed"
    assert classify_video_mode(None, 1080, 1920) is None


def test_unknown_video_mode_is_an_explicit_error() -> None:
    from xpst.content import ContentContractError

    with pytest.raises(ContentContractError):
        coerce_video_mode("medium")


# ── the capability document carries the envelope and integration status ────


def test_capability_document_declares_envelopes_and_integration_status() -> None:
    document = capability_document()
    youtube = document["platforms"]["youtube"]
    assert youtube["video_envelope"]["modes"] == ["shorts", "long"]
    assert youtube["video_envelope"]["shorts_max_duration_seconds"] == 60
    assert youtube["video_envelope"]["max_duration_seconds"] == 43_200
    assert youtube["integration"] == "ready"

    rumble = document["platforms"]["rumble"]
    assert rumble["integration"] == IntegrationStatus.INTEGRATION_UNCERTAIN.value
    assert rumble["video_envelope"]["long_form"] is True
    assert "video" in rumble["implemented"]
    assert "rumble" in document["publish_destinations"]


def test_every_publishing_destination_with_video_declares_an_envelope() -> None:
    for platform, profile in capability_document()["platforms"].items():
        if not profile["publishing"] or ContentType.VIDEO.value not in profile["implemented"]:
            continue
        assert profile["video_envelope"] is not None, platform


# ── YouTube: a real long-form path, and a refusal instead of a fake Short ──

SHORTS_ID = "short123"


class _FakeRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.metadata: dict[str, Any] | None = None

    def next_chunk(self):  # noqa: ANN201 - googleapiclient-shaped
        return None, self.payload


class _FakeVideos:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def insert(self, **kwargs: Any) -> _FakeRequest:
        self.calls.append(kwargs)
        return _FakeRequest({"id": SHORTS_ID})


class _FakeService:
    def __init__(self) -> None:
        self._videos = _FakeVideos()

    def videos(self) -> _FakeVideos:
        return self._videos


@pytest.fixture
def youtube_with_fake_service(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A YouTube uploader whose probe and API client are both stubs."""

    def _make(measured: dict[str, Any]):
        uploader = YouTubeUploader(XPSTConfig())
        fake = _FakeService()
        monkeypatch.setattr("xpst.platforms.youtube.measured_video_format", lambda path: dict(measured))

        async def _service():
            return fake

        monkeypatch.setattr(uploader, "_get_service", _service)
        video = tmp_path / "video.mp4"
        video.write_bytes(b"not really a video")
        return uploader, video, fake

    return _make


@pytest.mark.asyncio
async def test_youtube_shorts_mode_tags_and_returns_the_shorts_url(youtube_with_fake_service) -> None:
    uploader, video, fake = youtube_with_fake_service(
        {"duration_s": 45.0, "width": 1080, "height": 1920, "size_bytes": 1024}
    )
    result = await uploader.upload(video, "A short clip", mode="shorts")

    assert result.success is True
    assert result.post_url == f"https://youtube.com/shorts/{SHORTS_ID}"
    body = fake.videos().calls[0]["body"]
    assert body["snippet"]["title"].endswith("#Shorts")
    assert result.metadata["video_mode"] == "shorts"


@pytest.mark.asyncio
async def test_youtube_long_mode_does_not_tag_shorts_and_uses_the_watch_url(youtube_with_fake_service) -> None:
    uploader, video, fake = youtube_with_fake_service(
        {"duration_s": 1200.0, "width": 1920, "height": 1080, "size_bytes": 50 * 1024 * 1024}
    )
    result = await uploader.upload(video, "A long-form episode", mode="long")

    assert result.success is True
    assert result.post_url == f"https://www.youtube.com/watch?v={SHORTS_ID}"
    body = fake.videos().calls[0]["body"]
    assert "#shorts" not in body["snippet"]["title"].lower()
    assert "Shorts" not in body["snippet"]["tags"]
    assert result.metadata["video_mode"] == "long"
    # Long-form uploads use the large resumable chunk size, not 256 KB.
    from googleapiclient.http import MediaFileUpload

    assert isinstance(fake.videos().calls[0]["media_body"], MediaFileUpload)


@pytest.mark.asyncio
async def test_youtube_refuses_an_over_long_shorts_request(youtube_with_fake_service) -> None:
    uploader, video, fake = youtube_with_fake_service(
        {"duration_s": 90.0, "width": 1080, "height": 1920, "size_bytes": 1024}
    )
    result = await uploader.upload(video, "Ninety seconds", mode="shorts")

    assert result.success is False
    assert "60 s" in (result.error or "")
    assert fake.videos().calls == [], "nothing may be uploaded when the envelope refuses it"


@pytest.mark.asyncio
async def test_youtube_refuses_a_landscape_video_requested_as_a_short(youtube_with_fake_service) -> None:
    uploader, video, fake = youtube_with_fake_service(
        {"duration_s": 30.0, "width": 1920, "height": 1080, "size_bytes": 1024}
    )
    result = await uploader.upload(video, "Landscape short?", mode="shorts")

    assert result.success is False
    assert "YOUTUBE_VIDEO_ENVELOPE" in (result.error or "")
    assert "square or vertical" in (result.error or "")
    assert fake.videos().calls == []


@pytest.mark.asyncio
async def test_youtube_refuses_an_ambiguous_video_instead_of_guessing(youtube_with_fake_service) -> None:
    uploader, video, fake = youtube_with_fake_service(
        {"duration_s": 90.0, "width": 1080, "height": 1920, "size_bytes": 1024}
    )
    result = await uploader.upload(video, "Ninety seconds, portrait")

    assert result.success is False
    assert "YOUTUBE_MODE_AMBIGUOUS" in (result.error or "")
    assert "--shorts" in (result.error or "") and "--long" in (result.error or "")
    assert fake.videos().calls == []


@pytest.mark.asyncio
async def test_youtube_auto_mode_derives_long_form_for_a_landscape_video(youtube_with_fake_service) -> None:
    uploader, video, fake = youtube_with_fake_service(
        {"duration_s": 300.0, "width": 1920, "height": 1080, "size_bytes": 1024}
    )
    result = await uploader.upload(video, "An episode")

    assert result.success is True
    assert result.metadata["video_mode"] == "long"
    assert result.post_url == f"https://www.youtube.com/watch?v={SHORTS_ID}"


# ── Rumble: partner API when available, honest handoff otherwise ───────────


class _FakeTransport:
    def __init__(self, payload: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.payload = payload or {"id": "abc123", "url": "https://rumble.com/vabc123.html"}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def upload(self, video_path: Path, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"video_path": video_path, **kwargs})
        if self.error is not None:
            raise self.error
        return self.payload


@pytest.fixture
def rumble_config(tmp_path: Path) -> XPSTConfig:
    config = XPSTConfig()
    config.rumble.manual_handoff_dir = str(tmp_path / "handoff")
    return config


def _rumble_video(tmp_path: Path) -> Path:
    video = tmp_path / "episode.mp4"
    video.write_bytes(b"rendition bytes")
    return video


@pytest.mark.asyncio
async def test_rumble_api_path_publishes_through_the_transport(rumble_config, tmp_path) -> None:
    rumble_config.rumble.api_key = "partner-key"
    transport = _FakeTransport()
    uploader = RumbleUploader(rumble_config, transport=transport)

    result = await uploader.upload(_rumble_video(tmp_path), "Episode title\nbody copy")

    assert result.success is True
    assert result.post_id == "abc123"
    assert transport.calls[0]["title"] == "Episode title"
    assert transport.calls[0]["api_key"] == "partner-key"
    assert result.metadata["integration"] == "integration_uncertain"


@pytest.mark.asyncio
async def test_rumble_api_failure_is_never_reported_as_success(rumble_config, tmp_path) -> None:
    rumble_config.rumble.api_key = "partner-key"
    uploader = RumbleUploader(rumble_config, transport=_FakeTransport(error=RuntimeError("403 partner only")))

    result = await uploader.upload(_rumble_video(tmp_path), "Episode")

    assert result.success is False
    assert "RUMBLE_UPLOAD_ERROR" in (result.error or "")
    assert "403 partner only" in (result.error or "")


@pytest.mark.asyncio
async def test_rumble_without_a_key_hands_off_and_says_so(rumble_config, tmp_path) -> None:
    uploader = RumbleUploader(rumble_config, transport=_FakeTransport())
    video = _rumble_video(tmp_path)

    result = await uploader.upload(video, "Episode title\nbody")

    assert result.success is False, "a handoff is not a publication"
    assert RUMBLE_HANDOFF_CODE in (result.error or "")
    handoff = result.metadata["manual_handoff"]
    exported = Path(handoff["video"])
    assert exported.exists() and exported.read_bytes() == video.read_bytes()
    assert Path(handoff["caption_file"]).read_text(encoding="utf-8").startswith("# Episode title")
    assert handoff["upload_page_url"].startswith("https://rumble.com/")
    sidecar_json = exported.parent / f"{exported.stem}.handoff.json"
    assert json.loads(sidecar_json.read_text(encoding="utf-8"))["title"] == "Episode title"


def test_rumble_manifest_is_integration_uncertain_and_not_live_verified(rumble_config) -> None:
    manifest = RumbleUploader(rumble_config).manifest
    assert manifest.extra["integration"] == "integration_uncertain"
    assert manifest.extra["verified_live"] is False
    assert manifest.extra["manual_handoff"] is True
    assert manifest.extra["content"] == ("video",)
    # The capability registry says the same thing every surface reads.
    profile = content_profile("rumble")
    assert profile is not None
    assert profile.integration is IntegrationStatus.INTEGRATION_UNCERTAIN


@pytest.mark.asyncio
async def test_rumble_health_reports_the_mode_without_a_network_call(rumble_config) -> None:
    health = await RumbleUploader(rumble_config).check_health()
    assert health.details["mode"] == "manual_handoff"
    assert health.details["integration"] == "integration_uncertain"
