"""Opt-in platform targets and the capability-driven route resolver (R3/R4).

The contract these pin comes from ``docs/PROPOSAL-PRODUCT-RESET.md`` §4 and §5:

* R3 — every platform is optional. A post targets an explicit set, or the format
  route ∩ connected accounts. ``--all`` is explicit. A requested destination
  that is not connected is ``skipped_unconnected`` — never a failure — and zero
  connected accounts is ONE actionable error naming ``xpst connect``.
* R4 — the route is resolved from the one capability registry (the document
  ``xpst capabilities`` emits), printed before anything publishes, and refused
  with the envelope instead of truncated.

No test here touches the network or uploads a byte: the engine test uses a fake
uploader behind the real ``post_text`` path.
"""

from __future__ import annotations

import asyncio
from itertools import combinations
from typing import TYPE_CHECKING, Any

import pytest

from xpst.config import XPSTConfig
from xpst.content import capability_document
from xpst.engine import CrossPostEngine
from xpst.media.specs import PLATFORM_SPECS
from xpst.platforms.base import UploadResult
from xpst.services.batch_outcome import BATCH_PARTIAL, BATCH_PUBLISHED, batch_outcome, post_exit_code
from xpst.services.route_plan import (
    FAILED,
    NO_CONNECTED_ACCOUNTS_CODE,
    POSTED,
    SKIPPED_UNCONNECTED,
    UNSUPPORTED_FORMAT,
    build_route_plan,
    classify_upload_result,
    connect_command,
    no_connected_accounts_message,
    outcome_state,
    resolve_targets,
    text_refusal,
)

if TYPE_CHECKING:
    from pathlib import Path

_PLATFORMS = ("youtube", "x", "instagram", "tiktok", "threads", "facebook")


def _credential(tmp_path: Path, platform: str) -> Path:
    """A local credential marker so the plan reports the account as connected."""
    files = {
        "youtube": "youtube-token.json",
        "x": "x-cookies.json",
        "instagram": "instagram-session.json",
    }
    path = tmp_path / files.get(platform, f"{platform}.json")
    path.write_text("{}", encoding="utf-8")
    return path


def _config(tmp_path: Path, enabled: tuple[str, ...]) -> XPSTConfig:
    """Real config with exactly ``enabled`` accounts connected (opt-in)."""
    config = XPSTConfig(config_dir=str(tmp_path / "state"))
    for name in _PLATFORMS:
        if hasattr(config, name):
            getattr(config, name).enabled = name in enabled
    for name in enabled:
        account = getattr(config, name, None)
        if account is None:
            continue
        if name == "youtube":
            account.token_file = str(_credential(tmp_path, name))
        elif name == "x":
            account.cookies_file = str(_credential(tmp_path, name))
        elif name == "instagram":
            account.session_file = str(_credential(tmp_path, name))
        elif name == "tiktok":
            account.access_token = "test-token"
        elif name == "threads":
            account.graph_access_token = "test-token"
            account.threads_user_id = "123"
        elif name == "facebook":
            account.page_access_token = "test-token"
            account.page_id = "123"
    return config


# ── R3: target sets ─────────────────────────────────────────────────────────


def test_every_subset_of_the_connected_set_is_a_valid_target_set(tmp_path: Path) -> None:
    """1..N-1 destinations: any subset is honoured exactly, in request order."""
    connected = ("youtube", "x", "facebook")
    config = _config(tmp_path, connected)
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"\x00" * 64)

    for size in range(1, len(connected)):
        for subset in combinations(connected, size):
            plan = build_route_plan(
                config,
                media_paths=[str(media)],
                caption="hello",
                content_type="video",
                requested=list(reversed(subset)),
                include_transform=False,
                include_readiness=False,
            )
            assert plan.posting_destinations == tuple(reversed(subset))
            assert plan.source == "explicit"
            assert {item.state for item in plan.destinations} == {POSTED}


