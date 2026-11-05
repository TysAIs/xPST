"""Capability-driven route planning: who gets this, in what state, and why.

This module is the single implementation of two settled product requirements
(``docs/PROPOSAL-PRODUCT-RESET.md`` §4 and §5):

* **R3 — every platform is optional.** A post targets an explicit set, or the
  *format route* ∩ *connected accounts*. There is no implied "all platforms":
  ``--all`` is an explicit request for every connected account that accepts the
  format, and an unconnected destination is ``skipped_unconnected`` (neutral),
  never a failure.
* **R4 — format-aware routing is the core value.** A plan is resolved from the
  capability registry (``xpst capabilities`` → :mod:`xpst.content` +
  :mod:`xpst.media.specs`) and is shown before anything is published. Out of
  envelope input is refused with the envelope in the message; nothing is
  truncated to fit.

The plan object is what the CLI, the MCP server, the HTTP API and the scheduler
consume, so no surface can resolve a different destination set.

Side effects: this module reads local configuration and (optionally) probes
local media through :class:`~xpst.services.post_preflight.PostPreflightService`.
It never uploads, never contacts a network endpoint, never writes state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from xpst.content import (
    UnknownContentTypeError,
    coerce_content_type,
    content_profile,
    destination_envelope,
    format_route,
    infer_content_type,
    text_limit,
)
from xpst.services.post_preflight import NO_DESTINATIONS_CODE, NO_DESTINATIONS_MESSAGE

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from xpst.config import XPSTConfig
    from xpst.services.post_preflight import PreflightIssue

# ── Canonical destination states (R3 rule 4) ────────────────────────────────
# These are the only spellings any surface may emit. ``skipped_unconnected`` is
# deliberately NOT a failure: a user who has not connected an account has not
# broken anything.
POSTED = "posted"
SKIPPED_UNCONNECTED = "skipped_unconnected"
UNSUPPORTED_FORMAT = "unsupported_format"
REFUSED_BY_COMMUNITY = "refused_by_community"
FAILED = "failed"

DESTINATION_STATES: tuple[str, ...] = (
    POSTED,
    SKIPPED_UNCONNECTED,
    UNSUPPORTED_FORMAT,
    REFUSED_BY_COMMUNITY,
    FAILED,
)

#: States that mean "nothing was attempted here, and that is not an error".
NEUTRAL_STATES: frozenset[str] = frozenset({SKIPPED_UNCONNECTED})
#: States where the destination was evaluated and refused the content.
REFUSAL_STATES: frozenset[str] = frozenset({UNSUPPORTED_FORMAT, REFUSED_BY_COMMUNITY})
#: A real upload failure.
FAILURE_STATES: frozenset[str] = frozenset({FAILED})


class DestinationState(str, Enum):
    """Typed spelling of the states above (same string values)."""

    POSTED = POSTED
    SKIPPED_UNCONNECTED = SKIPPED_UNCONNECTED
    UNSUPPORTED_FORMAT = UNSUPPORTED_FORMAT
    REFUSED_BY_COMMUNITY = REFUSED_BY_COMMUNITY
    FAILED = FAILED


# ── Canonical refusals ──────────────────────────────────────────────────────
NO_CONNECTED_ACCOUNTS_CODE = "NO_CONNECTED_ACCOUNTS"
NO_ROUTE_FOR_FORMAT_CODE = "NO_ROUTE_FOR_FORMAT"
CONNECT_COMMAND = "xpst connect {platform}"


def connect_command(platform: str | None = None) -> str:
    """The command that fixes an unconnected destination, named in messages."""
    return (
        f"`xpst connect {platform}`"
        if platform
        else "`xpst connect youtube`, `xpst connect x`, …"
    )


def no_connected_accounts_message(connected_hint: str = "") -> str:
    """One actionable sentence: nothing is connected, here is the command."""
    return (
        "No platform account is connected, so there is nowhere to publish. "
        f"Connect one with {connect_command()} — nothing is connected by default."
    )


def no_route_for_format_message(content_type: str, connected: Sequence[str]) -> str:
    """The format route is empty after intersecting with connected accounts."""
    names = ", ".join(connected) if connected else "none"
    return (
        f"No connected account accepts {content_type} posts (connected: {names}). "
        f"Connect one with {connect_command()} that supports {content_type}, or "
        "pick the destinations explicitly with --platforms."
    )


def _human_bytes(value: int | None) -> str | None:
    """Byte ceiling in the largest whole unit (MB/GB/TB), for messages."""
    if not value:
        return None
    for unit, size in (("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2)):
        if value >= size:
            amount = value / size
            return f"{amount:.0f} {unit}" if amount >= 10 else f"{amount:.1f} {unit}"
    return f"{value} B"


def envelope_summary(platform: str, content_type: str | None = None) -> str:
    """One-line, human-readable envelope for a destination.

    Used verbatim in refusal messages ("the message carries the envelope", R4
    requirement 4) so a user learns the limit from the refusal instead of
    guessing, and in the plan output.
    """
    envelope = destination_envelope(platform)
    if envelope is None:
        return f"{platform}: unknown destination (no declared envelope)"
    parts: list[str] = []
    wanted = str(content_type or "").strip().lower()
    video = envelope.get("video") or {}
    image = envelope.get("image") or {}
    # A destination asked for a format it does not take still gets its real
    # envelope shown (that is the point of refusing with the envelope), so a
    # text post aimed at a video-only destination names the video limits.
    show_video = bool(video) and (wanted in {"video", "carousel"} or wanted not in {"image"})
    if show_video:
        caps: list[str] = []
        if video.get("max_duration_seconds"):
            caps.append(f"≤{video['max_duration_seconds']}s")
        if video.get("containers"):
            caps.append("/".join(str(item).upper().lstrip(".") for item in video["containers"]))
        if video.get("video_codec"):
            caps.append(str(video["video_codec"]))
        if video.get("max_bytes"):
            size = _human_bytes(int(video["max_bytes"]))
            if size:
                caps.append(f"≤{size}")
        parts.append("video " + ", ".join(caps))
    if wanted == "image" and image:
        caps = []
        if image.get("containers"):
            caps.append("/".join(str(item).upper().lstrip(".") for item in image["containers"]))
        if image.get("max_bytes"):
            size = _human_bytes(int(image["max_bytes"]))
            if size:
                caps.append(f"≤{size}")
        if image.get("aspect_min") and image.get("aspect_max"):
            caps.append(f"aspect {image['aspect_min']}:1–{image['aspect_max']}:1")
        parts.append("image " + ", ".join(caps))
    caption = (envelope.get("caption") or {}).get("max_characters")
    if caption is not None:
        parts.append(f"caption ≤{caption} chars")
    if not parts:
        return f"{platform}: {', '.join(envelope['content_types']['implemented']) or 'nothing'}"
    return f"{platform}: " + ", ".join(parts)


# ── Target resolution (R3) ──────────────────────────────────────────────────


@dataclass(frozen=True)
class TargetResolution:
    """The destination set a request resolves to, and where it came from."""

    targets: tuple[str, ...]
    source: str  # "explicit" | "all" | "format_route"
    content_type: str | None = None
    errors: tuple[dict[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.targets) and not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "targets": list(self.targets),
            "source": self.source,
            "content_type": self.content_type,
            "errors": [dict(error) for error in self.errors],
        }


def normalize_platforms(requested: Sequence[str] | None) -> tuple[str, ...]:
    """Normalize an explicit target list (lowercase, deduped, order preserved)."""
    seen: dict[str, None] = {}
    for platform in requested or ():
        name = str(platform).strip().lower()
        if name:
            seen.setdefault(name, None)
    return tuple(seen)


def connected_destinations(config: XPSTConfig) -> tuple[str, ...]:
    """Publishing destinations enabled (connected) in local configuration.

    ``enabled`` is xPST's "this account is connected and may publish" flag. A
    destination that is enabled but has no usable credential is still listed
    here; the plan reports it as ``skipped_unconnected`` with the
    ``AUTH_NOT_READY`` reason rather than pretending it will publish.
    """
    from xpst.content import PUBLISH_DESTINATIONS

    connected: list[str] = []
    for platform in PUBLISH_DESTINATIONS:
        account = getattr(config, platform, None)
        if account is not None and bool(getattr(account, "enabled", False)):
            connected.append(platform)
    return tuple(connected)


def resolve_targets(
    config: XPSTConfig,
    requested: Sequence[str] | None = None,
    *,
    all_connected: bool = False,
    content_type: str | None = None,
) -> TargetResolution:
    """Resolve the destination set for a post, with no implied set.

    * ``requested`` (an explicit ``--platforms`` / ``platforms`` argument) wins,
      including names that are not connected — the plan then marks those
      ``skipped_unconnected`` instead of dropping them.
    * ``all_connected=True`` (the explicit ``--all``) means every connected
      account that accepts this format.
    * otherwise the route is ``format route ∩ connected accounts``.

    Zero connected accounts produces ONE actionable error naming the connect
    command (R3 rule 5); so does a format no connected account accepts.
    """
    content_type_value = _normalize_content_type(content_type)
    explicit = normalize_platforms(requested)
    if explicit:
        return TargetResolution(
            targets=explicit, source="explicit", content_type=content_type_value
        )
    if requested is not None:
        # A list was given but held no usable name (`--platforms ","`): that is
        # a malformed request, not "nothing is connected".
        return TargetResolution(
            targets=(),
            source="explicit",
            content_type=content_type_value,
            errors=(
                {
                    "code": NO_DESTINATIONS_CODE,
                    "message": NO_DESTINATIONS_MESSAGE,
                },
            ),
        )

    connected = connected_destinations(config)
    if not connected:
        return TargetResolution(
            targets=(),
            source="explicit" if requested is not None else "format_route",
            content_type=content_type_value,
            errors=(
                {
                    "code": NO_CONNECTED_ACCOUNTS_CODE,
                    "message": no_connected_accounts_message(),
                },
            ),
        )

    route = (
        format_route(content_type_value)
        if content_type_value is not None
        else tuple(connected)
    )
    targets = tuple(platform for platform in route if platform in connected)
    if not targets:
        return TargetResolution(
            targets=(),
            source="all" if all_connected else "format_route",
            content_type=content_type_value,
            errors=(
                {
                    "code": NO_ROUTE_FOR_FORMAT_CODE,
                    "message": no_route_for_format_message(
                        content_type_value or "this", connected
                    ),
                },
            ),
        )
    return TargetResolution(
        targets=targets,
        source="all" if all_connected else "format_route",
        content_type=content_type_value,
    )


def _normalize_content_type(content_type: Any) -> str | None:
    raw = str(content_type or "").strip().lower()
    if not raw or raw == "auto":
        return None
    try:
        return coerce_content_type(raw).value
    except UnknownContentTypeError:
        return raw


def detect_content_type(
    media_paths: Sequence[str] | None = None,
    *,
    content_type: str | None = None,
) -> str:
    """Classify the requested format.

    A caller-stated type wins (``--content-type``), an explicit ``--format``
    wins over inference, and otherwise the media list decides exactly as the
    content contract does. ``"auto"`` (and an absent value) means "classify it".
    """
    raw = str(content_type or "").strip().lower()
    if raw and raw != "auto":
        try:
            return coerce_content_type(raw).value
        except UnknownContentTypeError:
            return raw
    items = [str(item) for item in (media_paths or ()) if str(item).strip()]
    return infer_content_type(items).value


# ── The plan object ─────────────────────────────────────────────────────────


def _state_for_blocker(issue: PreflightIssue) -> str:
    """Map one preflight blocker to a destination state.

    ``skipped_unconnected`` covers "not connected" (disabled, or no usable
    credential). ``unsupported_format`` covers every refusal about the content
    or the envelope. Everything else is a genuine ``failed`` plan.
    """
    return _state_for_code(str(getattr(issue, "code", "") or ""))


def _state_for_code(code: str) -> str:
    """State for a stable preflight blocker code (see :func:`_state_for_blocker`)."""
    upper = str(code or "").upper()
    if upper in {"PLATFORM_DISABLED", "AUTH_NOT_READY"}:
        return SKIPPED_UNCONNECTED
    if upper in {
        "CAPTION_TOO_LONG",
        "MEDIA_MODALITY_UNSUPPORTED",
        "REMOTE_MEDIA_UNSUPPORTED",
        "MEDIA_TRANSPORT",
        "THREADS_NEEDS_URL",
    } or upper.startswith("MEDIA_SPEC_"):
        return UNSUPPORTED_FORMAT
    return FAILED


@dataclass
class RouteDestination:
    """One destination's verdict in a resolved route plan."""

    platform: str
    state: str
    reason: str
    content_type: str | None = None
    connected: bool = False
    envelope: dict[str, Any] | None = None
    caption: str = ""
    constraints: dict[str, Any] = field(default_factory=dict)
    blockers: tuple[dict[str, str], ...] = ()
    warnings: tuple[dict[str, str], ...] = ()

    @property
    def will_post(self) -> bool:
        return self.state == POSTED

    @property
    def is_failure(self) -> bool:
        return self.state in FAILURE_STATES

    @property
    def connect_command(self) -> str | None:
        return None if self.connected else CONNECT_COMMAND.format(platform=self.platform)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "state": self.state,
            "will_post": self.will_post,
            "is_failure": self.is_failure,
            "reason": self.reason,
            "content_type": self.content_type,
            "connected": self.connected,
            "connect_command": self.connect_command,
            "envelope": self.envelope,
            "caption": self.caption,
            "caption_length": len(self.caption),
            "constraints": self.constraints,
            "blockers": [dict(item) for item in self.blockers],
            "warnings": [dict(item) for item in self.warnings],
        }


