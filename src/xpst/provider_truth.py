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

from xpst.providers import ProviderManifest, ProviderRole, ProviderState

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
    """Status of one provider/role pair."""

    platform: str
    role: ProviderRole
    state: ProviderState
    enabled: bool
    authenticated: bool = False
    session_valid: bool = False
    auth_mode: str = "unknown"
    live_checked: bool = False
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
    return RoleStatus(
        platform=name,
        role=role,
        state=state,
        enabled=enabled,
        authenticated=bool(role_raw.get("authenticated")),
        session_valid=bool(role_raw.get("session_valid")),
        auth_mode=_auth_mode(config, name, role_raw),
        live_checked=bool(role_raw.get("live_checked", False)),
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
            name: dict(live[name])
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
        role_dict = {key: value.to_dict() for key, value in statuses.items()}
        error = compat.error
        result[definition.name] = {
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
            "live_checked": any(item.live_checked for item in statuses.values()),
            "error": error,
            "details": dict(compat.details or {}),
            "enabled": _enabled(config, definition.name),
            "legacy_authenticated": bool(raw.get("credentials_stored")),
        }
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
        providers = dict(candidate)
    else:
        providers = build_canonical_status(config, live_status)
    return {
        "providers": providers,
        # ``platforms`` is intentionally an alias for existing web/CLI clients.
        "platforms": providers,
        "roles": [role.value for role in (ProviderRole.SOURCE, ProviderRole.VIDEO_DESTINATION, ProviderRole.MESSAGING, ProviderRole.ANALYTICS)],
    }


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
        return dict(live)
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
    "supported_provider_names",
    "provider_definition",
    "build_canonical_status",
    "canonical_status_report",
    "canonical_provider_catalog",
    "collect_canonical_status_async",
    "collect_canonical_status",
]