def test_no_targets_resolves_the_format_route_intersected_with_connected(
    tmp_path: Path,
) -> None:
    """A newly connected account never joins a post that cannot use it, and the
    resolved set is stated (source=format_route), not implied."""
    config = _config(tmp_path, ("youtube", "x"))

    video = resolve_targets(config, content_type="video")
    assert video.targets == ("youtube", "x")
    assert video.source == "format_route"

    # X publishes text; YouTube does not — a text post must not be routed there.
    text = resolve_targets(config, content_type="text")
    assert text.targets == ("x",)


def test_all_is_explicit_and_means_connected_accounts_that_accept_the_format(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, ("youtube", "x"))

    text = resolve_targets(config, None, all_connected=True, content_type="text")
    assert text.source == "all"
    assert text.targets == ("x",)  # YouTube cannot publish text

    video = resolve_targets(config, None, all_connected=True, content_type="video")
    assert video.targets == ("youtube", "x")


def test_zero_connected_accounts_is_one_actionable_error(tmp_path: Path) -> None:
    config = _config(tmp_path, ())

    resolution = resolve_targets(config)
    assert resolution.targets == ()
    assert resolution.errors[0]["code"] == NO_CONNECTED_ACCOUNTS_CODE
    message = resolution.errors[0]["message"]
    assert "xpst connect" in message
    assert message == no_connected_accounts_message()
    assert connect_command("youtube") == "`xpst connect youtube`"

    plan = build_route_plan(config, media_paths=[], caption="hi", content_type="text")
    assert plan.ready is False
    assert plan.actionable_error is not None
    assert plan.actionable_error["code"] == NO_CONNECTED_ACCOUNTS_CODE


def test_format_with_no_connected_destination_names_the_format(tmp_path: Path) -> None:
    """A connected account that cannot take this format is not a silent empty run."""
    config = _config(tmp_path, ("youtube",))

    resolution = resolve_targets(config, content_type="text")
    assert resolution.targets == ()
    assert resolution.errors[0]["code"] == "NO_ROUTE_FOR_FORMAT"
    assert "text" in resolution.errors[0]["message"]
    assert "youtube" in resolution.errors[0]["message"]


def test_requested_but_unconnected_target_is_skipped_not_failed(tmp_path: Path) -> None:
    config = _config(tmp_path, ("x",))

    plan = build_route_plan(
        config,
        media_paths=[],
        caption="hello",
        content_type="text",
        requested=["threads", "x"],
        include_transform=False,
        include_readiness=False,
    )
    states = {item.platform: item.state for item in plan.destinations}
    assert states == {"threads": SKIPPED_UNCONNECTED, "x": POSTED}
    assert plan.posting_destinations == ("x",)
    threads = plan.destination("threads")
    assert threads is not None
    assert "`xpst connect threads`" in threads.reason
    assert threads.connect_command == "xpst connect threads"


def test_a_route_of_only_unconnected_targets_does_not_fail(tmp_path: Path) -> None:
    """The aggregate outcome of all-skips is "skipped", and the exit code stays 0."""
    skipped = classify_upload_result(
        "threads",
        UploadResult(
            success=False,
            error="SKIPPED_UNCONNECTED: threads is not connected",
            platform="threads",
            metadata={"state": SKIPPED_UNCONNECTED},
        ),
    )
    assert skipped["state"] == SKIPPED_UNCONNECTED
    assert skipped["is_failure"] is False
    assert outcome_state({"threads": skipped}) == "skipped"

    from xpst.engine import CrossPostResult

    result = CrossPostResult(video_id="text-1", caption="hi")
    result.results["threads"] = UploadResult(
        success=False,
        error="SKIPPED_UNCONNECTED: threads is not connected",
        platform="threads",
        metadata={"state": SKIPPED_UNCONNECTED},
    )
    assert post_exit_code(result, ["threads"]) == 0
    verdict = batch_outcome([result])
    assert verdict["status"] == "skipped"
    assert verdict["exit_code"] == 0
    assert verdict["attempted"] == 0
    assert verdict["skipped_unconnected"] == 1


