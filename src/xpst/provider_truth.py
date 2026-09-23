"""Canonical provider roles, capability state, and live status contract.

This module is deliberately provider-neutral.  It turns the existing live
health probes into one role-aware shape consumed by the CLI, readiness/doctor,
web API, and MCP.  Compatibility fields (``authenticated``,
``session_valid``, ``live_checked``, and ``error``) remain on every provider
entry for existing clients.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xpst.providers import AuthMode, ProviderManifest, ProviderRole, ProviderState

# Public aliases make the contract easy to discover for callers that use the
# shorter names from the product architecture document.
RoleState = ProviderState


@dataclass(frozen=True)
class ProviderDefinition:
    """Static provider identity and the roles it supports."""

    name: str
    display_name: str
    roles: tuple[ProviderRole, ...]
    auth_mode: str
    official_api: bool = False
    docs_url: str | None = None


@dataclass(frozen=True)
class RoleStatus:
    """Status of one provider/role pair.

    ``authenticated``, ``session_valid`` and ``live_checked`` are ``None`` when
    no live probe produced a result for this role.  "Not probed" is not
    "invalid": a surface that emitted ``False`` here would be asserting a fact
    it never checked, which is exactly how the offline catalog used to
    contradict ``xpst auth status``.
    """

    platform: str
    role: ProviderRole
    state: ProviderState
    enabled: bool
    authenticated: bool | None = None
    session_valid: bool | None = None
    auth_mode: str = "unknown"
    live_checked: bool | None = None
    error: str | None = None
    details: Mapping[str, Any] | None = None

    @property
    def ready(self) -> bool:
        """Whether this role can be used now."""
        return self.state is ProviderState.READY

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON representation."""
        return {
            "platform": self.platform,
            "role": self.role.value,
            "state": self.state.value,
            "ready": self.ready,
            "enabled": self.enabled,
            "authenticated": self.authenticated,
            "session_valid": self.session_valid,
            "auth_mode": self.auth_mode,
            "live_checked": self.live_checked,
            "error": self.error,
            "details": dict(self.details or {}),
        }


# ProviderStatus is a compatibility-friendly public name for RoleStatus.
ProviderStatus = RoleStatus

# The facts only a live probe can answer.  Their presence in a raw result
# mapping is what makes a role "probed"; absent means unknown, never False.
LIVE_FACT_FIELDS: tuple[str, ...] = ("authenticated", "session_valid", "live_checked")


SUPPORTED_PROVIDERS: tuple[ProviderDefinition, ...] = (
    ProviderDefinition(
        "youtube",
        "YouTube Shorts",
        (ProviderRole.SOURCE, ProviderRole.VIDEO_DESTINATION, ProviderRole.ANALYTICS),
        "oauth",
        official_api=True,
        docs_url="https://developers.google.com/youtube/v3",
    ),
    ProviderDefinition(
        "x",
        "X",
        (ProviderRole.SOURCE, ProviderRole.VIDEO_DESTINATION, ProviderRole.ANALYTICS),
        "cookies",
        docs_url="https://developer.x.com/en/docs/x-api",
    ),
    ProviderDefinition(
        "instagram",
        "Instagram Reels",
        (ProviderRole.SOURCE, ProviderRole.VIDEO_DESTINATION, ProviderRole.ANALYTICS),
        "session",
        docs_url="https://developers.facebook.com/docs/instagram-api",
    ),
    ProviderDefinition(
        "tiktok",
        "TikTok",
        (ProviderRole.SOURCE, ProviderRole.VIDEO_DESTINATION, ProviderRole.ANALYTICS),
        "source_only",
        official_api=True,
        docs_url="https://developers.tiktok.com/doc/content-posting-api",
    ),
    ProviderDefinition(
        "threads",
        "Threads",
        (ProviderRole.VIDEO_DESTINATION, ProviderRole.ANALYTICS),
        "oauth",
        official_api=True,
        docs_url="https://developers.facebook.com/docs/threads",
    ),
    ProviderDefinition(
        "messenger",
        "Messenger",
        (ProviderRole.MESSAGING,),
        "oauth",
        official_api=True,
        docs_url="https://developers.facebook.com/docs/messenger-platform",
    ),
    ProviderDefinition("local", "Local files", (ProviderRole.SOURCE,), "local"),
)

_DEFINITIONS = {item.name: item for item in SUPPORTED_PROVIDERS}


