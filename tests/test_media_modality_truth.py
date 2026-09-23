"""The image trap: an offered file must never be one the preflight hard-rejects.

Two contracts are pinned here.

1. ``verify_media`` is modality-aware. An image at a destination that publishes
   images is judged by image rules (container, dimensions, aspect, size); an
   image at a destination that does not is refused with exactly ONE error that
   names the destination. Before this, a JPEG tripped the video container rule
   *and* the "real video stream present" rule and every surface showed the raw
   spec comparison ``".jpg vs accepted .mp4, .mov"``.
2. ``/api/media`` offers only what a destination can actually publish, and moves
   everything else into ``skipped`` with the reason — the compose screen can no
   longer list a file the post path refuses.

The destination capability that both contracts read is
``PlatformSpec.modalities`` (``xpst.media.specs``). Instagram and X declare the
image modality because they now have a real ``upload_image`` path (see
``tests/test_image_posts.py`` for the per-destination contract); YouTube,
TikTok and Threads are video-only and must stay that way until they do. Adding a
capability here without the adapter is exactly the defect this file guards, so
these tests change only in the same PR as the adapter that earns it.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst.config import XPSTConfig
from xpst.content import (
    ALL_MEDIA_EXTENSIONS,
    IMAGE_EXTENSIONS,
    MEDIA_KIND_IMAGE,
    MEDIA_KIND_VIDEO,
    VIDEO_EXTENSIONS,
    media_kind,
)
from xpst.dashboard.api import create_api_router
from xpst.media.modality import (
    MODALITIES,
    MODALITY_IMAGE,
    MODALITY_VIDEO,
    detect_modality,
)
from xpst.media.specs import (
    MODALITY_CHECK,
    PLATFORM_SPECS,
    PlatformSpec,
    destination_display_name,
    destinations_for_modality,
    modality_unsupported_message,
    verify_media,
)
from xpst.services.post_preflight import PostPlanRequest, PostPreflightService

if TYPE_CHECKING:
    from pathlib import Path

# A real 2x2 JPEG. ffprobe reads its SOF0 markers, so the accepted-image path
# (dimensions) is exercised on a real file without needing ffmpeg to author one.
_JPEG_2X2 = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAACAAIBAREA/8QAHwAAAQUBAQEB"
    "AQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1Fh"
    "ByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZ"
    "WmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG"
    "x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/APn+v//Z"
)


def _write_image(path: Path) -> Path:
    path.write_bytes(_JPEG_2X2)
    return path


def _jpeg_bytes(width: int, height: int) -> bytes:
    """A JPEG header declaring ``width`` x ``height`` (no ffmpeg needed).

    The dimensions come from the SOF0 segment, which is what the preflight reads
    from the image header — so a wrong-aspect fixture is a real file, not a mock.
    """
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = (
        b"\xff\xc0"
        + (17).to_bytes(2, "big")
        + bytes([8])
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + bytes([3, 1, 0x11, 0, 2, 0x11, 1, 3, 0x11, 1])
    )
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def _open_app(tmp_path: Path) -> TestClient:
    """Bare router app (the media listing takes no auth of its own)."""
    app = FastAPI()
    app.include_router(create_api_router(str(tmp_path / "cfg")))
    return TestClient(app)


def _image_spec(**overrides: Any) -> PlatformSpec:
    """A destination that really does publish images (used to test the accept path)."""
    base = {
        "display_name": "Photo Place",
        "containers": (".mp4",),
        "video_codec": "h264",
        "pix_fmt": "yuv420p",
        "long_edge": 1920,
        "fps_cap": 60,
        "audio_codec": "aac",
        "audio_rate": 44100,
        "lufs": -14.0,
        "max_video_bitrate_bps": 10_000_000,
        "file_size_cap_mb": 8,
        "duration_cap_s": None,
        "modalities": (MODALITY_VIDEO, MODALITY_IMAGE),
        "image_containers": (".jpg", ".png"),
    }
    base.update(overrides)
    return PlatformSpec(**base)


def _install_spec(monkeypatch: pytest.MonkeyPatch, name: str, spec: PlatformSpec) -> None:
    monkeypatch.setitem(PLATFORM_SPECS, name, spec)


# ---------------------------------------------------------------------------
# (a) an image container is accepted or rejected by destination capability
# ---------------------------------------------------------------------------


class TestImageContainerVersusDestinationCapability:
    #: Destinations that really publish single images today. Written out
    #: independently of the table so a silent capability change fails here.
    IMAGE_DESTINATIONS: frozenset[str] = frozenset({"instagram", "x"})

    def test_image_capability_is_declared_only_where_the_code_can_deliver(self) -> None:
        """The capability table must stay honest, in both directions."""
        declared = {name for name, spec in PLATFORM_SPECS.items() if spec.supports(MODALITY_IMAGE)}
        assert declared == self.IMAGE_DESTINATIONS, (
            "image capability changed — add/remove the adapter's upload_image in the same PR"
        )
        for name, spec in PLATFORM_SPECS.items():
            assert spec.supports(MODALITY_VIDEO), f"{name} must publish video"
            if name not in self.IMAGE_DESTINATIONS:
                assert not spec.supports(MODALITY_IMAGE)
                assert spec.image_containers == ()
        assert set(destinations_for_modality(MODALITY_IMAGE)) == self.IMAGE_DESTINATIONS
        assert set(destinations_for_modality(MODALITY_VIDEO)) == set(PLATFORM_SPECS)

    def test_declared_image_capability_requires_image_containers(self) -> None:
        """A capability with no accepted containers could never be satisfied."""
        for name, spec in PLATFORM_SPECS.items():
            assert set(spec.modalities) <= set(MODALITIES), f"{name} declares an unknown modality"
            assert bool(spec.image_containers) == spec.supports(MODALITY_IMAGE), (
                f"{name}: image_containers and the declared image modality must agree"
            )

    @pytest.mark.parametrize("platform", ["youtube", "tiktok", "threads"])
    def test_image_is_rejected_by_a_video_only_destination(self, tmp_path: Path, platform: str) -> None:
        photo = _write_image(tmp_path / "photo.jpg")

        report = verify_media(photo, platform, check_loudness=False)

        assert not report.ok
        assert [c.name for c in report.errors] == [MODALITY_CHECK], (
            "an image must produce exactly one error, not a pile of video rules"
        )
        detail = report.errors[0].detail
        assert destination_display_name(platform) in detail
        assert "image" in detail
        assert "vs accepted" not in detail, "no raw spec comparison in a user-facing message"

    @pytest.mark.parametrize("platform", ["youtube", "tiktok", "threads"])
    def test_image_produces_one_named_preflight_blocker(self, tmp_path: Path, platform: str) -> None:
        """Criterion 3: one clear preflight message naming the destination."""
        photo = _write_image(tmp_path / "photo.jpg")

        plan = (
            PostPreflightService(XPSTConfig())
            .plan(
                PostPlanRequest(
                    media_paths=[photo],
                    target_platforms=[platform],
                    include_readiness=False,
                    include_transform=False,
                    check_loudness=False,
                )
            )
            .to_dict()["platforms"][platform]
        )

        media_blockers = [
            blocker
            for blocker in plan["hard_blockers"]
            if blocker["media_path"] is not None or blocker["code"].startswith("MEDIA_")
        ]
        assert [blocker["code"] for blocker in media_blockers] == ["MEDIA_MODALITY_UNSUPPORTED"]
        message = media_blockers[0]["message"]
        assert destination_display_name(platform) in message
        assert "jpg" in message
        assert plan["media"][0]["transform"] is None, "no transform plan for a refused file"

    @pytest.mark.parametrize("platform", ["instagram", "x"])
    def test_an_image_is_accepted_by_a_destination_that_publishes_images(
        self, tmp_path: Path, platform: str
    ) -> None:
        """The flip side: the two destinations with a real image path take it."""
        photo = _write_image(tmp_path / "photo.jpg")

        report = verify_media(photo, platform, check_loudness=False)

        assert report.ok, [c.detail for c in report.errors]
        assert MODALITY_CHECK not in {c.name for c in report.checks}
        names = {c.name for c in report.checks}
        assert "container" in names
        assert "dimensions" in names
        assert "video_stream" not in names
        assert "video_codec" not in names
        assert "pix_fmt" not in names
        assert "loudness" not in names
        assert "faststart" not in names

    def test_accepted_image_container_still_checked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_spec(monkeypatch, "photo_place", _image_spec(image_containers=(".png",)))
        photo = _write_image(tmp_path / "photo.jpg")

        report = verify_media(photo, "photo_place", check_loudness=False)

        assert [c.name for c in report.errors] == ["container"]
        assert ".png" in report.errors[0].detail
        assert MODALITY_CHECK not in {c.name for c in report.checks}, "a container mismatch is not a capability refusal"

    def test_oversized_image_is_rejected_by_size(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_spec(monkeypatch, "photo_place", _image_spec(file_size_cap_mb=0))
        photo = _write_image(tmp_path / "photo.jpg")

        report = verify_media(photo, "photo_place", check_loudness=False)

        assert [c.name for c in report.errors] == ["file_size"]

    def test_video_behaviour_is_unchanged(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The modality split must not weaken the video checks."""
        _install_spec(monkeypatch, "photo_place", _image_spec(image_containers=(".jpg",)))
        clip = tmp_path / "clip.mkv"
        clip.write_bytes(b"\x00" * 32)

        report = verify_media(clip, "photo_place", check_loudness=False)

        assert [c.name for c in report.errors] == ["container"], (
            "a video container mismatch still fails the video container rule"
        )
        assert ".mp4" in report.errors[0].detail

    def test_unknown_extension_keeps_the_video_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unrecognised file is not silently blessed as an image."""
        _install_spec(monkeypatch, "photo_place", _image_spec(image_containers=(".jpg",)))
        other = tmp_path / "audio.flac"
        other.write_bytes(b"\x00" * 32)

        report = verify_media(other, "photo_place", check_loudness=False)

        names = {c.name for c in report.checks}
        assert "container" in {c.name for c in report.errors}
        assert MODALITY_CHECK not in names, "an unknown extension must not be reported as a capability refusal"

    def test_explicit_modality_overrides_the_extension(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A caller that knows the modality can override the extension."""
        _install_spec(monkeypatch, "photo_place", _image_spec())
        photo = _write_image(tmp_path / "photo.jpg")

        as_image = verify_media(photo, "photo_place", modality=MODALITY_IMAGE, check_loudness=False)
        as_video = verify_media(photo, "photo_place", modality=MODALITY_VIDEO, check_loudness=False)
        refused = verify_media(photo, "youtube", modality=MODALITY_IMAGE, check_loudness=False)

        assert as_image.ok
        assert MODALITY_CHECK not in {c.name for c in as_video.checks}
        assert "container" in {c.name for c in as_video.errors}
        assert [c.name for c in refused.errors] == [MODALITY_CHECK]

    def test_message_says_who_can_take_the_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_spec(monkeypatch, "photo_place", _image_spec())
        photo = _write_image(tmp_path / "photo.jpg")

        report = verify_media(photo, "youtube", check_loudness=False)

        detail = report.errors[0].detail
        assert "Photo Place" in detail
        assert detail.startswith("YouTube cannot accept an image file (.jpg).")

    def test_global_message_names_no_destination_and_gives_advice(self) -> None:
        """With no destination named, the sentence must never contradict itself.

        A publishable modality names who can take it; a modality nobody can take
        (monkeypatched below) is the only case that gets the "No destination can
        accept" head.
        """
        assert modality_unsupported_message(MODALITY_VIDEO, suffix=".mp4") == (
            "A video file (.mp4) can be posted to YouTube, TikTok, Instagram Reels, X (Twitter),"
            " Threads, Facebook Page. Choose a different file."
        )
        assert modality_unsupported_message(MODALITY_IMAGE, suffix=".png") == (
            "An image file (.png) can be posted to Instagram Reels, X (Twitter)."
            " Choose an image format your destinations accept, or a video file."
        )

    def test_global_message_still_says_nobody_can_when_nobody_can(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The no-image-path wording survives for a modality no destination takes."""
        from dataclasses import replace

        for name, spec in PLATFORM_SPECS.items():
            monkeypatch.setitem(
                PLATFORM_SPECS,
                name,
                replace(spec, modalities=(MODALITY_VIDEO,), image_containers=()),
            )

        assert modality_unsupported_message(MODALITY_IMAGE, suffix=".png") == (
            "No destination can accept an image file (.png). "
            "xPST has no image publish path yet. "
            "Choose an image format your destinations accept, or a video file."
        )

    @pytest.mark.parametrize(
        ("platform", "width", "height", "expected_code"),
        [
            ("instagram", 3000, 1000, "MEDIA_SPEC_ASPECT_RATIO"),  # 3:1, Instagram stops at 1.91:1
            ("x", 400, 4000, "MEDIA_SPEC_ASPECT_RATIO"),  # 1:10, X stops at 1:3
        ],
    )
    def test_a_wrong_aspect_image_is_blocked_in_preflight(
        self, tmp_path: Path, platform: str, width: int, height: int, expected_code: str
    ) -> None:
        """A destination's own limits are enforced before any upload, by name."""
        photo = tmp_path / "wide.jpg"
        photo.write_bytes(_jpeg_bytes(width, height))

        plan = (
            PostPreflightService(XPSTConfig())
            .plan(
                PostPlanRequest(
                    media_paths=[photo],
                    target_platforms=[platform],
                    include_readiness=False,
                    include_transform=False,
                    check_loudness=False,
                )
            )
            .to_dict()["platforms"][platform]
        )

        codes = [blocker["code"] for blocker in plan["hard_blockers"]]
        assert expected_code in codes, codes
        message = next(b["message"] for b in plan["hard_blockers"] if b["code"] == expected_code)
        assert destination_display_name(platform) in message
        assert f"{width}x{height}" in message


# ---------------------------------------------------------------------------
# The modality vocabulary IS the content-contract vocabulary (one source of truth)
# ---------------------------------------------------------------------------


class TestModalityVocabularyIsTheContentContract:
    """``xpst.content`` (PR #203) owns the media vocabulary.

    ``xpst.media.modality`` is a compatibility shim over it. These tests pin
    the identity so the two can never quietly drift apart again: an extension
    set that disagrees between the offer surface and the publish contract is
    exactly the bug class this card exists to kill.
    """

    def test_extension_sets_are_the_content_contract_objects(self) -> None:
        from xpst.media import modality as shim

        assert shim.VIDEO_EXTENSIONS is VIDEO_EXTENSIONS
        assert shim.IMAGE_EXTENSIONS is IMAGE_EXTENSIONS
        assert shim.ALL_MEDIA_EXTENSIONS is ALL_MEDIA_EXTENSIONS

    def test_modality_labels_match_content_media_kinds(self) -> None:
        assert MODALITY_VIDEO == MEDIA_KIND_VIDEO
        assert MODALITY_IMAGE == MEDIA_KIND_IMAGE

    def test_detect_modality_agrees_with_media_kind(self) -> None:
        for probe in ("clip.mp4", "MOVIE.MOV", "photo.jpeg", "icon.WEBP", "notes.txt", ".flac", ""):
            kind = media_kind(probe)
            detected = detect_modality(probe)
            if kind == "unknown":
                assert detected is None, f"{probe!r}: unknown to the contract but classified by the shim"
            else:
                assert detected == kind, f"{probe!r}: shim says {detected!r}, contract says {kind!r}"

    def test_media_kind_classifies_by_extension_only(self) -> None:
        assert media_kind("clip.mp4") == MEDIA_KIND_VIDEO
        assert media_kind("photo.jpeg") == MEDIA_KIND_IMAGE
        assert media_kind("notes.txt") == "unknown"


# ---------------------------------------------------------------------------
# (b) /api/media may not offer anything the preflight would hard-reject
# ---------------------------------------------------------------------------


def _folder_with_video_and_image(tmp_path: Path) -> Path:
    folder = tmp_path / "media"
    folder.mkdir()
    (folder / "clip.mp4").write_bytes(b"\x00" * 4096)
    _write_image(folder / "photo.jpg")
    (folder / "notes.txt").write_text("not media", encoding="utf-8")
    return folder


def _hard_blocker_codes(path: str, platform: str) -> list[str]:
    plan = (
        PostPreflightService(XPSTConfig())
        .plan(
            PostPlanRequest(
                media_paths=[path],
                target_platforms=[platform],
                include_readiness=False,
                include_transform=False,
                check_loudness=False,
            )
        )
        .to_dict()
    )
    return [blocker["code"] for blocker in plan["platforms"][platform]["hard_blockers"]]


def test_api_media_offers_only_files_the_preflight_would_accept(tmp_path: Path) -> None:
    folder = _folder_with_video_and_image(tmp_path)

    with _open_app(tmp_path) as client:
        data = client.get("/api/media", params={"folder": str(folder)}).json()

    assert data["ok"] is True
    # Video is universal; the image is offered because Instagram and X publish
    # images now — the listing follows the same capability table as the preflight.
    assert sorted(item["name"] for item in data["items"]) == ["clip.mp4", "photo.jpg"]
    assert all(item["postable"] is True for item in data["items"])
    assert data["skipped_count"] == 0

    # Every offered entry survives the canonical preflight somewhere, and the
    # destinations that refuse it are exactly the ones the capability table says
    # cannot take that content type: the listing and the preflight are one fact.
    for item in data["items"]:
        modality = detect_modality(item["path"])
        assert modality is not None
        refusing = {
            platform
            for platform in PLATFORM_SPECS
            if "MEDIA_MODALITY_UNSUPPORTED" in _hard_blocker_codes(item["path"], platform)
        }
        accepting = set(PLATFORM_SPECS) - refusing
        assert accepting, f"/api/media offered {item['name']} but no destination accepts its content type"
        assert accepting == set(destinations_for_modality(modality)), (
            f"{item['name']}: offered, but the preflight disagrees about who can take it"
        )


def test_api_media_skips_a_file_no_destination_can_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The not-offered file is reported, never silently dropped (criterion 2).

    Images are publishable now, so the "skipped with a reason" contract is
    exercised in the state that produces it: a modality no destination accepts
    (exactly how images looked before this change).
    """
    from dataclasses import replace

    for name, spec in PLATFORM_SPECS.items():
        monkeypatch.setitem(PLATFORM_SPECS, name, replace(spec, modalities=(MODALITY_VIDEO,), image_containers=()))
    folder = _folder_with_video_and_image(tmp_path)

    with _open_app(tmp_path) as client:
        data = client.get("/api/media", params={"folder": str(folder)}).json()

    assert data["count"] == 1
    assert data["skipped_count"] == 1
    skipped = data["skipped"][0]
    assert skipped["name"] == "photo.jpg"
    assert skipped["type"] == "image"
    assert skipped["postable"] is False
    assert "image" in skipped["reason"]
    assert data["hint"] == skipped["reason"]

    # And the reason is the same fact the preflight reports: this file is
    # refused everywhere for its content type.
    for platform in PLATFORM_SPECS:
        assert "MEDIA_MODALITY_UNSUPPORTED" in _hard_blocker_codes(skipped["path"], platform)


def test_api_media_still_reports_the_folder_states_it_always_did(tmp_path: Path) -> None:
    with _open_app(tmp_path) as client:
        empty = client.get("/api/media").json()
        missing = client.get("/api/media", params={"folder": str(tmp_path / "nope")}).json()

    assert empty["items"] == [] and empty["skipped"] == []
    assert "No content folder" in empty["hint"]
    assert missing["ok"] is False and "Folder not found" in missing["error"]
    assert missing["skipped"] == [] and missing["skipped_count"] == 0


def test_api_media_offers_an_image_once_a_destination_can_publish_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flipping the capability flips the listing — one source of truth."""
    _install_spec(monkeypatch, "photo_place", _image_spec())
    folder = tmp_path / "media"
    folder.mkdir()
    _write_image(folder / "photo.jpg")

    with _open_app(tmp_path) as client:
        data = client.get("/api/media", params={"folder": str(folder)}).json()

    assert data["count"] == 1
    assert data["skipped_count"] == 0
    assert data["items"][0]["type"] == "image"
    assert data["items"][0]["postable"] is True


# ---------------------------------------------------------------------------
# The CLI and MCP-facing surfaces report the same single fact
# ---------------------------------------------------------------------------


def test_cli_verify_media_prints_one_modality_row(tmp_path: Path) -> None:
    """`xpst verify-media` shows one named refusal, not the same row twice."""
    import json

    from click.testing import CliRunner

    from xpst.cli import main

    photo = _write_image(tmp_path / "photo.jpg")

    result = CliRunner().invoke(
        main,
        ["verify-media", str(photo), "--platform", "youtube", "--json"],
        obj={},
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    checks = payload["reports"][0]["checks"]
    errors = [check for check in checks if check["status"] == "error"]
    assert len(errors) == 1, [(c["name"], c["detail"]) for c in errors]
    assert errors[0]["name"] == MODALITY_CHECK
    assert "YouTube cannot accept an image file (.jpg)" in errors[0]["detail"]
    assert "vs accepted" not in json.dumps(payload)


def test_cli_verify_media_accepts_an_image_at_a_destination_that_publishes_it(tmp_path: Path) -> None:
    """The same command, at a destination with a real image path, reports ok."""
    import json

    from click.testing import CliRunner

    from xpst.cli import main

    photo = _write_image(tmp_path / "photo.jpg")

    result = CliRunner().invoke(
        main,
        ["verify-media", str(photo), "--platform", "instagram", "--json"],
        obj={},
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    checks = payload["reports"][0]["checks"]
    names = {check["name"] for check in checks}
    assert "container" in names and "dimensions" in names
    assert MODALITY_CHECK not in names
    assert "video_stream" not in names


def test_cli_verify_media_still_reports_a_distinct_blocker(tmp_path: Path) -> None:
    """The dedupe must not hide a distinct blocker (only byte-identical echoes).

    A different fact about the same file — Threads needs a public URL — has its
    own message, so it survives alongside the container error.
    """
    import json

    from click.testing import CliRunner

    from xpst.cli import main

    bad = tmp_path / "bad.avi"
    bad.write_bytes(b"x" * 64)

    result = CliRunner().invoke(
        main,
        ["verify-media", str(bad), "--platform", "threads", "--json"],
        obj={},
    )

    assert result.exit_code == 1
    errors = [check for check in json.loads(result.output)["reports"][0]["checks"] if check["status"] == "error"]
    names = [check["name"] for check in errors]
    assert "container" in names, names
    assert "THREADS_NEEDS_URL" in names, names
    assert len(errors) == len({check["detail"] for check in errors}), "no two error rows may carry the same message"
