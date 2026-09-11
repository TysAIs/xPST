"""Shared, side-effect-free post planning and media preflight.

The service is the common seam for future CLI, desktop, and MCP clients.  It
only reads local configuration, credential-file metadata, quota usage, and
media files.  It never uploads, records quota usage, refreshes credentials,
changes state, creates temporary output, or performs network I/O.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlparse

from xpst.config import EncodingConfig, XPSTConfig
from xpst.media.pipeline import TransformPlan, plan_transform
from xpst.media.specs import PLATFORM_SPECS, Check, MediaReport, verify_media
from xpst.utils.quota import QuotaManager
from xpst.utils.video import VideoProcessor

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_SUPPORTED_PLATFORMS = tuple(PLATFORM_SPECS)
_CAPTION_LIMITS = {"threads": 500}


@dataclass(frozen=True)
class PreflightIssue:
    """A stable, machine-readable warning or hard blocker."""

    code: str
    message: str
    severity: str
    media_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "media_path": self.media_path,
        }


@dataclass(frozen=True)
class AuthReadiness:
    """Local-only authentication readiness; no health endpoint is contacted."""

    ready: bool
    status: str
    detail: str
    auth_mode: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "status": self.status,
            "detail": self.detail,
            "auth_mode": self.auth_mode,
        }


@dataclass(frozen=True)
class QuotaReadiness:
    """Read-only quota snapshot for a target platform."""

    ready: bool
    remaining: dict[str, int | None]
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "remaining": self.remaining,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PlatformReadiness:
    """Enabled, auth, and quota gates for one target."""

    enabled: bool
    auth: AuthReadiness
    quota: QuotaReadiness

    @property
    def ready(self) -> bool:
        return self.enabled and self.auth.ready and self.quota.ready

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "auth": self.auth.to_dict(),
            "quota": self.quota.to_dict(),
            "ready": self.ready,
        }


@dataclass(frozen=True)
class PostPlanRequest:
    """Input accepted by :class:`PostPreflightService`.

    Values are normalized at construction time, but caption values are never
    trimmed or otherwise changed.  That makes the selected caption safe to
    display and send verbatim after a later execution step.
    """

    media_paths: Sequence[Path | str]
    target_platforms: Sequence[str]
    base_caption: str = ""
    per_platform_captions: Mapping[str, str] = field(default_factory=dict)
    platform_captions: Mapping[str, str] | None = None
    captions: Mapping[str, str] | None = None
    config: XPSTConfig | None = None
    include_transform: bool = True
    include_readiness: bool = True
    check_loudness: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "media_paths", tuple(self.media_paths))
        normalized = tuple(dict.fromkeys(str(platform).strip().lower() for platform in self.target_platforms))
        object.__setattr__(self, "target_platforms", normalized)
        caption_overrides: dict[str, str] = {}
        for mapping in (self.captions, self.platform_captions, self.per_platform_captions):
            if mapping:
                caption_overrides.update(
                    {str(platform).strip().lower(): caption for platform, caption in mapping.items()}
                )
        object.__setattr__(self, "per_platform_captions", caption_overrides)


@dataclass
class MediaFilePlan:
    """Local file facts, probe/spec results, and transform decision."""

    path: str
    file_type: str
    exists: bool | None
    is_file: bool | None
    size_bytes: int | None
    ffprobe: dict[str, Any] | None
    media_spec: dict[str, Any]
    duration_seconds: float | None
    aspect_ratio: float | None
    transform: TransformPlan | None
    warnings: tuple[PreflightIssue, ...] = ()
    hard_blockers: tuple[PreflightIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.hard_blockers

    @property
    def file_exists(self) -> bool | None:
        """Compatibility spelling for clients that prefer an explicit name."""
        return self.exists

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "file_type": self.file_type,
            "exists": self.exists,
            "file_exists": self.file_exists,
            "is_file": self.is_file,
            "size_bytes": self.size_bytes,
            "ffprobe": self.ffprobe,
            "media_spec": self.media_spec,
            "duration_seconds": self.duration_seconds,
            "aspect_ratio": self.aspect_ratio,
            "transform": self.transform.to_dict() if self.transform else None,
            "warnings": [issue.to_dict() for issue in self.warnings],
            "hard_blockers": [issue.to_dict() for issue in self.hard_blockers],
            "ok": self.ok,
        }


@dataclass
class PlatformPlan:
    """Normalized plan for one requested destination platform."""

    platform: str
    effective_caption: str
    media: tuple[MediaFilePlan, ...]
    constraints: dict[str, Any]
    readiness: PlatformReadiness
    warnings: tuple[PreflightIssue, ...] = ()
    hard_blockers: tuple[PreflightIssue, ...] = ()

    @property
    def caption(self) -> str:
        """Compatibility alias for clients that call the caption ``caption``."""
        return self.effective_caption

    @property
    def ready(self) -> bool:
        return not self.hard_blockers and self.readiness.ready

    @property
    def enabled(self) -> bool:
        return self.readiness.enabled

    @property
    def auth_ready(self) -> bool:
        return self.readiness.auth.ready

    @property
    def quota_ready(self) -> bool:
        return self.readiness.quota.ready

    @property
    def ok(self) -> bool:
        return self.ready

    @property
    def transform(self) -> TransformPlan | None:
        """Return the single-media transform for convenient client access."""
        return self.media[0].transform if len(self.media) == 1 else None

    @property
    def transforms(self) -> tuple[TransformPlan, ...]:
        return tuple(item.transform for item in self.media if item.transform is not None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "effective_caption": self.effective_caption,
            "caption": self.effective_caption,
            "media": [item.to_dict() for item in self.media],
            "transform": self.transform.to_dict() if self.transform else None,
            "transforms": [transform.to_dict() for transform in self.transforms],
            "constraints": self.constraints,
            "readiness": self.readiness.to_dict(),
            "enabled": self.enabled,
            "auth_ready": self.auth_ready,
            "quota_ready": self.quota_ready,
            "warnings": [issue.to_dict() for issue in self.warnings],
            "hard_blockers": [issue.to_dict() for issue in self.hard_blockers],
            "ready": self.ready,
            "ok": self.ok,
        }


@dataclass
class PostPlanResult:
    """Complete normalized post plan, preserving requested platform order."""

    platforms: dict[str, PlatformPlan]

    @property
    def plans(self) -> dict[str, PlatformPlan]:
        """Alias used by clients that call the per-platform map ``plans``."""
        return self.platforms

    @property
    def ready(self) -> bool:
        return all(plan.ready for plan in self.platforms.values())

    @property
    def ok(self) -> bool:
        return self.ready

    @property
    def hard_blockers(self) -> tuple[PreflightIssue, ...]:
        return tuple(issue for plan in self.platforms.values() for issue in plan.hard_blockers)

    @property
    def warnings(self) -> tuple[PreflightIssue, ...]:
        return tuple(issue for plan in self.platforms.values() for issue in plan.warnings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "ready": self.ready,
            "platforms": {name: plan.to_dict() for name, plan in self.platforms.items()},
            "hard_blockers": [issue.to_dict() for issue in self.hard_blockers],
            "warnings": [issue.to_dict() for issue in self.warnings],
        }

    def to_json(self) -> str:
        """Serialize deterministically for CLI/UI/MCP snapshots and caching."""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class AuthReadinessProvider(Protocol):
    """Optional injected local auth readiness service."""

    def check(self, platform: str, config: XPSTConfig) -> AuthReadiness:
        ...


class PostPreflightService:
    """Build post plans without invoking any upload or mutation path."""

    def __init__(
        self,
        config: XPSTConfig | None = None,
        *,
        video_processor: Any | None = None,
        quota_manager: Any | None = None,
        auth_readiness: AuthReadinessProvider | None = None,
    ) -> None:
        self.config = config or XPSTConfig()
        self.video_processor = video_processor
        self._quota_manager_injected = quota_manager is not None
        self.quota_manager = quota_manager or self._readonly_quota_manager(self.config)
        self.auth_readiness = auth_readiness
        self._processor_initialized = video_processor is not None

    def plan(self, request: PostPlanRequest) -> PostPlanResult:
        """Return one complete plan per requested platform.

        Unknown platforms are represented by a hard-blocked plan rather than
        being dropped.  This is important for deterministic machine clients.
        """
        config = request.config or self.config
        quota_manager = self.quota_manager
        if not self._quota_manager_injected and request.config is not None and request.config is not self.config:
            quota_manager = self._readonly_quota_manager(config)
        platforms: dict[str, PlatformPlan] = {}
        for platform in request.target_platforms:
            platforms[platform] = self._plan_platform(platform, request, config, quota_manager)
        return PostPlanResult(platforms=platforms)

    def _readonly_quota_manager(self, config: XPSTConfig) -> QuotaManager:
        return QuotaManager(config.config_dir, config=config, persist=False)

    def _plan_platform(
        self,
        platform: str,
        request: PostPlanRequest,
        config: XPSTConfig,
        quota_manager: Any,
    ) -> PlatformPlan:
        caption = request.per_platform_captions.get(platform, request.base_caption)
        platform_issues: list[PreflightIssue] = []
        if platform not in _SUPPORTED_PLATFORMS:
            platform_issues.append(
                PreflightIssue(
                    "UNKNOWN_PLATFORM",
                    f"No media preflight specification exists for {platform!r}.",
                    "blocker",
                )
            )
            readiness = self._readiness(platform, config, request.include_readiness, quota_manager)
            return PlatformPlan(platform, caption, (), {}, readiness, (), tuple(platform_issues))

        readiness = self._readiness(platform, config, request.include_readiness, quota_manager)
        if request.include_readiness:
            if not readiness.enabled:
                platform_issues.append(
                    PreflightIssue("PLATFORM_DISABLED", f"{platform} is disabled in local configuration.", "blocker")
                )
            if not readiness.auth.ready:
                platform_issues.append(
                    PreflightIssue("AUTH_NOT_READY", readiness.auth.detail, "blocker")
                )
            if not readiness.quota.ready:
                platform_issues.append(
                    PreflightIssue("QUOTA_NOT_READY", readiness.quota.detail, "blocker")
                )

        media_plans: list[MediaFilePlan] = []
        for raw_path in request.media_paths:
            media_plan = self._plan_media(raw_path, platform, config, request)
            media_plans.append(media_plan)
            platform_issues.extend(media_plan.hard_blockers)

        if not request.media_paths:
            platform_issues.append(PreflightIssue("MEDIA_REQUIRED", "At least one media path is required.", "blocker"))

        limit = _CAPTION_LIMITS.get(platform)
        if limit is not None and len(caption) > limit:
            platform_issues.append(
                PreflightIssue(
                    "CAPTION_TOO_LONG",
                    f"Caption is {len(caption)} characters; {platform} allows {limit}.",
                    "blocker",
                )
            )

        warnings = tuple(issue for item in media_plans for issue in item.warnings)
        constraints = self._constraints(platform, media_plans, caption)
        return PlatformPlan(
            platform=platform,
            effective_caption=caption,
            media=tuple(media_plans),
            constraints=constraints,
            readiness=readiness,
            warnings=warnings,
            hard_blockers=tuple(platform_issues),
        )

    def _plan_media(
        self,
        raw_path: Path | str,
        platform: str,
        config: XPSTConfig,
        request: PostPlanRequest,
    ) -> MediaFilePlan:
        display_path = str(raw_path)
        if _is_url(display_path):
            if platform != "threads":
                issue = PreflightIssue(
                    "REMOTE_MEDIA_UNSUPPORTED",
                    "Only a local media path is supported for this platform.",
                    "blocker",
                    display_path,
                )
                return MediaFilePlan(display_path, "remote_url", None, None, None, None, _empty_media_spec(display_path, platform), None, None, None, (), (issue,))
            warning = PreflightIssue(
                "MEDIA_REMOTE_NOT_PROBED",
                "Remote media was not fetched or probed; no network call is made during planning.",
                "warning",
                display_path,
            )
            return MediaFilePlan(display_path, "remote_url", None, None, None, None, _empty_media_spec(display_path, platform), None, None, None, (warning,), ())

        path = Path(raw_path).expanduser()
        try:
            exists = path.exists()
            is_file = path.is_file() if exists else False
        except OSError as exc:
            exists = False
            is_file = False
            stat_error = PreflightIssue("MEDIA_STAT_FAILED", f"Could not inspect media path: {exc}", "blocker", display_path)
            return MediaFilePlan(display_path, "unavailable", exists, is_file, None, None, _empty_media_spec(display_path, platform), None, None, None, (), (stat_error,))

        if not exists:
            issue = PreflightIssue("MEDIA_NOT_FOUND", f"Media path does not exist: {display_path}", "blocker", display_path)
            return MediaFilePlan(display_path, "missing", False, False, None, None, _empty_media_spec(display_path, platform), None, None, None, (), (issue,))
        if not is_file:
            issue = PreflightIssue("MEDIA_DIRECTORY", f"Media path is not a regular file: {display_path}", "blocker", display_path)
            return MediaFilePlan(display_path, "directory", True, False, None, None, _empty_media_spec(display_path, platform), None, None, None, (), (issue,))

        size_bytes = path.stat().st_size
        if size_bytes == 0:
            issue = PreflightIssue("MEDIA_EMPTY", f"Media file is empty: {display_path}", "blocker", display_path)
            return MediaFilePlan(display_path, "file", True, True, size_bytes, None, _empty_media_spec(display_path, platform), None, None, None, (), (issue,))

        warnings: list[PreflightIssue] = []
        blockers: list[PreflightIssue] = []
        report = verify_media(path, platform, check_loudness=request.check_loudness)
        for check in report.checks:
            issue = self._issue_from_check(check, display_path)
            if issue is None:
                continue
            if check.status == "error":
                blockers.append(issue)
            elif check.status == "warn":
                warnings.append(issue)

        if platform == "threads":
            blockers.append(
                PreflightIssue(
                    "THREADS_NEEDS_URL",
                    "Meta Threads requires a publicly reachable video URL; local files are unavailable to the uploader.",
                    "blocker",
                    display_path,
                )
            )

        duration, aspect_ratio = _probe_dimensions(report)
        transform = None
        if request.include_transform:
            transform = self._transform(path, platform, config)

        return MediaFilePlan(
            path=display_path,
            file_type="file",
            exists=True,
            is_file=True,
            size_bytes=size_bytes,
            ffprobe=report.probe,
            media_spec=report.to_dict(include_probe=True),
            duration_seconds=duration,
            aspect_ratio=aspect_ratio,
            transform=transform,
            warnings=tuple(warnings),
            hard_blockers=tuple(blockers),
        )

    @staticmethod
    def _issue_from_check(check: Check, media_path: str) -> PreflightIssue | None:
        if check.status == "error":
            code = f"MEDIA_SPEC_{check.name.upper()}"
            severity = "blocker"
        elif check.status == "warn":
            code = "MEDIA_PROBE_FAILED" if check.name == "probe" else f"MEDIA_SPEC_{check.name.upper()}"
            severity = "warning"
        else:
            return None
        return PreflightIssue(code, check.detail, severity, media_path)

    def _transform(self, path: Path, platform: str, config: XPSTConfig) -> TransformPlan:
        processor = self._get_processor()
        encoding = _encoding_config(config, platform)
        if processor is None:
            plan = TransformPlan(platform=platform, action="transcode")
            plan.reasons.append("ffmpeg unavailable — compliance not verified")
            return plan
        return plan_transform(path, platform, encoding, processor)

    def _get_processor(self) -> Any | None:
        if self._processor_initialized:
            return self.video_processor
        self._processor_initialized = True
        try:
            self.video_processor = VideoProcessor()
        except Exception:
            self.video_processor = None
        return self.video_processor

    def _readiness(
        self,
        platform: str,
        config: XPSTConfig,
        enabled: bool,
        quota_manager: Any,
    ) -> PlatformReadiness:
        if not enabled:
            return PlatformReadiness(True, AuthReadiness(True, "unchecked", "Readiness checks disabled."), QuotaReadiness(True, {"daily": None, "hourly": None}, "Readiness checks disabled."))

        account = getattr(config, platform, None)
        is_enabled = bool(account is not None and getattr(account, "enabled", False))
        auth = self._auth(platform, config)
        remaining: dict[str, int | None] = {"daily": None, "hourly": None}
        quota_ready = True
        quota_detail = "No quota limit is configured."
        if quota_manager is not None and platform in _SUPPORTED_PLATFORMS:
            try:
                peek_can_upload = getattr(quota_manager, "peek_can_upload", None)
                peek_remaining = getattr(quota_manager, "peek_remaining", None)
                can_upload = bool(
                    peek_can_upload(platform)
                    if callable(peek_can_upload)
                    else quota_manager.can_upload(platform)
                )
                raw_remaining: Any = (
                    peek_remaining(platform)
                    if callable(peek_remaining)
                    else quota_manager.get_remaining(platform)
                )
                remaining = {
                    "daily": raw_remaining.get("daily"),
                    "hourly": raw_remaining.get("hourly"),
                }
                quota_ready = can_upload and all(value is None or value > 0 for value in remaining.values())
                quota_detail = "Quota available." if quota_ready else f"Quota exhausted: {remaining}."
            except Exception as exc:  # noqa: BLE001 - readiness must be reportable
                quota_ready = False
                quota_detail = f"Quota readiness unavailable: {exc}"
        return PlatformReadiness(is_enabled, auth, QuotaReadiness(quota_ready, remaining, quota_detail))

    def _auth(self, platform: str, config: XPSTConfig) -> AuthReadiness:
        if self.auth_readiness is not None:
            return self.auth_readiness.check(platform, config)
        return _local_auth_readiness(platform, config)

    @staticmethod
    def _constraints(platform: str, media: Sequence[MediaFilePlan], caption: str) -> dict[str, Any]:
        spec = PLATFORM_SPECS[platform]
        observed_duration = next((item.duration_seconds for item in media if item.duration_seconds is not None), None)
        observed_aspect = next((item.aspect_ratio for item in media if item.aspect_ratio is not None), None)
        observed_size = next((item.size_bytes for item in media if item.size_bytes is not None), None)
        return {
            "duration": {
                "observed_seconds": observed_duration,
                "max_seconds": spec.duration_cap_s,
            },
            "aspect": {
                "observed_ratio": observed_aspect,
                "constraint": "orientation-preserving; no fixed aspect ratio",
                "max_long_edge": spec.long_edge,
            },
            "size": {
                "observed_bytes": observed_size,
                "max_bytes": spec.file_size_cap_mb * 1024 * 1024 if spec.file_size_cap_mb is not None else None,
            },
            "containers": list(spec.containers),
            "caption": {
                "observed_characters": len(caption),
                "max_characters": _CAPTION_LIMITS.get(platform),
            },
        }


def build_post_plan(
    media_paths: Sequence[Path | str],
    target_platforms: Sequence[str],
    *,
    base_caption: str = "",
    per_platform_captions: Mapping[str, str] | None = None,
    config: XPSTConfig | None = None,
    **options: Any,
) -> PostPlanResult:
    """Functional facade for CLI/UI/MCP clients that do not need a class."""
    request = PostPlanRequest(
        media_paths=media_paths,
        target_platforms=target_platforms,
        base_caption=base_caption,
        per_platform_captions=per_platform_captions or {},
        config=config,
        **options,
    )
    return PostPreflightService(config).plan(request)


def _is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _empty_media_spec(path: str, platform: str) -> dict[str, Any]:
    """Keep the media-spec shape stable when probing is not possible."""
    return {"path": path, "platform": platform, "ok": False, "checks": [], "probe": None}


def _probe_dimensions(report: MediaReport) -> tuple[float | None, float | None]:
    if not report.probe:
        return None, None
    fmt = report.probe.get("format", {})
    try:
        duration = float(fmt.get("duration")) if fmt.get("duration") is not None else None
    except (TypeError, ValueError):
        duration = None
    video = next((stream for stream in report.probe.get("streams", []) if stream.get("codec_type") == "video"), None)
    if not video:
        return duration, None
    try:
        width = float(video.get("width"))
        height = float(video.get("height"))
        aspect = width / height if width > 0 and height > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        aspect = None
    return duration, aspect


def _encoding_config(config: XPSTConfig, platform: str) -> EncodingConfig:
    profile = "instagram" if platform == "threads" else platform
    encoding = getattr(config.video, f"encoding_{profile}", None)
    if encoding is None:
        return EncodingConfig()
    return encoding


def _local_auth_readiness(platform: str, config: XPSTConfig) -> AuthReadiness:
    account = getattr(config, platform, None)
    if account is None:
        return AuthReadiness(False, "unsupported", f"No local auth configuration exists for {platform}.")

    if platform == "youtube":
        ready = bool(account.token_file and Path(account.token_file).expanduser().is_file())
        return AuthReadiness(ready, "ready" if ready else "missing", "YouTube token file exists." if ready else "YouTube OAuth token file is missing.", "oauth")
    if platform == "x":
        if account.auth_mode == "api_v2":
            ready = bool(account.access_token)
            detail = "X API access token is configured." if ready else "X API access token is missing."
            return AuthReadiness(ready, "ready" if ready else "missing", detail, "oauth")
        ready = bool(account.cookies_file and Path(account.cookies_file).expanduser().is_file())
        return AuthReadiness(ready, "ready" if ready else "missing", "X cookie file exists." if ready else "X cookie file is missing.", "cookies")
    if platform == "instagram":
        if account.auth_mode == "graph_api":
            ready = bool(account.graph_access_token and account.graph_ig_user_id)
            detail = "Instagram Graph credentials are configured." if ready else "Instagram Graph credentials are missing."
            return AuthReadiness(ready, "ready" if ready else "missing", detail, "oauth")
        ready = bool(account.session_file and Path(account.session_file).expanduser().is_file())
        return AuthReadiness(ready, "ready" if ready else "missing", "Instagram session file exists." if ready else "Instagram session file is missing.", "session")
    if platform == "tiktok":
        ready = bool(account.access_token)
        return AuthReadiness(ready, "ready" if ready else "missing", "TikTok access token is configured." if ready else "TikTok access token is missing.", "oauth")
    if platform == "threads":
        ready = bool(account.graph_access_token and account.threads_user_id)
        detail = "Threads credentials are configured." if ready else "Threads access token and user ID are missing."
        return AuthReadiness(ready, "ready" if ready else "missing", detail, "oauth")
    return AuthReadiness(False, "unsupported", f"No local auth readiness rule exists for {platform}.")


__all__ = [
    "AuthReadiness",
    "AuthReadinessProvider",
    "MediaFilePlan",
    "PlatformPlan",
    "PlatformReadiness",
    "PostPlanRequest",
    "PostPlanResult",
    "PostPreflightService",
    "PreflightIssue",
    "QuotaReadiness",
    "build_post_plan",
]