def supported_provider_names() -> tuple[str, ...]:
    """Return every built-in provider represented by canonical truth."""
    return tuple(item.name for item in SUPPORTED_PROVIDERS)


def provider_definition(name: str) -> ProviderDefinition:
    """Return a provider definition, raising for an unknown name."""
    return _DEFINITIONS[name]


def _config_for(config: Any, name: str) -> Any:
    return getattr(config, name, None)


def _enabled(config: Any, name: str) -> bool:
    if name == "local":
        return True
    account = _config_for(config, name)
    return bool(getattr(account, "enabled", False))


def _auth_mode(config: Any, name: str, raw: Mapping[str, Any]) -> str:
    if raw.get("auth_mode"):
        return str(raw["auth_mode"])
    account = _config_for(config, name)
    configured = str(getattr(account, "auth_mode", "") or "")
    if configured:
        return configured
    if name == "tiktok":
        account = _config_for(config, name)
        if getattr(account, "client_key", "") and (
            getattr(account, "access_token", "") or getattr(account, "refresh_token", "")
        ):
            return "content_posting_api"
        return "source_only"
    return _DEFINITIONS[name].auth_mode


def _path_exists(value: Any) -> bool:
    return bool(value and Path(str(value)).expanduser().exists())


def _configured(config: Any, name: str, role: ProviderRole, raw: Mapping[str, Any]) -> bool:
    """Determine whether the inputs for one role have been supplied.

    This is only used when a live probe says ``False`` or has not run.  A live
    success always wins, and ``credentials_stored`` lets encrypted keyring
    credentials count even when no credential path is present.
    """
    if raw.get("credentials_stored") or raw.get("configured"):
        return True

    account = _config_for(config, name)
    if account is None:
        return False
    if name == "local":
        return _path_exists(getattr(account, "path", ""))
    if name == "tiktok":
        if role is ProviderRole.SOURCE:
            return bool(
                getattr(account, "username", "")
                or getattr(account, "cookies_from_browser", False)
                or _path_exists(getattr(account, "cookies_file", ""))
            )
        if role in (ProviderRole.VIDEO_DESTINATION, ProviderRole.ANALYTICS):
            return bool(
                getattr(account, "client_key", "")
                and (
                    getattr(account, "access_token", "")
                    or getattr(account, "refresh_token", "")
                )
            )
    if name == "x":
        if getattr(account, "auth_mode", "cookies") == "api_v2":
            return bool(
                getattr(account, "bearer_token", "")
                or all(
                    getattr(account, field, "")
                    for field in ("api_key", "api_secret", "access_token", "access_token_secret")
                )
            )
        return _path_exists(getattr(account, "cookies_file", ""))
    if name == "instagram":
        if getattr(account, "auth_mode", "session") == "graph_api":
            # Legacy installs may have only a session file while the default
            # graph mode is selected.  It is configured data, but a live probe
            # still decides whether the configured mode is actually healthy.
            return bool(
                getattr(account, "graph_access_token", "")
                or _path_exists(getattr(account, "session_file", ""))
            )
        return _path_exists(getattr(account, "session_file", ""))
    if name == "youtube":
        return _path_exists(getattr(account, "token_file", ""))
    if name == "threads":
        return bool(
            getattr(account, "graph_access_token", "")
            and getattr(account, "threads_user_id", "")
        )
    if name == "messenger":
        return bool(getattr(account, "page_access_token", ""))
    return False


def _raw_for_role(name: str, role: ProviderRole, raw: Mapping[str, Any]) -> Mapping[str, Any]:
    """Select a role-specific live result, preserving legacy flat results."""
    if name == "tiktok":
        if role is ProviderRole.SOURCE and isinstance(raw.get("source_check"), Mapping):
            return raw["source_check"]
        if role is ProviderRole.VIDEO_DESTINATION and isinstance(raw.get("destination_check"), Mapping):
            return raw["destination_check"]
        if role is ProviderRole.ANALYTICS and isinstance(raw.get("analytics_check"), Mapping):
            return raw["analytics_check"]
    role_checks = raw.get("role_checks")
    if isinstance(role_checks, Mapping) and isinstance(role_checks.get(role.value), Mapping):
        return role_checks[role.value]
    return raw