# ── R4: format-aware routing ────────────────────────────────────────────────


def test_unsupported_format_names_the_capability_and_the_envelope(tmp_path: Path) -> None:
    config = _config(tmp_path, ("youtube", "x"))

    plan = build_route_plan(
        config,
        media_paths=[],
        caption="hello",
        content_type="text",
        requested=["youtube"],
        include_transform=False,
        include_readiness=False,
    )
    destination = plan.destinations[0]
    assert destination.state == UNSUPPORTED_FORMAT
    assert "cannot publish text posts" in destination.reason
    # The refusal carries the envelope (the video limits YouTube does accept).
    assert "video" in destination.reason
    assert destination.envelope is not None
    assert destination.envelope["video"]["containers"]


def test_caption_over_the_limit_is_refused_with_the_envelope(tmp_path: Path) -> None:
    """Refuse, never truncate: the message names the limit and the destination."""
    config = _config(tmp_path, ("x",))
    long_caption = "a" * 281

    plan = build_route_plan(
        config,
        media_paths=[],
        caption=long_caption,
        content_type="text",
        requested=["x"],
        include_transform=False,
        include_readiness=False,
    )
    destination = plan.destinations[0]
    assert destination.state == UNSUPPORTED_FORMAT
    assert "280" in destination.reason
    assert "281" in destination.reason
    assert "refuses rather than truncates" in destination.reason

    refusal = text_refusal("x", long_caption)
    assert refusal is not None and "Envelope" in refusal
    # A per-destination override makes the same post fit without editing the body.
    fitted = build_route_plan(
        config,
        media_paths=[],
        caption=long_caption,
        per_platform_captions={"x": "short copy"},
        content_type="text",
        requested=["x"],
        include_transform=False,
        include_readiness=False,
    )
    assert fitted.destinations[0].state == POSTED
    assert len(fitted.destinations[0].caption) == len("short copy")


def test_envelope_comes_from_the_one_registry_the_capabilities_document_emits() -> None:
    document = capability_document()
    assert document["contract_version"] >= 2
    envelopes = document["envelopes"]
    assert envelopes["x"]["video"]["max_duration_seconds"] == PLATFORM_SPECS["x"].duration_cap_s
    assert envelopes["x"]["caption"]["max_characters"] == 280
    assert envelopes["youtube"]["caption"]["max_characters"] is None
    # Format routes are derived from the same implemented capability, not a copy.
    assert document["format_routes"]["text"] == ["x", "threads"]
    assert "youtube" in document["format_routes"]["video"]


# ── outcomes: partial failure is a successful outcome ───────────────────────


def test_partial_failure_is_a_successful_outcome() -> None:
    from xpst.engine import CrossPostResult

    result = CrossPostResult(video_id="clip-1", caption="hi")
    result.results["x"] = UploadResult(success=True, post_id="1", post_url="https://x/1", platform="x")
    result.results["youtube"] = UploadResult(
        success=False, error="QUOTA_EXHAUSTED: daily limit", platform="youtube"
    )
    result.update_status()

    states = {
        platform: classify_upload_result(platform, upload)
        for platform, upload in result.results.items()
    }
    assert states["x"]["state"] == POSTED
    assert states["youtube"]["state"] == FAILED
    assert outcome_state(states) == "partial"

    # The route succeeded: something published, so the exit code is 0 and the
    # batch reports the failed destination by name (never as a total failure).
    assert post_exit_code(result, ["x", "youtube"]) == 0
    verdict = batch_outcome([result])
    assert verdict["exit_code"] == 0
    assert verdict["status"] == BATCH_PARTIAL
    assert verdict["failed_destinations"][0]["platform"] == "youtube"