@dataclass
class RoutePlan:
    """A resolved, per-destination plan — the object post/schedule consume."""

    content_type: str | None
    source: str
    destinations: tuple[RouteDestination, ...]
    errors: tuple[dict[str, str], ...] = ()
    request_blockers: tuple[dict[str, str], ...] = ()

    @property
    def posting_destinations(self) -> tuple[str, ...]:
        """The destinations a live run should attempt, in plan order."""
        return tuple(item.platform for item in self.destinations if item.will_post)

    @property
    def skipped_unconnected(self) -> tuple[str, ...]:
        return tuple(
            item.platform for item in self.destinations if item.state == SKIPPED_UNCONNECTED
        )

    @property
    def unsupported(self) -> tuple[str, ...]:
        return tuple(
            item.platform for item in self.destinations if item.state == UNSUPPORTED_FORMAT
        )

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(item.platform for item in self.destinations if item.state == FAILED)

    @property
    def actionable_error(self) -> dict[str, str] | None:
        """The one error a caller must act on (no destinations at all)."""
        if self.errors:
            return dict(self.errors[0])
        if self.request_blockers:
            return dict(self.request_blockers[0])
        return None

    @property
    def ready(self) -> bool:
        """Whether at least one destination will be posted to."""
        return bool(self.posting_destinations)

    @property
    def ok(self) -> bool:
        return self.ready and not self.errors

    def destination(self, platform: str) -> RouteDestination | None:
        name = str(platform).strip().lower()
        return next((item for item in self.destinations if item.platform == name), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_type": self.content_type,
            "source": self.source,
            "ready": self.ready,
            "ok": self.ok,
            "posting_destinations": list(self.posting_destinations),
            "skipped_unconnected": list(self.skipped_unconnected),
            "unsupported_format": list(self.unsupported),
            "failed": list(self.failed),
            "destinations": [item.to_dict() for item in self.destinations],
            "errors": [dict(error) for error in self.errors],
            "request_blockers": [dict(item) for item in self.request_blockers],
            "error": self.actionable_error,
            "network_calls": False,
        }

    def to_json(self) -> str:
        import json

        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_route_plan(
    config: XPSTConfig,
    *,
    media_paths: Sequence[str] = (),
    caption: str = "",
    per_platform_captions: Mapping[str, str] | None = None,
    content_type: str | None = None,
    requested: Sequence[str] | None = None,
    all_connected: bool = False,
    include_transform: bool = False,
    include_readiness: bool = True,
    probe_media: bool = True,
) -> RoutePlan:
    """Resolve the full plan for one request without uploading anything.

    Args:
        config: local configuration (defines which accounts are connected).
        media_paths: media the post would carry.
        caption: shared caption text.
        per_platform_captions: per-destination overrides (validated per
            destination, so an override is length-checked against its own
            destination, not the shortest one in the route).
        content_type: explicit format, or ``None``/``"auto"`` to classify.
        requested: explicit destination list.
        all_connected: the explicit "all connected accounts that accept this
            format" request. It never adds an account that is not connected.
        include_transform: include the ffmpeg transform decision (needs ffmpeg).
        include_readiness: check enabled/auth/quota (local only).
        probe_media: probe local media (ffprobe). ``False`` skips the probe and
            reports the format/envelope verdict only.
    """
    from xpst.services.post_preflight import PostPlanRequest, PostPreflightService

    detected = detect_content_type(media_paths, content_type=content_type)
    resolution = resolve_targets(
        config, requested, all_connected=all_connected, content_type=detected
    )
    if not resolution.targets:
        return RoutePlan(
            content_type=detected,
            source=resolution.source,
            destinations=(),
            errors=resolution.errors,
        )

    overrides = {
        str(name).strip().lower(): value for name, value in (per_platform_captions or {}).items()
    }
    plan_media = list(media_paths) if probe_media else []
    service = PostPreflightService(config)
    plan = service.plan(
        PostPlanRequest(
            media_paths=plan_media,
            target_platforms=resolution.targets,
            base_caption=caption,
            per_platform_captions=overrides,
            content_type=detected,
            config=config,
            include_transform=include_transform,
            include_readiness=include_readiness,
            # A probe-less plan must not claim "media required" for a request
            # that carried files; the media verdict is simply unprobed.
            check_loudness=False,
        )
    )

    destinations: list[RouteDestination] = []
    for platform in resolution.targets:
        platform_plan = plan.platforms.get(platform)
        profile = content_profile(platform)
        connected = bool(
            profile is not None
            and getattr(getattr(config, platform, None), "enabled", False)
        )
        envelope = destination_envelope(platform)
        effective_caption = overrides.get(platform, caption)
        blockers = tuple(
            {"code": issue.code, "message": issue.message}
            for issue in (platform_plan.hard_blockers if platform_plan else ())
            if not (not probe_media and issue.code == "MEDIA_REQUIRED")
            # "Connected" is the per-account opt-in flag (handled above); an
            # enabled account whose credential looks missing is still attempted
            # so the uploader reports the authoritative auth error (exit 3).
            # Quota is an execution-time gate with its own exit code (4). Both
            # are reported as warnings, never as a skip.
            and issue.code not in {"QUOTA_NOT_READY", "AUTH_NOT_READY"}
        )
        warnings = tuple(
            {"code": issue.code, "message": issue.message}
            for issue in (platform_plan.warnings if platform_plan else ())
        ) + tuple(
            {"code": issue.code, "message": issue.message}
            for issue in (platform_plan.hard_blockers if platform_plan else ())
            if issue.code in {"QUOTA_NOT_READY", "AUTH_NOT_READY"}
        )

        state = POSTED
        reason = "will publish"
        if profile is None:
            state = UNSUPPORTED_FORMAT
            reason = f"{platform} is not a known publishing destination."
        elif not connected:
            state = SKIPPED_UNCONNECTED
            reason = (
                f"{platform} is not connected in local configuration. "
                f"Connect it with {connect_command(platform)}."
            )
        elif not profile.supports(coerce_content_type(detected)):
            state = UNSUPPORTED_FORMAT
            reason = (
                f"{platform} cannot publish {detected} posts "
                f"(supports: {', '.join(sorted(item.value for item in profile.implemented))}). "
                f"Envelope — {envelope_summary(platform, detected)}."
            )
        elif blockers:
            state = _state_for_code(blockers[0]["code"])
            reason = "; ".join(item["message"] for item in blockers)
            caption_refusal = text_refusal(platform, effective_caption)
            if any(item["code"] == "CAPTION_TOO_LONG" for item in blockers) and caption_refusal:
                # Refusal carries the envelope (R4 requirement 4) instead of a
                # bare "too long" the caller has to decode.
                reason = caption_refusal
            if state == SKIPPED_UNCONNECTED:
                reason += f" Connect with {connect_command(platform)}."
            if state == UNSUPPORTED_FORMAT:
                reason = f"{reason} Envelope — {envelope_summary(platform, detected)}."

        destinations.append(
            RouteDestination(
                platform=platform,
                state=state,
                reason=reason,
                content_type=detected,
                connected=connected,
                envelope=envelope,
                caption=effective_caption,
                constraints=dict(platform_plan.constraints) if platform_plan else {},
                blockers=blockers,
                warnings=warnings,
            )
        )

    return RoutePlan(
        content_type=detected,
        source=resolution.source,
        destinations=tuple(destinations),
        errors=resolution.errors,
        request_blockers=tuple(
            {"code": issue.code, "message": issue.message} for issue in plan.request_blockers
        ),
    )


