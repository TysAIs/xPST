"""The Threads media-transport truth, asserted against real behaviour.

Threads is the one destination xPST cannot hand a local file to. Meta's Threads
publishing docs are explicit: creating a media container takes ``video_url`` /
``image_url`` and "Threads retrieves your video from the URL provided, so it must
be on a public server". There is no binary, multipart or resumable upload
endpoint — only ``POST /{threads-user-id}/threads`` (container),
``threads_publish``, the container status field, repost and delete. xPST is a
local tool with no server, so it cannot produce that URL.

That limitation is declared **once**, in ``xpst.content``
(``MediaTransport.PUBLIC_URL`` + the destination profile's exact requirement),
and read from there by every surface: the provider manifest, request validation,
the preflight plan and the uploader.

These tests exist because "the plan says blocked" and "the upload really refuses"
are two different claims. The preflight verdict is checked against what the code
actually does — the uploader's real refusal, with the network turned into an
explicit failure — so the plan and the code cannot drift into disagreeing, and a
local file can never be promised and then silently dropped.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from xpst.config import XPSTConfig
from xpst.content import (
    PUBLISH_DESTINATIONS,
    THREADS_PUBLIC_URL_REQUIREMENT,
    ContentRequest,
    ContentType,
    MediaTransport,
    capability_matrix,
    content_profile,
    media_transport_blocker,
    validate_content_request,
)
from xpst.platforms.threads import ThreadsUploader
from xpst.services.post_preflight import PlatformPlan, PostPlanRequest, PostPreflightService

#: A local file of the modality Threads would otherwise publish. The content of
#: the file is irrelevant here: the transport rule is decided before anything is
#: probed, encoded or sent.
LOCAL_MEDIA_BYTES = b"not a real video"

#: A media reference the *user* already hosts — the one form Threads accepts.
PUBLIC_MEDIA_URL = "https://cdn.example.com/clip.mp4"


def _local_media(tmp_path: Path) -> Path:
    media = tmp_path / "clip.mp4"
    media.write_bytes(LOCAL_MEDIA_BYTES)
    return media


def _config(tmp_path: Path, *, platform: str = "threads") -> XPSTConfig:
    """A config with exactly one destination enabled, as the CLI builds it."""
    config = XPSTConfig(config_dir=str(tmp_path / "state"))
    for name in ("youtube", "x", "instagram", "tiktok", "threads"):
        getattr(config, name).enabled = name == platform
    if platform == "threads":
        config.threads.graph_access_token = "local-test-token"
        config.threads.threads_user_id = "123"
    return config


def _plan(config: XPSTConfig, media: Path | str, *, platform: str = "threads") -> PlatformPlan:
    return PostPreflightService(config).plan(
        PostPlanRequest(
            media_paths=(media,),
            target_platforms=(platform,),
            base_caption="caption",
            config=config,
        )
    ).platforms[platform]


def _transport_blockers(plan: PlatformPlan) -> list:
    profile = content_profile("threads")
    assert profile is not None
    return [issue for issue in plan.hard_blockers if issue.code == profile.media_transport_error_code]


# ── The declaration ─────────────────────────────────────────────────────────


def test_threads_declares_that_it_fetches_media_instead_of_taking_a_file() -> None:
    """Threads is a URL-fetch destination: a local file is not a capability it has."""
    profile = content_profile("threads")
    assert profile is not None

    assert profile.media_transport is MediaTransport.PUBLIC_URL
    assert profile.accepts_local_media is False

    # The requirement is specific, not a vague "may not work": it says what the
    # API does, that xPST has no server, why a local file fails, and what the
    # user can do instead.
    requirement = profile.media_transport_requirement
    assert requirement == THREADS_PUBLIC_URL_REQUIREMENT
    assert "no upload endpoint" in requirement
    assert "public server" in requirement
    assert "no server" in requirement
    assert "local file cannot be published to Threads" in requirement
    assert "post this content from the Threads app" in requirement

    # The error code is deliberately the one xPST already published, so an agent
    # (or a user's script) branching on it keeps working; only the wording, which
    # used to promise a future tunnel, changed.
    assert profile.media_transport_error_code == "THREADS_NEEDS_URL"


def test_the_manifest_and_the_capability_matrix_carry_the_same_declaration(tmp_path: Path) -> None:
    """A machine reading a manifest must not have to parse prose to learn this."""
    profile = content_profile("threads")
    assert profile is not None
    manifest = ThreadsUploader(_config(tmp_path)).manifest

    assert manifest.extra["media_transport"] == profile.media_transport.value
    assert manifest.extra["media_transport"] == MediaTransport.PUBLIC_URL.value

    row = capability_matrix()["platforms"]["threads"]
    assert row["media_transport"] == "public_url"
    assert row["accepts_local_media"] is False
    assert row["media_transport_requirement"] == profile.media_transport_requirement
    assert row["media_transport_error_code"] == "THREADS_NEEDS_URL"
    assert "threads" in capability_matrix()["publish_destinations"]


def test_the_constraint_is_targeted_and_not_applied_to_other_destinations(tmp_path: Path) -> None:
    """Every other publish destination still takes the bytes xPST has."""
    media = _local_media(tmp_path)
    assert "threads" in PUBLISH_DESTINATIONS

    for platform in PUBLISH_DESTINATIONS:
        if platform == "threads":
            continue
        profile = content_profile(platform)
        assert profile is not None
        assert profile.accepts_local_media is True, platform
        assert media_transport_blocker(platform, [media]) is None, platform


def test_media_transport_blocker_is_pure_and_only_fires_for_a_local_file(tmp_path: Path) -> None:
    """The one predicate behind every surface: no I/O, no false positives."""
    media = _local_media(tmp_path)

    assert media_transport_blocker("threads", [media]) is not None
    assert media_transport_blocker("threads", [str(media)]) is not None
    # Media the user already hosts is exactly what Threads can take.
    assert media_transport_blocker("threads", [PUBLIC_MEDIA_URL]) is None
    # No media, nothing to transport.
    assert media_transport_blocker("threads", []) is None
    # A destination that uploads the bytes is unaffected.
    assert media_transport_blocker("youtube", [media]) is None
    # A destination xPST knows nothing about is not accused of anything.
    assert media_transport_blocker("not_a_platform", [media]) is None


# ── The verdict, checked against what the code actually does ────────────────


def test_the_preflight_verdict_matches_what_the_uploader_actually_does(tmp_path: Path, monkeypatch: Any) -> None:
    """The card's test: the plan's verdict must be the code's real behaviour.

    The truth is taken from the uploader itself, called for real, with the
    network patched into an explicit failure — so a "refused" verdict can only
    come from a local decision, never from a request that happened and failed.
    """
    media = _local_media(tmp_path)
    config = _config(tmp_path)
    profile = content_profile("threads")
    assert profile is not None

    # 1. What the preflight tells a user before anything is sent.
    plan = _plan(config, media)
    blockers = _transport_blockers(plan)
    assert len(blockers) == 1, [issue.code for issue in plan.hard_blockers]
    assert plan.ready is False
    assert plan.readiness.auth.ready is True  # blocked by the media, not by auth

    # 2. What the code can actually do, called for real.
    network_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: network_calls.append(args))
    result = asyncio.run(ThreadsUploader(config).upload(media, "caption"))

    # 3. They agree — in code *and* in wording, because both read one declaration.
    assert result.success is False
    assert result.retryable is False
    assert network_calls == [], "the refusal must happen before any network call"
    assert result.error == f"{blockers[0].code}: {blockers[0].message}"
    assert result.error == f"{profile.media_transport_error_code}: {profile.media_transport_requirement}"


def test_request_validation_refuses_before_dispatch_with_the_same_code_and_wording(tmp_path: Path) -> None:
    """The engine's content validation is the third surface reading the declaration."""
    media = _local_media(tmp_path)
    profile = content_profile("threads")
    assert profile is not None

    issues = [
        issue
        for issue in validate_content_request(
            ContentRequest(content_type=ContentType.VIDEO, media=(str(media),), text="caption", platforms=("threads",))
        )
        if issue.platform == "threads"
    ]

    assert len(issues) == 1, [issue.code for issue in issues]
    assert issues[0].is_error
    assert issues[0].code == profile.media_transport_error_code
    assert issues[0].message == profile.media_transport_requirement