def _state_for(
    config: Any,
    name: str,
    role: ProviderRole,
    raw: Mapping[str, Any],
    enabled: bool,
) -> ProviderState:
    if not enabled:
        return ProviderState.DISABLED
    if name == "tiktok" and role is ProviderRole.VIDEO_DESTINATION:
        account = _config_for(config, name)
        if bool(getattr(account, "sandbox", False)) and _configured(config, name, role, raw):
            return ProviderState.BLOCKED_EXTERNAL_REVIEW
        error_text = str(raw.get("error", "")).lower()
        if "external review" in error_text or "review required" in error_text:
            return ProviderState.BLOCKED_EXTERNAL_REVIEW
    if bool(raw.get("authenticated")) and bool(raw.get("session_valid", True)):
        return ProviderState.READY
    if str(raw.get("error", "")).lower() == "disabled":
        return ProviderState.DISABLED
    if not raw.get("live_checked", False):
        return ProviderState.READY if _configured(config, name, role, raw) else ProviderState.UNCONFIGURED
    if not _configured(config, name, role, raw):
        return ProviderState.UNCONFIGURED
    return ProviderState.DEGRADED


def _status_for_role(
    config: Any,
    name: str,
    role: ProviderRole,
    raw: Mapping[str, Any],
) -> RoleStatus:
    role_raw = _raw_for_role(name, role, raw)
    enabled = _enabled(config, name)
    state = _state_for(config, name, role, role_raw, enabled)
    details = role_raw.get("details")
    if not isinstance(details, Mapping):
        details = {}
    # A live result is any mapping that carries a live fact; the offline path
    # passes ``{}``.  Unprobed facts stay ``None`` (unknown) rather than
    # defaulting to ``False``, so no surface can claim a session is invalid
    # without having probed it.
    probed = any(field in role_raw for field in LIVE_FACT_FIELDS)
    return RoleStatus(
        platform=name,
        role=role,
        state=state,
        enabled=enabled,
        authenticated=bool(role_raw.get("authenticated")) if probed else None,
        session_valid=bool(role_raw.get("session_valid")) if probed else None,
        auth_mode=_auth_mode(config, name, role_raw),
        live_checked=(bool(role_raw.get("live_checked", False)) if probed else None),
        error=role_raw.get("error"),
        details=details,
    )


def _aggregate_state(statuses: Mapping[str, RoleStatus], primary: ProviderRole) -> ProviderState:
    primary_state = statuses[primary].state
    if primary_state is ProviderState.READY:
        return primary_state
    states = {status.state for status in statuses.values()}
    if ProviderState.DEGRADED in states:
        return ProviderState.DEGRADED
    if ProviderState.BLOCKED_EXTERNAL_REVIEW in states:
        return ProviderState.BLOCKED_EXTERNAL_REVIEW
    if ProviderState.UNCONFIGURED in states:
        return ProviderState.UNCONFIGURED
    return primary_state


# ── posting truth ───────────────────────────────────────────────────────────
#
# A platform can be perfectly healthy as a SOURCE and still be unable to
# receive a post (TikTok before its Content Posting API app is approved).  The
# platform-level ``authenticated`` flag on a canonical entry describes the
# provider's *primary* role, and for a source-only provider that role is the
# source — it proves xPST can download from the platform and says nothing at
# all about its uploader.  Reading that flag as "this account is connected" is
# how a surface ends up promising a post it cannot deliver, so every posting
# decision on every surface goes through :func:`posting_truth` instead.

#: Roles a post can be delivered to.  Every other role is not a posting
#: capability, however healthy it is.
POSTING_ROLES: tuple[ProviderRole, ...] = (
    ProviderRole.VIDEO_DESTINATION,
    ProviderRole.MESSAGING,
)

#: The flat keys every canonical provider entry carries about posting.
POSTING_FIELDS: tuple[str, ...] = (
    "posting_destination",
    "posting_role",
    "posting_state",
    "posting_error",
    "can_post",
    "source_only",
    "posting_note",
)


def _posting_note(
    display_name: str,
    source_only: bool,
    state: str | None,
    error: str | None,
) -> str | None:
    """One truthful sentence explaining why a platform cannot be posted to."""
    if not source_only:
        return None
    note = (
        f"{display_name} is a source only: xPST downloads from it, and it cannot be "
        "used as a posting destination."
    )
    if error:
        note = f"{note} The uploader reported: {error}"
    elif state is None:
        note = f"{note} No destination role is configured for it."
    return note