# ── Execution outcomes ──────────────────────────────────────────────────────


def classify_upload_result(platform: str, upload: Any) -> dict[str, Any]:
    """Classify one upload result into the canonical destination states.

    A destination row written by the engine already carries its state in
    ``metadata["state"]``. Legacy rows are classified from their error code, so
    an old caller cannot report a skip as a failure.
    """
    metadata = dict(getattr(upload, "metadata", None) or {})
    success = bool(getattr(upload, "success", False))
    error = str(getattr(upload, "error", None) or "")
    declared = str(metadata.get("state") or "").strip().lower()
    if success:
        state = POSTED
    elif declared in DESTINATION_STATES:
        state = declared
    elif metadata.get("skipped_unconnected"):
        state = SKIPPED_UNCONNECTED
    elif metadata.get("unsupported") or metadata.get("unsupported_format"):
        state = UNSUPPORTED_FORMAT
    elif metadata.get("refused_by_community"):
        state = REFUSED_BY_COMMUNITY
    elif error.startswith("SKIPPED_UNCONNECTED"):
        state = SKIPPED_UNCONNECTED
    elif error.startswith("UNSUPPORTED_FORMAT"):
        state = UNSUPPORTED_FORMAT
    elif error.startswith("REFUSED_BY_COMMUNITY"):
        state = REFUSED_BY_COMMUNITY
    else:
        state = FAILED
    return {
        "platform": platform,
        "state": state,
        "reason": None if success else (
            error or str(metadata.get("reason") or "Upload failed")
        ),
        "is_failure": state in FAILURE_STATES,
        "connect_command": (
            CONNECT_COMMAND.format(platform=platform)
            if state == SKIPPED_UNCONNECTED
            else None
        ),
    }