def test_a_local_file_is_reported_for_threads_without_any_network_call(tmp_path: Path, monkeypatch: Any) -> None:
    """Planning states the requirement; it never reaches out to find out."""
    media = _local_media(tmp_path)
    config = _config(tmp_path)
    profile = content_profile("threads")
    assert profile is not None

    network_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: network_calls.append(args))

    plan = _plan(config, media)

    assert network_calls == []
    assert [issue.code for issue in plan.hard_blockers] == [profile.media_transport_error_code]
    assert plan.hard_blockers[0].message == profile.media_transport_requirement
    assert plan.hard_blockers[0].media_path == str(media)


def test_the_same_verdict_reaches_the_plan_for_every_requested_destination(tmp_path: Path) -> None:
    """Threads is reported, blocked and explained — never silently dropped."""
    media = _local_media(tmp_path)
    config = XPSTConfig(config_dir=str(tmp_path / "state"))
    for name in ("youtube", "tiktok", "instagram", "x", "threads"):
        getattr(config, name).enabled = True
    profile = content_profile("threads")
    assert profile is not None

    requested = ("youtube", "tiktok", "instagram", "x", "threads")
    result = PostPreflightService(config).plan(
        PostPlanRequest(
            media_paths=(media,),
            target_platforms=requested,
            base_caption="caption",
            config=config,
            include_transform=False,
        )
    )

    assert tuple(result.platforms) == requested
    assert result.platforms["threads"].ready is False
    assert any(issue.code == profile.media_transport_error_code for issue in result.platforms["threads"].hard_blockers)
    # And no other destination inherits Threads' constraint.
    for platform in requested:
        if platform == "threads":
            continue
        assert not any(issue.code == profile.media_transport_error_code for issue in result.platforms[platform].hard_blockers)


