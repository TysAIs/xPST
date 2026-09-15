"""Contract tests for declared-capability and limit truth.

These pin four defects reproduced in the 2026-09-15 stack audit:

1. The TikTok provider manifest omitted the duration ceiling, while the upload
   duration preflight reads the MANIFEST value. ``media.specs`` knew about the
   600s cap but the check could never fire for TikTok.
2. Threads advertised a ``text`` content capability and Instagram an ``image``
   one, while neither has an implementation (the container calls hard-code
   ``media_type=VIDEO`` / ``REELS``). Agents read that list from
   ``xpst_capabilities`` and will attempt operations that cannot work.
3. An over-length X caption was silently truncated at publish time
   (``x.py`` slices to 277 characters plus an ellipsis) and nothing in the plan
   said so, so a mutilated caption shipped with no signal.
4. ``xpst_run`` returned a hard-coded ``ok: true``, so an agent that trusted it
   reported success for a run that published nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from xpst.config import XPSTConfig
from xpst.engine import CrossPostResult
from xpst.media.specs import PLATFORM_SPECS
from xpst.platforms.base import UploadResult
from xpst.platforms.instagram import InstagramUploader
from xpst.platforms.threads import ThreadsUploader
from xpst.platforms.tiktok import TikTokUploader
from xpst.services.post_preflight import PostPlanRequest, PostPreflightService

if TYPE_CHECKING:
    from pathlib import Path


def _manifest(uploader_cls, tmp_path: Path):
    config = XPSTConfig(config_dir=str(tmp_path / "state"))
    return uploader_cls(config).manifest


def test_tiktok_declares_a_duration_ceiling_matching_the_media_spec(tmp_path: Path) -> None:
    """The duration preflight reads the manifest, so the two sources must agree."""
    manifest = _manifest(TikTokUploader, tmp_path)

    declared = manifest.extra.get("max_video_duration_seconds")

    assert declared is not None, (
        "the TikTok manifest must declare a duration ceiling or the upload "
        "duration preflight silently never fires for TikTok"
    )
    assert declared == PLATFORM_SPECS["tiktok"].duration_cap_s


def test_threads_does_not_advertise_a_text_capability_it_cannot_deliver(
    tmp_path: Path,
) -> None:
    manifest = _manifest(ThreadsUploader, tmp_path)

    assert "text" not in manifest.extra["content"]
    assert "video" in manifest.extra["content"]
    assert "text" not in manifest.notes.lower()


def test_instagram_does_not_advertise_an_image_capability_it_cannot_deliver(
    tmp_path: Path,
) -> None:
    manifest = _manifest(InstagramUploader, tmp_path)

    assert "image" not in manifest.extra["content"]
    assert "video" in manifest.extra["content"]


def _platform_plan(tmp_path: Path, platform: str, caption: str):
    """Build a real preflight plan for one platform and one caption."""
    media = tmp_path / f"{platform}-clip.mp4"
    media.write_bytes(b"not a real video")
    config = XPSTConfig(config_dir=str(tmp_path / "state"))
    for name in ("youtube", "x", "instagram", "tiktok", "threads"):
        getattr(config, name).enabled = name == platform
    account = getattr(config, platform)
    if platform == "x":
        cookies = tmp_path / "x-cookies.json"
        cookies.write_text("{}")
        account.cookies_file = str(cookies)
    elif platform == "threads":
        account.graph_access_token = "local-test-token"
        account.threads_user_id = "123"

    request = PostPlanRequest(
        media_paths=(media,),
        target_platforms=(platform,),
        base_caption=caption,
        config=config,
    )
    return PostPreflightService(config).plan(request).platforms[platform]


def test_overlength_x_caption_is_reported_instead_of_silently_truncated(
    tmp_path: Path,
) -> None:
    plan = _platform_plan(tmp_path, "x", "x" * 300)

    blockers = [issue for issue in plan.hard_blockers if issue.code == "CAPTION_TOO_LONG"]
    assert not blockers, (
        "X truncates an over-length caption rather than rejecting the post, so "
        "reporting is enough - it must not hard-block a post that would work"
    )

    reported = [issue for issue in plan.warnings if issue.code == "CAPTION_TOO_LONG"]
    assert reported, "an over-length X caption must be reported, not silently truncated"
    assert "truncated" in reported[0].message
    assert reported[0].severity == "warning"


def test_threads_overlength_caption_remains_a_hard_blocker(tmp_path: Path) -> None:
    """Threads rejects the caption outright, so the severity must stay a blocker."""
    plan = _platform_plan(tmp_path, "threads", "t" * 600)

    blockers = [issue for issue in plan.hard_blockers if issue.code == "CAPTION_TOO_LONG"]
    assert blockers
    assert blockers[0].severity == "blocker"


def test_plan_constraints_still_report_the_caption_ceiling(tmp_path: Path) -> None:
    """The machine-readable plan must keep exposing the limit as a plain int."""
    plan = _platform_plan(tmp_path, "x", "short")

    assert plan.constraints["caption"]["max_characters"] == 280
    assert plan.constraints["caption"]["observed_characters"] == 5


def test_run_payload_reports_failure_truthfully() -> None:
    """``xpst_run`` must not claim success for a run that failed."""
    from xpst.mcp.server import _run_payload

    published = CrossPostResult(video_id="v-published", caption="c")
    published.results["youtube"] = UploadResult(
        success=True,
        post_id="post-1",
        post_url="https://example.invalid/post-1",
        platform="youtube",
    )
    published.update_status()

    failed = CrossPostResult(video_id="v-failed", caption="c")
    failed.results["youtube"] = UploadResult(
        success=False, error="provider rejected the upload", platform="youtube"
    )
    failed.update_status()

    payload = _run_payload([published, failed])

    assert payload["ok"] is False
    assert payload["processed"] == 2
    assert payload["succeeded"] == 1
    assert payload["failed"] == 1

    # Nothing to do is not a failure, and a clean run is reported as success.
    assert _run_payload([])["ok"] is True
    assert _run_payload([published])["ok"] is True
    assert _run_payload([published])["failed"] == 0
