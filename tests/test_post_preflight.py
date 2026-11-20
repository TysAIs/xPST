"""Contract tests for the side-effect-free shared post preflight service."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from xpst.config import XPSTConfig
from xpst.services.post_preflight import PostPlanRequest, PostPreflightService

if TYPE_CHECKING:
    from pathlib import Path


def _config(tmp_path: Path, *, platform: str = "youtube") -> XPSTConfig:
    config = XPSTConfig(config_dir=str(tmp_path / "state"))
    for name in ("youtube", "x", "instagram", "tiktok", "threads"):
        getattr(config, name).enabled = name == platform
    account = getattr(config, platform)
    if platform == "youtube":
        token = tmp_path / "youtube-token.json"
        token.write_text("{}")
        account.token_file = str(token)
    elif platform == "x":
        cookies = tmp_path / "x-cookies.json"
        cookies.write_text("{}")
        account.cookies_file = str(cookies)
    elif platform == "instagram":
        session = tmp_path / "instagram-session.json"
        session.write_text("{}")
        account.session_file = str(session)
    elif platform == "tiktok":
        account.access_token = "local-test-token"
    elif platform == "threads":
        account.graph_access_token = "local-test-token"
        account.threads_user_id = "123"
    return config


def _request(
    path: Path | str,
    *,
    platform: str = "youtube",
    config: XPSTConfig | None = None,
    base_caption: str = "base caption",
    per_platform_captions: dict[str, str] | None = None,
) -> PostPlanRequest:
    return PostPlanRequest(
        media_paths=(path,),
        target_platforms=(platform,),
        base_caption=base_caption,
        per_platform_captions=per_platform_captions or {},
        config=config,
    )


def test_post_preflight_is_side_effect_free(tmp_path: Path, monkeypatch: Any) -> None:
    from xpst.utils.quota import QuotaManager

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"not a real video")
    config = _config(tmp_path)
    quota = QuotaManager(str(tmp_path / "quota-state"), config=config, persist=False)
    quota_before = quota.quotas["youtube"].to_dict()
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    network_calls: list[object] = []
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: network_calls.append(args))

    result = PostPreflightService(config, quota_manager=quota).plan(_request(media, config=config))

    after = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert before == after
    assert quota.quotas["youtube"].to_dict() == quota_before
    assert network_calls == []
    assert result.platforms["youtube"].effective_caption == "base caption"


def test_platform_specific_captions_are_exact(tmp_path: Path) -> None:
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"not a real video")
    config = _config(tmp_path, platform="youtube")
    config.x.enabled = True
    x_cookies = tmp_path / "x-cookies.json"
    x_cookies.write_text("{}")
    config.x.cookies_file = str(x_cookies)

    result = PostPreflightService(config).plan(
        PostPlanRequest(
            media_paths=(media,),
            target_platforms=("youtube", "x"),
            base_caption=" base caption  ",
            per_platform_captions={"x": "x caption\nwith exact spacing"},
            config=config,
        )
    )

    assert result.platforms["youtube"].effective_caption == " base caption  "
    assert result.platforms["x"].effective_caption == "x caption\nwith exact spacing"


def test_media_blockers_and_probe_warnings_are_distinct(tmp_path: Path) -> None:
    media = tmp_path / "clip.avi"
    media.write_bytes(b"not a real video")
    config = _config(tmp_path, platform="x")

    plan = PostPreflightService(config).plan(_request(media, platform="x", config=config)).platforms["x"]

    blocker_codes = {issue.code for issue in plan.hard_blockers}
    warning_codes = {issue.code for issue in plan.warnings}
    assert any(code.startswith("MEDIA_SPEC_") for code in blocker_codes)
    assert "MEDIA_PROBE_FAILED" in warning_codes
    assert plan.ready is False


def test_exhausted_quota_is_a_blocker_without_consuming_quota(tmp_path: Path) -> None:
    from xpst.utils.quota import PlatformQuota, QuotaManager

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"not a real video")
    config = _config(tmp_path)
    quota = QuotaManager(str(tmp_path / "quota-state"), config=config, persist=False)
    quota.quotas["youtube"] = PlatformQuota(platform="youtube", daily_limit=5, used_today=5)

    plan = PostPreflightService(config, quota_manager=quota).plan(_request(media, config=config)).platforms["youtube"]

    assert any(issue.code == "QUOTA_NOT_READY" for issue in plan.hard_blockers)
    assert quota.quotas["youtube"].used_today == 5
    assert not (tmp_path / "quota-state" / "quotas.json").exists()


def test_directory_is_rejected_as_a_hard_blocker(tmp_path: Path) -> None:
    directory = tmp_path / "media-directory"
    directory.mkdir()
    config = _config(tmp_path)

    plan = PostPreflightService(config).plan(_request(directory, config=config)).platforms["youtube"]

    assert plan.media[0].file_type == "directory"
    assert any(issue.code == "MEDIA_DIRECTORY" for issue in plan.hard_blockers)
    assert not any(issue.code == "MEDIA_PROBE_FAILED" for issue in plan.warnings)


def test_threads_reports_local_file_unavailability_without_network(tmp_path: Path) -> None:
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"not a real video")
    config = _config(tmp_path, platform="threads")

    plan = PostPreflightService(config).plan(_request(media, platform="threads", config=config)).platforms["threads"]

    assert any(issue.code == "THREADS_NEEDS_URL" for issue in plan.hard_blockers)
    assert plan.readiness.auth.ready is True
    assert plan.ready is False


def test_all_requested_platforms_are_present_and_threads_is_blocked(tmp_path: Path) -> None:
    """A local-file plan must not silently omit Threads from its result."""
    media = tmp_path / "canary.mp4"
    media.write_bytes(b"not a real video")
    config = XPSTConfig(config_dir=str(tmp_path / "state"))
    config.threads.enabled = True
    config.threads.graph_access_token = "local-test-token"
    config.threads.threads_user_id = "123"

    requested = ("youtube", "tiktok", "instagram", "x", "threads")
    result = PostPreflightService(config).plan(
        PostPlanRequest(
            media_paths=(media,),
            target_platforms=requested,
            base_caption="canary",
            config=config,
            include_transform=True,
        )
    )

    assert tuple(result.platforms) == requested
    assert set(result.platforms) == set(requested)
    assert any(issue.code == "THREADS_NEEDS_URL" for issue in result.platforms["threads"].hard_blockers)
    assert "threads" in result.to_dict()["platforms"]


def test_verify_media_json_keeps_threads_in_reports_and_plans(tmp_path: Path) -> None:
    """The compatibility CLI must expose every requested platform.

    Threads is enabled in the config here: since the 2026-09-28 defect wave,
    ``--platform all`` resolves to the *enabled* destinations (the same rule
    every post path uses), so a disabled destination must set ``enabled: true``
    to appear — see :func:`test_verify_media_all_skips_disabled_destination`.
    """
    from click.testing import CliRunner

    from xpst.cli import main

    media = tmp_path / "canary.mp4"
    media.write_bytes(b"not a real video")
    config_path = tmp_path / "config.yaml"
    cfg = XPSTConfig(config_dir=str(tmp_path / "state"))
    cfg.threads.enabled = True
    cfg.save(str(config_path))

    result = CliRunner().invoke(
        main,
        [
            "--config",
            str(config_path),
            "verify-media",
            str(media),
            "--platform",
            "all",
            "--plan",
            "--json",
        ],
        obj={},
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    # Canonical destination order (xpst.services.post_preflight.DESTINATION_ORDER).
    requested = ["youtube", "x", "instagram", "tiktok", "threads"]
    assert [report["platform"] for report in payload["reports"]] == requested
    assert [plan["platform"] for plan in payload["plans"]] == requested
    assert payload["ok"] is False
    assert "THREADS_NEEDS_URL" in result.output


def test_verify_media_all_skips_disabled_destination(tmp_path: Path) -> None:
    """D4: a destination switched off in config must not fail the exit code.

    The shipped defect: with the shipped default (``threads.enabled: false``),
    ``verify-media -p all`` exited 1 solely on Threads' THREADS_NEEDS_URL —
    an error the disabled destination would never have produced, because no
    upload to it was ever going to happen. `-p all` now verifies the enabled
    destinations only, and a healthy file exits 0.
    """
    from click.testing import CliRunner

    from xpst.cli import main

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"not a real video")
    config_path = tmp_path / "config.yaml"
    cfg = XPSTConfig(config_dir=str(tmp_path / "state"))
    # The shipped DEFAULT_CONFIG writes `threads: enabled: false`; mirror it.
    cfg.threads.enabled = False
    cfg.save(str(config_path))

    result = CliRunner().invoke(
        main,
        ["--config", str(config_path), "verify-media", str(media), "--platform", "all", "--json"],
        obj={},
    )

    payload = json.loads(result.output)
    reported = [report["platform"] for report in payload["reports"]]
    assert "threads" not in reported
    assert "THREADS_NEEDS_URL" not in result.output
    # The unusable-but-plausibly-named file now only WARNs on the enabled
    # platforms: with a real Threads blocker removed, the honest verdict is a
    # clean pass, exit 0 — the exact inversion of the shipped defect.
    assert result.exit_code == 0
    assert payload["ok"] is True

    # A file the enabled platforms really do reject still fails.
    bad = tmp_path / "bad.avi"
    bad.write_bytes(b"x" * 64)
    failed = CliRunner().invoke(
        main,
        ["--config", str(config_path), "verify-media", str(bad), "--platform", "all", "--json"],
        obj={},
    )
    assert failed.exit_code == 1
    assert json.loads(failed.output)["ok"] is False

    # Explicitly naming a disabled destination still verifies it — and says so.
    named = CliRunner().invoke(
        main,
        ["--config", str(config_path), "verify-media", str(media), "--platform", "threads", "--json"],
        obj={},
    )
    named_payload = json.loads(named.output)
    assert [report["platform"] for report in named_payload["reports"]] == ["threads"]
    assert named_payload["disabled_platforms"] == ["threads"]


def test_post_preflight_json_is_deterministic(tmp_path: Path) -> None:
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"not a real video")
    config = _config(tmp_path)
    request = _request(media, config=config)
    service = PostPreflightService(config)

    first = service.plan(request).to_json()
    second = service.plan(request).to_json()

    assert first == second
    assert json.loads(first)["platforms"]["youtube"]["effective_caption"] == "base caption"