def destination_states(results: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Per-destination state map for an executed post."""
    return {
        str(platform): classify_upload_result(str(platform), upload)
        for platform, upload in (results or {}).items()
    }


def outcome_state(states: Mapping[str, Mapping[str, Any]] | None) -> str:
    """Aggregate one post's destination states into a single outcome word.

    Returns ``posted`` (at least one published, none failed), ``partial``
    (something published and something failed/refused), ``skipped`` (only
    unconnected destinations — NOT a failure), ``refused`` (evaluated and
    refused, nothing attempted) or ``failed``.
    """
    values = [str(item.get("state")) for item in (states or {}).values()]
    if not values:
        return FAILED
    published = values.count(POSTED)
    failed = sum(1 for value in values if value in FAILURE_STATES)
    refused = sum(1 for value in values if value in REFUSAL_STATES)
    skipped = sum(1 for value in values if value in NEUTRAL_STATES)
    if published:
        return POSTED if not (failed or refused) else "partial"
    if skipped == len(values):
        return "skipped"
    if refused and not failed:
        return "refused"
    return FAILED


def merge_plan_destinations(result: Any, plan: Any) -> Any:
    """Complete a post result with the plan's non-attempted destinations.

    An execution consumes only the plan's ``posting_destinations`` (that is what
    "the plan is what post and schedule consume" means). The destinations the
    plan already decided against still appear in the result, with their
    canonical state — so an unconnected or unsupported destination is never a
    silent drop, and never a fabricated upload failure.

    ``plan`` is a :class:`RoutePlan` or its ``to_dict()`` payload, so a surface
    that already serialized the plan (the HTTP service) can reuse it.
    """
    from xpst.engine import skipped_unconnected_result, unsupported_format_result

    results = getattr(result, "results", None)
    if not isinstance(results, dict):
        return result
    if isinstance(plan, dict):
        destinations = plan.get("destinations") or []
        entries = [
            (
                str(item.get("platform") or ""),
                str(item.get("state") or ""),
                str(item.get("content_type") or "content"),
                str(item.get("reason") or ""),
                str(((item.get("blockers") or [{}])[0] or {}).get("code") or ""),
            )
            for item in destinations
        ]
    else:
        entries = [
            (
                item.platform,
                item.state,
                item.content_type or "content",
                item.reason,
                str(item.blockers[0]["code"]) if item.blockers else "",
            )
            for item in getattr(plan, "destinations", ())
        ]
    for platform, state, content_type, reason, code in entries:
        if not platform or platform in results:
            continue
        if state == SKIPPED_UNCONNECTED:
            results[platform] = skipped_unconnected_result(platform)
        elif state == UNSUPPORTED_FORMAT:
            detail = reason if (not code or reason.startswith(f"{code}:")) else f"{code}: {reason}"
            results[platform] = unsupported_format_result(platform, content_type, detail)
    update_status = getattr(result, "update_status", None)
    if callable(update_status):
        update_status()
    return result


def text_refusal(platform: str, caption: str) -> str | None:
    """The refusal message when a caption exceeds a destination's limit.

    Refusing (not truncating) is the contract: the message names both numbers
    and the destination's caption envelope, so the caller can shorten once
    instead of discovering the limit per platform.
    """
    limit = text_limit(platform)
    if limit is None or len(caption) <= limit:
        return None
    return (
        f"{platform} accepts {limit} characters and this caption is {len(caption)}; "
        f"xPST refuses rather than truncates. Shorten it to {limit} characters "
        f"or use --caption-for {platform}=<text>. Envelope — {envelope_summary(platform, 'text')}."
    )


__all__ = [
    "CONNECT_COMMAND",
    "DESTINATION_STATES",
    "DestinationState",
    "FAILED",
    "FAILURE_STATES",
    "NEUTRAL_STATES",
    "NO_CONNECTED_ACCOUNTS_CODE",
    "NO_ROUTE_FOR_FORMAT_CODE",
    "POSTED",
    "REFUSAL_STATES",
    "REFUSED_BY_COMMUNITY",
    "RouteDestination",
    "RoutePlan",
    "SKIPPED_UNCONNECTED",
    "TargetResolution",
    "UNSUPPORTED_FORMAT",
    "build_route_plan",
    "classify_upload_result",
    "connect_command",
    "connected_destinations",
    "destination_states",
    "detect_content_type",
    "envelope_summary",
    "normalize_platforms",
    "outcome_state",
    "resolve_targets",
    "text_refusal",
]
