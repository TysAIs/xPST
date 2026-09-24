"""Contract tests for the content_type publish contract (xpst.content).

These tests are the guard rail for the whole modality story:

* the capability matrix (declared vs implemented) must match the **real code** —
  a platform may not declare a content type it cannot publish, and a content
  type the table calls implemented must have a real uploader method behind it;
* validation must refuse an unsupported content type *before* any upload and
  must name the destination in the error;
* the legacy vertical-video flow must behave exactly as it did before the
  contract existed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from xpst.content import (
    CONTENT_TYPES,
    DESTINATION_CONTENT_PROFILES,
    IMPLEMENTATION_METHODS,
    MAX_MEDIA_ITEMS,
    PUBLISH_DESTINATIONS,
    ContentRequest,
    ContentType,
    DestinationContentProfile,
    UnknownContentTypeError,
    UnsupportedContentTypeError,
    _build_support,
    capability_matrix,
    coerce_content_type,
    content_profile,
    content_support_status,
    content_type_from_source,
    infer_content_type,
    media_kind,
    unsupported_content_message,
    validate_content_request,
    validate_destination_content,
)
from xpst.platforms.base import PlatformHealth, PlatformRegistry, PlatformUploader, UploadResult
from xpst.providers import ProviderRole

# ── The capability matrix ───────────────────────────────────────────────────

#: The expected truth, written out independently of the implementation so the
#: test fails loudly if either the table or the belief above drifts.
#: (platform, content_type) -> supported by the engine today?
EXPECTED_MATRIX: dict[tuple[str, str], bool] = {
    ("youtube", "video"): True,
    ("youtube", "image"): False,
    ("youtube", "carousel"): False,
    ("youtube", "text"): False,
    ("youtube", "thread"): False,
    ("x", "video"): True,
    ("x", "image"): True,  # single image post (upload_image: twikit or v1.1 media + v2 tweet)
    ("x", "carousel"): True,  # published as a tweet thread, one media per tweet
    ("x", "text"): True,  # post_text (280 characters, no media)
    ("x", "thread"): False,  # no text-thread sender; multi-media posts are carousel
    ("instagram", "video"): True,
    ("instagram", "image"): True,  # feed photo (upload_image: instagrapi or the Graph image container)
    ("instagram", "carousel"): True,
    ("instagram", "text"): False,
    ("instagram", "thread"): False,
    ("tiktok", "video"): True,
    ("tiktok", "image"): False,
    ("tiktok", "carousel"): False,
    ("tiktok", "text"): False,
    ("tiktok", "thread"): False,
    ("threads", "video"): True,
    ("threads", "image"): False,
    ("threads", "carousel"): False,
    ("threads", "text"): True,  # post_text builds a media_type TEXT container
    ("threads", "thread"): False,
    # Facebook is Page-scoped and, in this wave, declares only feed video: the
    # photo/text publisher methods exist but are not wired to content types yet.
    ("facebook", "video"): True,
    ("facebook", "image"): False,
    ("facebook", "carousel"): False,
    ("facebook", "text"): False,
    ("facebook", "thread"): False,
}


def _config() -> Any:
    from xpst.config import XPSTConfig

    return XPSTConfig()


def _uploader_class(platform: str) -> type[PlatformUploader]:
    PlatformRegistry.auto_discover()
    return type(PlatformRegistry.get(platform, _config()))


@pytest.mark.parametrize(("platform", "content_type", "expected"), [(p, c, ok) for (p, c), ok in EXPECTED_MATRIX.items()])
def test_capability_matrix_matches_the_expected_truth(platform: str, content_type: str, expected: bool) -> None:
    status = content_support_status(platform, coerce_content_type(content_type))
    assert status == ("supported" if expected else "unsupported")


def test_capability_matrix_covers_every_registered_destination() -> None:
    PlatformRegistry.auto_discover()
    registered = set(PlatformRegistry.list_platforms())
    assert registered - set(DESTINATION_CONTENT_PROFILES) == set(), "a registered platform has no content profile"
    assert set(DESTINATION_CONTENT_PROFILES) - registered == set(), "a content profile names an unregistered platform"


def test_every_implemented_content_type_has_a_real_uploader_method() -> None:
    """'Implemented' must mean code exists — checked against the uploader classes.

    Only publishing destinations are checked this way: a messaging adapter
    (Messenger) reuses ``upload()`` to deliver a DM, so method presence alone
    does not mean "can publish"; its text path is checked separately below.
    """
    for platform, profile in DESTINATION_CONTENT_PROFILES.items():
        if not profile.is_publishing:
            continue
        uploader = _uploader_class(platform)
        for content_type in CONTENT_TYPES:
            method_name = IMPLEMENTATION_METHODS[content_type]
            base_method = getattr(PlatformUploader, method_name, None)
            actual = getattr(uploader, method_name, None)
            has_override = actual is not None and actual is not base_method
            assert has_override == profile.supports(content_type), (
                f"{platform}.{method_name} override={has_override} but the table says "
                f"implemented={profile.supports(content_type)} for {content_type.value}"
            )


def test_message_destination_implements_only_its_message_path() -> None:
    messenger = _uploader_class("messenger")
    assert callable(getattr(messenger, "send_text", None)), "messenger's text path is send_text"
    profile = content_profile("messenger")
    assert profile is not None
    assert profile.supports(ContentType.TEXT)
    assert not profile.supports(ContentType.VIDEO)


def test_declared_labels_match_the_provider_manifests() -> None:
    """One source of truth: the table mirrors each manifest's declared content."""
    PlatformRegistry.auto_discover()
    manifests = {manifest.name: manifest for manifest in PlatformRegistry.list_manifests(_config())}
    assert set(manifests) == set(DESTINATION_CONTENT_PROFILES)
    for platform, manifest in manifests.items():
        declared = tuple(manifest.extra.get("content") or ())
        assert declared == DESTINATION_CONTENT_PROFILES[platform].declared_labels, (
            f"{platform} manifest declares {declared} but the content contract says "
            f"{DESTINATION_CONTENT_PROFILES[platform].declared_labels}"
        )


