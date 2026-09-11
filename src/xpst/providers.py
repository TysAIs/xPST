"""Shared provider metadata for source and destination adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ProviderRole(str, Enum):
    """Role a provider can play in xPST."""

    SOURCE = "source"
    VIDEO_DESTINATION = "video_destination"
    MESSAGING = "messaging"
    ANALYTICS = "analytics"
    # Kept for clients that used the pre-Phase-1 destination vocabulary. New
    # manifests should include VIDEO_DESTINATION explicitly; this value is
    # deliberately not treated as a video capability by canonical truth.
    DESTINATION = "destination"


class ProviderCapability(str, Enum):
    """Feature flags exposed by provider adapters."""

    LIST = "list"
    DOWNLOAD = "download"
    UPLOAD = "upload"
    DELETE = "delete"
    ANALYTICS = "analytics"
    CAROUSEL = "carousel"
    HEALTH = "health"
    OFFICIAL_API = "official_api"
    COOKIE_AUTH = "cookie_auth"
    OAUTH = "oauth"
    LOCAL_ONLY = "local_only"
    RATE_LIMITS = "rate_limits"


class AuthMode(str, Enum):
    """Authentication model for a provider."""

    NONE = "none"
    OAUTH = "oauth"
    GRAPH_API = "graph_api"
    API_V2 = "api_v2"
    COOKIES = "cookies"
    SESSION = "session"
    SOURCE_ONLY = "source_only"
    CONTENT_POSTING_API = "content_posting_api"
    LOCAL = "local"
    UNKNOWN = "unknown"


class ProviderState(str, Enum):
    """Canonical state for one provider role."""

    READY = "ready"
    UNCONFIGURED = "unconfigured"
    DISABLED = "disabled"
    DEGRADED = "degraded"
    BLOCKED_EXTERNAL_REVIEW = "blocked_external_review"


@dataclass(frozen=True)
class ProviderManifest:
    """Machine-readable provider description.

    The manifest is intentionally small and serializable so the CLI, desktop
    app, dashboard, MCP server, updater, and docs can all reason about providers
    without importing platform-specific implementation details.
    """

    name: str
    display_name: str
    roles: tuple[ProviderRole, ...]
    capabilities: tuple[ProviderCapability, ...] = field(default_factory=tuple)
    auth_mode: AuthMode = AuthMode.UNKNOWN
    is_official_api: bool = False
    is_local_first: bool = True
    docs_url: str | None = None
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def canonical_roles(self) -> tuple[ProviderRole, ...]:
        """Return Phase-1 roles while retaining legacy destination input.

        A manifest that explicitly declares a new role is authoritative. An
        old destination-only manifest is interpreted as a video destination so
        third-party providers do not silently lose their old capability.
        Messenger declares MESSAGING explicitly, so its legacy marker is not
        upgraded into a video destination.
        """
        explicit = tuple(role for role in self.roles if role is not ProviderRole.DESTINATION)
        if explicit:
            if (
                ProviderRole.DESTINATION in self.roles
                and ProviderRole.VIDEO_DESTINATION not in explicit
                and ProviderRole.MESSAGING not in explicit
            ):
                return explicit + (ProviderRole.VIDEO_DESTINATION,)
            return explicit
        if ProviderRole.DESTINATION in self.roles:
            return (ProviderRole.VIDEO_DESTINATION,)
        return explicit

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable manifest."""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "roles": [role.value for role in self.roles],
            "canonical_roles": [role.value for role in self.canonical_roles],
            "capabilities": [capability.value for capability in self.capabilities],
            "auth_mode": self.auth_mode.value,
            "is_official_api": self.is_official_api,
            "is_local_first": self.is_local_first,
            "docs_url": self.docs_url,
            "notes": self.notes,
            "extra": self.extra,
        }