def test_all_published_is_not_partial() -> None:
    from xpst.engine import CrossPostResult

    result = CrossPostResult(video_id="clip-2", caption="hi")
    result.results["x"] = UploadResult(success=True, post_id="1", post_url="u", platform="x")
    result.results["youtube"] = UploadResult(success=True, post_id="2", post_url="u", platform="youtube")
    result.update_status()

    verdict = batch_outcome([result])
    assert verdict["exit_code"] == 0
    assert verdict["status"] == BATCH_PUBLISHED


# ── the engine writes the state row, not a silent drop ─────────────────────


class _FakeTextUploader:
    """Minimal stand-in with the real uploader's text entry point."""

    def __init__(self, success: bool = True) -> None:
        self.success = success
        self.calls: list[str] = []

    async def post_text(self, text: str, **kwargs: Any) -> UploadResult:
        self.calls.append(text)
        if self.success:
            return UploadResult(success=True, post_id="1", post_url="https://x/1", platform="x")
        return UploadResult(success=False, error="boom", platform="x")


@pytest.fixture()
def engine(tmp_path: Path):
    config = _config(tmp_path, ("x",))
    return CrossPostEngine(config)


def test_engine_records_skipped_unconnected_instead_of_dropping_it(engine, monkeypatch: Any) -> None:
    uploader = _FakeTextUploader()
    monkeypatch.setattr(engine, "_platforms", {"x": uploader})

    result = asyncio.run(engine.post_text("hello", ["x", "threads"]))

    assert uploader.calls == ["hello"]
    assert set(result.results) == {"x", "threads"}
    assert result.results["x"].success is True
    threads = result.results["threads"]
    assert threads.success is False
    assert threads.metadata["state"] == SKIPPED_UNCONNECTED
    assert threads.metadata["connect_command"] == "xpst connect threads"

    # Unconnected is not a failure: the post published, exit 0, nothing failed.
    assert post_exit_code(result, ["x", "threads"]) == 0
    verdict = batch_outcome([result])
    assert verdict["exit_code"] == 0
    assert verdict["failed"] == 0
    assert verdict["skipped_unconnected"] == 1


def test_engine_refuses_a_format_the_destination_cannot_publish(engine, monkeypatch: Any) -> None:
    """A destination with no path for this format is refused by name, not uploaded."""
    uploader = _FakeTextUploader()
    monkeypatch.setattr(engine, "_platforms", {"x": uploader, "youtube": _FakeTextUploader()})

    result = asyncio.run(engine.post_text("hello", ["youtube", "x"]))

    youtube = result.results["youtube"]
    assert youtube.success is False
    assert youtube.metadata["state"] == UNSUPPORTED_FORMAT
    assert "cannot publish text" in (youtube.error or "")
    # Only the capable destination was called.
    assert uploader.calls == ["hello"]


def test_a_deferred_destination_is_deferred_not_failed() -> None:
    """M0: a cooldown/window deferral must never be reported as a failure.

    Measured before this: a rate-limit cooldown returned ``metadata.deferred``
    with the platform's resume window, and the canonical classifier turned that
    into ``failed`` — a phone saw a red row for a post that had not been tried.
    """
    deferred = classify_upload_result(
        "x",
        UploadResult(
            success=False,
            error="RATE_LIMIT_COOLDOWN: x cooling, resume after 2026-11-07T23:45:00",
            platform="x",
            metadata={
                "deferred": True,
                "cooldown": True,
                "resume_after": "2026-11-07T23:45:00",
                "remaining_s": 240.0,
            },
        ),
    )
    assert deferred["state"] == "deferred"
    assert deferred["is_failure"] is False
    assert deferred["resume_after"] == "2026-11-07T23:45:00"
    # All-deferred is its own outcome word, not "failed".
    assert outcome_state({"x": deferred}) == "deferred"

    # A posted destination plus a deferred one is still a successful run.
    posted = classify_upload_result("youtube", UploadResult(success=True, post_id="v1", platform="youtube"))
    assert outcome_state({"youtube": posted, "x": deferred}) == POSTED