def test_the_shipped_table_has_no_false_declarations() -> None:
    """No destination declares a content type it cannot publish.

    This is the invariant that stops an agent reading a capability list and
    attempting an operation that cannot work; it is asserted over the whole
    table, so re-adding a declaration without its implementation fails here.
    """
    for platform, profile in DESTINATION_CONTENT_PROFILES.items():
        assert profile.declared_but_unimplemented == frozenset(), (
            f"{platform} declares "
            f"{sorted(item.value for item in profile.declared_but_unimplemented)} "
            f"with no implementation"
        )


def test_a_false_declaration_is_refused_and_names_the_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal behavior itself, on a profile that does declare a dead type.

    Kept even though the shipped table has no false declarations: it is what a
    future destination hits the moment it declares more than it can post.
    """
    profile = DestinationContentProfile(
        platform="hypothetical",
        display_name="Hypothetical",
        role=ProviderRole.VIDEO_DESTINATION,
        support=_build_support(("video", "text"), (ContentType.VIDEO,)),
        declared_labels=("video", "text"),
    )
    monkeypatch.setitem(DESTINATION_CONTENT_PROFILES, "hypothetical", profile)

    assert profile.declared_but_unimplemented == frozenset({ContentType.TEXT})
    assert not profile.supports(ContentType.TEXT)
    assert content_support_status("hypothetical", ContentType.TEXT) == "unsupported"
    with pytest.raises(UnsupportedContentTypeError) as excinfo:
        validate_destination_content("hypothetical", ContentType.TEXT)
    message = str(excinfo.value)
    assert "hypothetical" in message, "the refusal must name the destination"
    assert "declared" in message, "a false declaration must be named as such"


def test_every_publish_destination_has_a_media_spec() -> None:
    """A destination that publishes must be describable to the preflight.

    The preflight is what a machine client reads before posting: without a spec
    it answers UNKNOWN_PLATFORM for a destination the engine will actually
    publish to, so the plan and the pipeline disagree. Facebook landed that way
    (#221 added the destination and its uploader, not the spec).
    """
    from xpst.media.specs import PLATFORM_SPECS

    missing = sorted(set(PUBLISH_DESTINATIONS) - set(PLATFORM_SPECS))
    assert missing == [], f"publish destinations with no media spec: {missing}"


def test_messenger_is_a_messaging_destination_not_a_publisher() -> None:
    profile = content_profile("messenger")
    assert profile is not None and not profile.is_publishing
    assert "messenger" not in PUBLISH_DESTINATIONS
    with pytest.raises(UnsupportedContentTypeError) as excinfo:
        validate_destination_content("messenger", ContentType.TEXT)
    assert "messaging" in str(excinfo.value)


def test_unknown_destination_is_unknown_not_supported() -> None:
    assert content_support_status("friendster", ContentType.VIDEO) == "unknown"
    # Unknown destinations are not fabricated into a capability: no exception.
    validate_destination_content("friendster", ContentType.TEXT)


def test_capability_matrix_is_json_serializable() -> None:
    payload = capability_matrix()
    json.dumps(payload)  # must not raise: CLI/MCP/dashboard all serialize this
    assert payload["content_types"] == [item.value for item in CONTENT_TYPES]
    # No destination declares a content type it cannot publish — the false
    # declarations (threads/text, instagram/image, x/thread) are withdrawn.
    assert payload["platforms"]["threads"]["declared_but_unimplemented"] == []
    assert all(
        profile["declared_but_unimplemented"] == [] for profile in payload["platforms"].values()
    )


# ── Vocabulary ──────────────────────────────────────────────────────────────


def test_vocabulary_is_stable() -> None:
    assert [item.value for item in CONTENT_TYPES] == ["video", "image", "carousel", "text", "thread"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("VIDEO", "video"), (" reels ", "video"), ("Photo", "image"), ("album", "carousel"), ("tweet", "text")],
)
def test_alias_spellings_resolve_to_the_canonical_value(value: str, expected: str) -> None:
    assert coerce_content_type(value).value == expected


def test_unknown_content_type_is_an_explicit_error() -> None:
    with pytest.raises(UnknownContentTypeError):
        coerce_content_type("hologram")


@pytest.mark.parametrize(
    ("media", "expected"),
    [
        ([], "text"),
        (["a.mp4"], "video"),
        (["a.MP4"], "video"),
        (["a.jpg"], "image"),
        (["a.webp"], "image"),
        (["a.mp4", "b.jpg"], "carousel"),
        (["a.weird"], "video"),  # pre-contract behaviour: one non-image file is a video
    ],
)
def test_infer_content_type_preserves_pre_contract_behaviour(media: list[str], expected: str) -> None:
    assert infer_content_type(media).value == expected


def test_media_kind_classifies_by_extension() -> None:
    assert media_kind("clip.mp4") == "video"
    assert media_kind(Path("photo.png")) == "image"
    assert media_kind("notes.txt") == "unknown"


def test_source_content_type_bridge() -> None:
    assert content_type_from_source("carousel_mixed") is ContentType.CAROUSEL
    assert content_type_from_source(ContentType.IMAGE) is ContentType.IMAGE
    with pytest.raises(UnknownContentTypeError):
        content_type_from_source("hologram")


def test_extension_sets_have_one_source() -> None:
    from xpst.content import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
    from xpst.sources import local

    assert local.VIDEO_EXTENSIONS is VIDEO_EXTENSIONS
    assert local.IMAGE_EXTENSIONS is IMAGE_EXTENSIONS


# ── The typed request ───────────────────────────────────────────────────────


def test_legacy_payload_shape_is_unchanged() -> None:
    request = ContentRequest.from_payload(
        {"media_paths": ["/tmp/a.mp4"], "caption": "hello", "platforms": ["YouTube", "youtube", " X "]}
    )
    assert request.media_paths == ["/tmp/a.mp4"]
    assert request.caption == "hello"
    assert request.platforms == ("youtube", "x")
    assert request.effective_content_type is ContentType.VIDEO
    assert not request.is_explicit_content_type


def test_typed_payload_shape_is_accepted() -> None:
    request = ContentRequest.from_payload(
        {
            "content_type": "carousel",
            "media": ["/tmp/a.jpg", "/tmp/b.jpg"],
            "text": "two photos",
            "platforms": ["instagram"],
            "overrides": {"instagram": {"text": "second caption"}},
        }
    )
    assert request.effective_content_type is ContentType.CAROUSEL
    assert request.text_for("instagram") == "second caption"
    assert request.text_for("x") == "two photos"
    assert request.media_paths == ["/tmp/a.jpg", "/tmp/b.jpg"]


def test_unknown_content_type_in_a_payload_is_a_blocker_not_an_exception() -> None:
    request = ContentRequest.from_payload({"content_type": "hologram", "text": "hi", "platforms": ["x"]})
    issues = validate_content_request(request)
    assert [issue.code for issue in issues] == ["content_type.unknown"]
    assert "hologram" in issues[0].message


def test_media_list_is_capped() -> None:
    request = ContentRequest.from_payload({"media": [f"/tmp/{i}.jpg" for i in range(MAX_MEDIA_ITEMS + 5)]})
    assert len(request.media) == MAX_MEDIA_ITEMS


def test_request_serializes_with_stable_keys() -> None:
    request = ContentRequest.from_payload({"media_paths": ["/tmp/a.mp4"], "caption": "hi", "platforms": ["youtube"]})
    payload = request.to_dict()
    assert payload["content_type"] is None
    assert payload["effective_content_type"] == "video"
    assert payload["media"] == payload["media_paths"] == ["/tmp/a.mp4"]
    assert payload["text"] == payload["caption"] == "hi"
    json.dumps(payload)


# ── Validation ──────────────────────────────────────────────────────────────


def test_validation_is_silent_for_the_legacy_video_flow() -> None:
    request = ContentRequest.from_payload(
        {"media_paths": ["/tmp/a.mp4"], "caption": "hi", "platforms": ["youtube", "instagram", "x"]}
    )
    assert validate_content_request(request) == ()


def test_text_to_a_destination_without_a_text_path_names_the_destination() -> None:
    issues = validate_content_request(
        ContentRequest.from_payload({"content_type": "text", "text": "hello", "platforms": ["youtube"]})
    )
    assert [issue.code for issue in issues] == ["content_type.unsupported"]
    assert issues[0].severity == "error"
    assert issues[0].platform == "youtube"
    assert "youtube" in issues[0].message


def test_text_without_text_is_blocked() -> None:
    issues = validate_content_request(
        ContentRequest.from_payload({"content_type": "text", "text": "   ", "platforms": ["x"]})
    )
    assert "content_type.text_required" in [issue.code for issue in issues]


def test_text_with_media_is_blocked() -> None:
    issues = validate_content_request(ContentRequest(content_type=ContentType.TEXT, media=("a.mp4",), text="hi"))
    assert "content_type.media_not_allowed" in [issue.code for issue in issues]


def test_video_requires_exactly_one_file() -> None:
    issues = validate_content_request(
        ContentRequest(content_type=ContentType.VIDEO, media=("a.mp4", "b.mp4"), text="hi", platforms=("youtube",))
    )
    assert [issue.code for issue in issues] == ["content_type.media_count"]


def test_carousel_requires_two_to_ten_items() -> None:
    one = validate_content_request(ContentRequest(content_type=ContentType.CAROUSEL, media=("a.jpg",), platforms=("instagram",)))
    assert [issue.code for issue in one] == ["content_type.media_count"]
    eleven = validate_content_request(
        ContentRequest(content_type=ContentType.CAROUSEL, media=tuple(f"{i}.jpg" for i in range(11)), platforms=("instagram",))
    )
    assert [issue.code for issue in eleven] == ["content_type.media_count"]
    ok = validate_content_request(
        ContentRequest(content_type=ContentType.CAROUSEL, media=("a.jpg", "b.jpg"), platforms=("instagram",))
    )
    assert ok == ()


def test_image_rejects_a_video_file() -> None:
    issues = validate_content_request(
        ContentRequest(content_type=ContentType.IMAGE, media=("clip.mp4",), platforms=("instagram",))
    )
    assert "content_type.media_kind" in [issue.code for issue in issues]


def test_override_for_a_destination_that_was_not_requested_is_blocked() -> None:
    request = ContentRequest.from_payload(
        {
            "media_paths": ["a.mp4"],
            "platforms": ["youtube"],
            "overrides": {"instagram": {"text": "nope"}},
        }
    )
    issues = [issue for issue in validate_content_request(request) if issue.code == "content_type.override_destination"]
    assert len(issues) == 1
    assert issues[0].platform == "instagram"
    assert "instagram" in issues[0].message


def test_override_content_type_is_validated_for_that_destination() -> None:
    request = ContentRequest.from_payload(
        {
            "media_paths": ["a.mp4"],
            "platforms": ["youtube", "x"],
            # x/thread is still unimplemented (x/text is implemented now — see
            # tests/test_text_posts.py), so the override is refused for x only.
            "overrides": {"x": {"content_type": "thread"}},
        }
    )
    issues = [issue for issue in validate_content_request(request) if issue.code == "content_type.unsupported"]
    assert [issue.platform for issue in issues] == ["x"], "only the overriding destination is refused"


def test_undeclared_destination_warns_instead_of_blocking() -> None:
    issues = validate_content_request(
        ContentRequest(content_type=ContentType.TEXT, text="hi", platforms=("friendster",))
    )
    assert [issue.code for issue in issues] == ["content_type.unknown_destination"]
    assert issues[0].severity == "warning"


def test_empty_target_list_is_left_to_the_preflight_service() -> None:
    assert validate_content_request(ContentRequest(content_type=ContentType.TEXT, text="hi")) == ()


def test_unsupported_message_names_the_destination_and_the_supported_types() -> None:
    message = unsupported_content_message("youtube", ContentType.TEXT, supported=[ContentType.VIDEO])
    assert "youtube" in message and "video" in message


# ── Engine: validated before any upload ─────────────────────────────────────

VIDEO_BYTES = b"\x00" * 4096

#: A JPEG whose header declares 1080x1080 — the real dimensions the preflight
#: reads (no ffmpeg, no photo of anybody).
_JPEG_HEADER_BYTES = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    + b"\xff\xc0\x00\x11\x08"
    + (1080).to_bytes(2, "big")
    + (1080).to_bytes(2, "big")
    + bytes([3, 1, 0x11, 0, 2, 0x11, 1, 3, 0x11, 1])
    + b"\xff\xd9"
)


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


def _recording_uploader(platform: str, *, success: bool = True) -> MagicMock:
    uploader = MagicMock(spec=PlatformUploader)
    uploader.platform_name = platform
    uploader.upload = AsyncMock(
        return_value=UploadResult(
            success=success,
            post_id="post1" if success else None,
            post_url="https://example.invalid/post1" if success else None,
            error=None if success else "upload failed",
            platform=platform,
        )
    )
    uploader.upload_carousel = AsyncMock(
        return_value=UploadResult(success=success, post_id="c1", post_url="https://example.invalid/c1", platform=platform)
    )
    # A double for a destination with no text path: the base class's default
    # behaviour, spelled out so a text request still gets a destination-named
    # refusal instead of a generic adapter error.
    uploader.post_text = AsyncMock(
        return_value=UploadResult(
            success=False,
            error=f"{platform.upper()}_TEXT_UNSUPPORTED: {platform} has no text-post path in xPST.",
            platform=platform,
            retryable=False,
        )
    )
    uploader.check_health = AsyncMock(return_value=PlatformHealth(platform=platform, authenticated=True, session_valid=True))
    uploader.delete = MagicMock(return_value=True)
    return uploader


@pytest.mark.asyncio
async def test_engine_refuses_an_unsupported_content_type_without_uploading(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    uploader = _recording_uploader("youtube")
    engine._platforms["youtube"] = uploader

    result = await engine.post_request(ContentRequest(content_type=ContentType.TEXT, text="hello", platforms=("youtube",)))

    assert uploader.upload.await_count == 0, "an unsupported content type must not touch an uploader"
    assert uploader.upload_carousel.await_count == 0
    assert result.all_success is False
    row = result.results["youtube"]
    assert row.success is False
    assert "youtube" in (row.error or "")
    assert row.metadata.get("blocked") is True


@pytest.mark.asyncio
async def test_engine_reports_every_requested_destination_when_refusing(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    engine._platforms["youtube"] = _recording_uploader("youtube")
    engine._platforms["x"] = _recording_uploader("x")

    result = await engine.post_request(ContentRequest(content_type=ContentType.TEXT, text="hello", platforms=("youtube", "x")))

    assert set(result.results) == {"youtube", "x"}
    assert all(row.success is False for row in result.results.values())


@pytest.mark.asyncio
async def test_engine_requires_a_destination(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    result = await engine.post_request(ContentRequest(content_type=ContentType.TEXT, text="hello"))
    assert result.all_success is False
    assert result.results == {}


@pytest.mark.asyncio
async def test_engine_dispatches_video_through_post_manual(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    uploader = _recording_uploader("youtube")
    engine._platforms["youtube"] = uploader
    video = tmp_path / "clip.mp4"
    video.write_bytes(VIDEO_BYTES)

    from unittest.mock import patch

    with patch.object(engine, "post_manual", new_callable=AsyncMock) as manual:
        manual.return_value = "sentinel"
        result = await engine.post_request(
            ContentRequest(content_type=ContentType.VIDEO, media=(str(video),), text="hi", platforms=("youtube",))
        )

    assert result == "sentinel"
    manual.assert_awaited_once()
    assert manual.await_args.args[0] == video


@pytest.mark.asyncio
async def test_engine_dispatches_carousel_through_post_manual_carousel(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    from unittest.mock import patch

    with patch.object(engine, "post_manual_carousel", new_callable=AsyncMock) as carousel:
        carousel.return_value = "sentinel"
        result = await engine.post_request(
            ContentRequest(content_type=ContentType.CAROUSEL, media=("a.jpg", "b.jpg"), platforms=("instagram",))
        )

    assert result == "sentinel"
    carousel.assert_awaited_once()


@pytest.mark.asyncio
async def test_engine_reports_undeclared_destination_content_types_explicitly(tmp_path: Path) -> None:
    """A plugin destination passes validation but has no publishing path."""
    engine = _make_engine(tmp_path)
    engine._platforms["friendster"] = _recording_uploader("friendster")

    result = await engine.post_request(ContentRequest(content_type=ContentType.TEXT, text="hi", platforms=("friendster",)))

    assert result.all_success is False
    assert engine._platforms["friendster"].upload.await_count == 0
    assert "friendster" in (result.results["friendster"].error or "")


# ── PostService: the HTTP/API path ──────────────────────────────────────────


def _post_config_dir(tmp_path: Path) -> str:
    media = tmp_path / "media"
    media.mkdir()
    (media / "clip.mp4").write_bytes(VIDEO_BYTES)
    (media / "clip2.mp4").write_bytes(VIDEO_BYTES)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    token = tmp_path / "youtube-token.json"
    token.write_text("{}", encoding="utf-8")
    config = {
        "version": 4,
        "accounts": {
            "local": {"path": str(media)},
            "youtube": {"enabled": True, "token_file": str(token)},
            "instagram": {
                "enabled": True,
                "auth_mode": "graph_api",
                "graph_access_token": "fixture-token",
                "graph_ig_user_id": "1",
            },
        },
        "video": {"download_dir": str(tmp_path / "downloads")},
        "monitoring": {},
    }
    (cfg_dir / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return str(cfg_dir)


def _post_service(tmp_path: Path, engine: Any = None) -> Any:
    from xpst.config import XPSTConfig
    from xpst.services.post_service import PostService

    config_dir = _post_config_dir(tmp_path)
    # Load the config the way every surface does (config.yaml inside config_dir),
    # so the temp token file and enabled destinations are what preflight sees.
    config = XPSTConfig.load(str(Path(config_dir) / "config.yaml"))
    return PostService(config, config_dir, engine_factory=lambda _cfg: engine)


def test_service_blocks_text_to_a_video_only_destination(tmp_path: Path) -> None:
    from xpst.services.post_service import PostRequest

    service = _post_service(tmp_path)
    verdict = service.preflight(PostRequest.from_payload({"content_type": "text", "text": "hi", "platforms": ["youtube"]}))
    assert verdict["ready"] is False
    assert any("youtube" in blocker for blocker in verdict["blockers"])
    assert verdict["content_type"] == "text"
    assert [issue["code"] for issue in verdict["content_issues"]] == ["content_type.unsupported"]


def test_service_keeps_the_legacy_video_flow_ready(tmp_path: Path) -> None:
    from xpst.services.post_service import PostRequest

    service = _post_service(tmp_path)
    media = str(tmp_path / "media" / "clip.mp4")
    verdict = service.preflight(PostRequest.from_payload({"media_paths": [media], "caption": "hi", "platforms": ["youtube"]}))
    assert verdict["ready"] is True, verdict["blockers"]
    assert verdict["content_issues"] == []


def test_service_refuses_an_image_for_a_video_only_destination_without_calling_the_engine(
    tmp_path: Path,
) -> None:
    """A destination that cannot publish images is refused, engine untouched.

    Before this card the same request was refused because *no* destination had an
    image path; the refusal must now be about the destination, by name.
    """
    from xpst.services.post_service import PostRequest

    engine = MagicMock()
    engine.post_manual = AsyncMock()
    engine.post_manual_carousel = AsyncMock()
    engine.post_manual_image = AsyncMock()
    service = _post_service(tmp_path, engine)
    photo = tmp_path / "media" / "photo.jpg"
    photo.write_bytes(_JPEG_HEADER_BYTES)

    envelope = service.execute(
        PostRequest.from_payload(
            {"content_type": "image", "media": [str(photo)], "caption": "hi", "platforms": ["youtube"]}
        )
    )

    assert envelope["ok"] is False
    assert envelope["content_type"] == "image"
    assert engine.post_manual.await_count == 0
    assert engine.post_manual_carousel.await_count == 0
    assert engine.post_manual_image.await_count == 0
    assert any("youtube" in blocker for blocker in envelope["blockers"]), envelope["blockers"]


def test_service_reports_the_content_type_in_the_envelope(tmp_path: Path) -> None:
    from xpst.services.post_service import PostRequest

    engine = MagicMock()
    engine.post_manual = AsyncMock(
        return_value=MagicMock(results={"youtube": UploadResult(success=True, post_id="1", post_url="https://e.invalid/1")}, video_id="v1", caption="hi")
    )
    service = _post_service(tmp_path, engine)

    media = str(tmp_path / "media" / "clip.mp4")
    envelope = service.execute(PostRequest.from_payload({"media_paths": [media], "caption": "hi", "platforms": ["youtube"]}))

    assert envelope["content_type"] == "video"
    assert envelope["ok"] is True
    engine.post_manual.assert_awaited_once()


def test_service_sends_a_two_file_request_to_the_carousel_path(tmp_path: Path) -> None:
    from xpst.services.post_service import PostRequest

    engine = MagicMock()
    engine.post_manual = AsyncMock()
    engine.post_manual_carousel = AsyncMock(
        return_value=MagicMock(results={"instagram": UploadResult(success=True, post_id="1", post_url="https://e.invalid/1")}, video_id="c1", caption="hi")
    )
    service = _post_service(tmp_path, engine)

    media_dir = tmp_path / "media"
    envelope = service.execute(
        PostRequest.from_payload(
            {
                "media_paths": [str(media_dir / "clip.mp4"), str(media_dir / "clip2.mp4")],
                "caption": "hi",
                "platforms": ["instagram"],
            }
        )
    )

    assert envelope["content_type"] == "carousel"
    engine.post_manual_carousel.assert_awaited_once()
    engine.post_manual.assert_not_awaited()