# ── The other direction: a URL is only useful where it can be fetched ───────


def test_the_transport_rule_is_symmetric_for_url_media(tmp_path: Path) -> None:
    """Same declaration, both directions: no surface keeps its own platform list.

    This is the *destination-side* rule only — that Threads, which fetches media
    itself, is the one destination a URL is not rejected for, while a destination
    that needs the bytes rejects it. Whether xPST can actually deliver a URL is a
    different (and, today, unanswered) question, pinned by the test below.
    """
    config = _config(tmp_path)

    threads_plan = _plan(config, PUBLIC_MEDIA_URL)
    assert _transport_blockers(threads_plan) == []
    # A URL is never fetched or probed during planning, and the plan says so.
    assert any(issue.code == "MEDIA_REMOTE_NOT_PROBED" for issue in threads_plan.warnings)

    youtube_plan = _plan(_config(tmp_path, platform="youtube"), PUBLIC_MEDIA_URL, platform="youtube")
    assert any(issue.code == "REMOTE_MEDIA_UNSUPPORTED" for issue in youtube_plan.hard_blockers)
    assert youtube_plan.ready is False


@pytest.mark.xfail(
    strict=True,
    reason=(
        "known gap (kanban t_4f0f74c6): the publish pipeline hashes/probes/encodes a local file before any "
        "uploader runs, so a media URL cannot reach an uploader — engine.post_manual raises AttributeError "
        "on a str URL. After making the Threads transport constraint first-class, this is the one remaining "
        "place xPST is not a real Threads destination. It will XPASS when the remote-media door lands, and "
        "this test must then assert a real delivered-URL outcome instead."
    ),
)
def test_a_remote_url_cannot_reach_an_uploader_yet(tmp_path: Path) -> None:
    """xPST refuses a local file to Threads honestly; it cannot carry a URL at all.

    Stated as an executable fact rather than a comment, so nobody reads the
    preflight's remote-media branch as a working publish path.
    """
    from xpst.engine import CrossPostEngine

    config = _config(tmp_path)
    engine = CrossPostEngine(config)

    # A str URL, exactly what a URL-aware surface would hand the engine.
    result = asyncio.run(engine.post_manual(PUBLIC_MEDIA_URL, "url evidence", ["threads"]))  # type: ignore[arg-type]

    uploaded = result.results["threads"]
    assert uploaded.success is True or "THREADS_URL_NOT_DELIVERABLE" in (uploaded.error or "")


# ── The claim, kept out of the docs ─────────────────────────────────────────


def test_the_docs_do_not_advertise_a_local_file_path_for_threads() -> None:
    """Advertised-but-broken is the exact failure this work exists to stop.

    Both files used to promise a local-file path for Threads that the code has
    never had: the setup doc described "a two-step container upload" for local
    files, and the README's local-files example listed `threads` next to a
    `.mp4` path. The guard is deliberately phrase-level so the wording can be
    rewritten without re-introducing the claim.
    """
    root = Path(__file__).resolve().parents[1]
    setup_doc = (root / "docs" / "setup-threads.md").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    profile = content_profile("threads")
    assert profile is not None

    for name, text in (("docs/setup-threads.md", setup_doc), ("README.md", readme)):
        assert "two-step container upload" not in text.lower(), name
        assert profile.media_transport_error_code in text, name

    # A local-file example that includes Threads would promise a path that
    # cannot work, however the surrounding prose is worded.
    assert "-p youtube,x,threads" not in readme
    assert "-p youtube,instagram,x,threads" not in readme