def posting_truth(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Return the posting verdict for ONE canonical provider entry.

    ``can_post`` is True only when the provider's posting role is ``ready``
    according to a canonical live probe.  ``source_only`` is True when the
    provider *has* a posting role that is not usable while its effective auth
    mode is ``source_only`` — i.e. it is wired up as a download source, not as
    an upload destination.
    """
    statuses = entry.get("role_status") or entry.get("roles") or {}
    if not isinstance(statuses, Mapping):
        statuses = {}

    role_name: str | None = None
    role: Mapping[str, Any] = {}
    for candidate in POSTING_ROLES:
        status = statuses.get(candidate.value)
        if isinstance(status, Mapping) and status:
            role_name = candidate.value
            role = status
            break

    state = role.get("state")
    auth_modes = {
        str(entry.get("auth_mode") or ""),
        str(role.get("auth_mode") or ""),
    }
    can_post = state == ProviderState.READY.value
    source_only = (
        role_name is not None
        and not can_post
        and AuthMode.SOURCE_ONLY.value in auth_modes
    )
    error = role.get("error")
    return {
        "posting_destination": role_name is not None,
        "posting_role": role_name,
        "posting_state": state,
        "posting_error": error,
        "can_post": can_post,
        "source_only": source_only,
        "posting_note": _posting_note(
            str(entry.get("display_name") or entry.get("name") or "This platform"),
            source_only,
            state if isinstance(state, str) else None,
            str(error) if error else None,
        ),
    }


def with_posting_truth(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``entry`` plus the flat posting fields every surface can read.

    Idempotent: re-applying it to an entry that already carries a posting
    verdict (a cached payload from an older build, or the early-return path in
    :func:`build_canonical_status`) recomputes the same values rather than
    trusting the ones it finds.
    """
    enriched = dict(entry)
    truth = posting_truth(enriched)
    for field, value in truth.items():
        enriched[field] = value
    return enriched


def posting_capability(
    config: Any,
    name: str,
    live_facts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Posting verdict for one provider from config + optional live facts.

    ``live_facts`` are the platform's *posting-role* probe results (the shape
    the uploader ``check_health()`` returns).  Omitting them keeps the verdict
    offline: "not probed" yields ``can_post: False`` rather than an invented
    success.
    """
    raw = dict(live_facts or {})
    if raw and "live_checked" not in raw:
        raw["live_checked"] = True
    live = {name: raw} if raw else None
    canonical = build_canonical_status(config, live)
    return posting_truth(canonical.get(name) or {"name": name})


def live_status_from_booleans(
    config: Any,
    values: Mapping[str, bool],
) -> dict[str, dict[str, Any]]:
    """Rebuild role-aware live facts from a legacy ``{platform: bool}`` map.

    A single boolean cannot describe a provider with two independent
    capabilities.  For TikTok in ``source_only`` mode the boolean is the
    SOURCE verdict, so promoting it to every role would claim the uploader was
    verified by a probe that never ran against it.  The destination role gets
    the same not-configured verdict the live collector produces instead.
    """
    statuses = build_canonical_status(config, None)
    live: dict[str, dict[str, Any]] = {}
    for name, ok in values.items():
        if name == "local":
            continue
        entry = statuses.get(name) or {}
        if name == "tiktok" and entry.get("auth_mode") == AuthMode.SOURCE_ONLY.value:
            live[name] = {
                "authenticated": bool(ok),
                "session_valid": bool(ok),
                "live_checked": True,
                "error": None if ok else "Live check failed",
                "details": {},
                "source_check": {
                    "authenticated": bool(ok),
                    "session_valid": bool(ok),
                    "auth_mode": AuthMode.SOURCE_ONLY.value,
                    "live_checked": True,
                    "error": None if ok else "Live check failed",
                    "details": {},
                },
                "destination_check": {
                    "authenticated": False,
                    "session_valid": False,
                    "auth_mode": AuthMode.SOURCE_ONLY.value,
                    "live_checked": True,
                    "error": "TikTok Content Posting API is not configured",
                    "details": {"auth_mode": AuthMode.SOURCE_ONLY.value},
                },
            }
        else:
            live[name] = {
                "authenticated": bool(ok),
                "session_valid": bool(ok),
                "live_checked": True,
                "error": None if ok else "Live check failed",
                "details": {},
            }
    return live


def build_canonical_status(
    config: Any,
    live_status: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build canonical per-provider status from live probe results.

    ``live_status`` may be the legacy ``{platform: result}`` mapping or the
    envelope returned by :func:`canonical_status_report`.  An omitted mapping
    intentionally means "no live probe has run", not "healthy".
    """
    live: Mapping[str, Any] = live_status or {}
    if isinstance(live.get("platforms"), Mapping):
        live = live["platforms"]
    if isinstance(live.get("providers"), Mapping):
        live = live["providers"]
    if live and all(
        isinstance(value, Mapping) and "role_status" in value
        for value in live.values()
    ):
        return {
            name: with_posting_truth(live[name])
            for name in supported_provider_names()
            if name in live
        }
    result: dict[str, dict[str, Any]] = {}
    for definition in SUPPORTED_PROVIDERS:
        raw_value = live.get(definition.name, {})
        raw = raw_value if isinstance(raw_value, Mapping) else {}
        statuses = {
            role.value: _status_for_role(config, definition.name, role, raw)
            for role in definition.roles
        }
        primary = (
            ProviderRole.VIDEO_DESTINATION
            if ProviderRole.VIDEO_DESTINATION in definition.roles
            else ProviderRole.MESSAGING
            if ProviderRole.MESSAGING in definition.roles
            else ProviderRole.SOURCE
        )
        aggregate = _aggregate_state(statuses, primary)
        # Legacy auth fields represent the usable source for TikTok source-only
        # mode, and the primary role everywhere else.
        compat_role = primary
        if definition.name == "tiktok" and "source_check" in raw and _auth_mode(config, "tiktok", raw) == "source_only":
            compat_role = ProviderRole.SOURCE
        compat = statuses[compat_role.value]
        live_flags = [item.live_checked for item in statuses.values()]
        role_dict = {key: value.to_dict() for key, value in statuses.items()}
        # The compat fields describe the primary (or, for TikTok source-only,
        # the source) role.  When that role has no error but the provider is
        # nonetheless not usable, surface the role error that explains it —
        # otherwise a payload reads "state: unconfigured, authenticated: true,
        # error: null", which is a status that contradicts itself.
        error = compat.error
        if error is None and aggregate not in (ProviderState.READY, ProviderState.DISABLED):
            error = next((item.error for item in statuses.values() if item.error), None)
        # Probe classification (see xpst.utils.probe_errors): whether the
        # provider rejected the credential or the probe simply could not reach
        # a verdict.  Promoted to the platform level so every consumer can tell
        # "re-authenticate" apart from "retry" without re-parsing the message.
        compat_details = compat.details if isinstance(compat.details, Mapping) else {}
        result[definition.name] = with_posting_truth({
            "name": definition.name,
            "display_name": definition.display_name,
            "state": aggregate.value,
            "state_value": aggregate.value,
            "roles": role_dict,
            "role_status": role_dict,
            "role_states": {key: value["state"] for key, value in role_dict.items()},
            "role_names": [role.value for role in definition.roles],
            "auth_mode": compat.auth_mode,
            "authenticated": compat.authenticated,
            "session_valid": compat.session_valid,
            "live_checked": (
                True
                if any(flag is True for flag in live_flags)
                else None
                if all(flag is None for flag in live_flags)
                else False
            ),
            "error": error,
            "details": dict(compat_details),
            "probe_class": compat_details.get("probe_class"),
            "probe_error": compat_details.get("probe_error"),
            "probe_retryable": compat_details.get("probe_retryable"),
            "enabled": _enabled(config, definition.name),
            "legacy_authenticated": bool(raw.get("credentials_stored")),
        })
    return result


def canonical_status_report(
    config: Any,
    live_status: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return an envelope suitable for API and MCP clients."""
    candidate: Mapping[str, Any] = live_status or {}
    if isinstance(candidate.get("platforms"), Mapping):
        candidate = candidate["platforms"]
    if candidate and all(
        isinstance(value, Mapping) and "role_status" in value
        for value in candidate.values()
    ):
        providers = {name: with_posting_truth(entry) for name, entry in candidate.items()}
    else:
        providers = build_canonical_status(config, live_status)
    return {
        "providers": providers,
        # ``platforms`` is intentionally an alias for existing web/CLI clients.
        "platforms": providers,
        "roles": [role.value for role in (ProviderRole.SOURCE, ProviderRole.VIDEO_DESTINATION, ProviderRole.MESSAGING, ProviderRole.ANALYTICS)],
    }


def status_snapshot(config: Any, *, live: bool = True) -> dict[str, Any]:
    """Return the ONE provider-status document every surface must serve.

    ``live=True`` (the default, and what ``xpst auth status`` means) runs the
    canonical live probe and returns its verdict.  ``live=False`` returns the
    offline/config-only view, in which the live-derived facts are ``None``
    (unknown) rather than ``False``.

    CLI, MCP and the HTTP API all call this instead of assembling their own
    status payload; a fact therefore has exactly one implementation.
    """
    if not live:
        return canonical_status_report(config, None)
    from xpst.auth_status import collect_live_auth_status

    return canonical_status_report(config, collect_live_auth_status(config))


async def status_snapshot_async(config: Any, *, live: bool = True) -> dict[str, Any]:
    """Async :func:`status_snapshot` for callers already inside an event loop.

    The MCP tool dispatch is async, so it cannot use the sync wrapper (which
    calls ``asyncio.run``).  Same document, same probe, one implementation.
    """
    if not live:
        return canonical_status_report(config, None)
    from xpst.auth_status import collect_live_auth_status_async

    return canonical_status_report(config, await collect_live_auth_status_async(config))


def _manifest_for(name: str, config: Any) -> ProviderManifest | None:
    """Find a registered manifest without making provider imports mandatory."""
    try:
        from xpst.platforms.base import PlatformRegistry
        from xpst.sources.base import SourceRegistry

        PlatformRegistry.auto_discover()
        SourceRegistry.auto_discover()
        for manifest in [*PlatformRegistry.list_manifests(config), *SourceRegistry.list_manifests(config)]:
            if manifest.name == name:
                return manifest
    except Exception:
        return None
    return None


def canonical_provider_catalog(config: Any) -> dict[str, Any]:
    """Return static role-aware provider metadata without network calls."""
    statuses = build_canonical_status(config)
    providers: list[dict[str, Any]] = []
    for definition in SUPPORTED_PROVIDERS:
        manifest = _manifest_for(definition.name, config)
        item = dict(statuses[definition.name])
        item.update(
            {
                "roles": [role.value for role in definition.roles],
                "role_names": [role.value for role in definition.roles],
                "capabilities": (
                    [capability.value for capability in manifest.capabilities]
                    if manifest is not None
                    else []
                ),
                "is_official_api": definition.official_api,
                "docs_url": definition.docs_url,
                # Legacy manifest roles/capabilities remain discoverable but do
                # not change canonical role truth (notably Messenger).
                "legacy_roles": list(manifest.to_dict().get("roles", [])) if manifest else [],
            }
        )
        providers.append(item)
    return {
        "providers": providers,
        "by_name": {item["name"]: item for item in providers},
        "roles": [role.value for role in (ProviderRole.SOURCE, ProviderRole.VIDEO_DESTINATION, ProviderRole.MESSAGING, ProviderRole.ANALYTICS)],
    }


async def collect_canonical_status_async(config: Any) -> dict[str, dict[str, Any]]:
    """Collect live auth status and return the canonical role-aware mapping."""
    from xpst.auth_status import collect_live_auth_status_async

    live = await collect_live_auth_status_async(config)
    # The auth collector returns canonical entries in current builds.  Accept
    # legacy mappings too so third-party callers can upgrade incrementally.
    if live and all(isinstance(value, Mapping) and "roles" in value for value in live.values()):
        return {name: with_posting_truth(entry) for name, entry in live.items()}
    return build_canonical_status(config, live)


def collect_canonical_status(config: Any) -> dict[str, dict[str, Any]]:
    """Synchronous canonical status collector for CLI/doctor callers."""
    import asyncio

    return asyncio.run(collect_canonical_status_async(config))


__all__ = [
    "ProviderDefinition",
    "ProviderState",
    "RoleState",
    "RoleStatus",
    "ProviderStatus",
    "SUPPORTED_PROVIDERS",
    "POSTING_ROLES",
    "POSTING_FIELDS",
    "supported_provider_names",
    "provider_definition",
    "build_canonical_status",
    "canonical_status_report",
    "canonical_provider_catalog",
    "posting_truth",
    "with_posting_truth",
    "posting_capability",
    "live_status_from_booleans",
    "status_snapshot",
    "status_snapshot_async",
    "collect_canonical_status_async",
    "collect_canonical_status",
]
