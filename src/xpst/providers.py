"""Shared provider metadata for source and destination adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ProviderRole(str, Enum):
    """Role a provider can play in xPST."""

    SOURCE = "source"
    DESTINATION = "destination"
    ANALYTICS = "analytics"


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
    COOKIES = "cookies"
    SESSION = "session"
    LOCAL = "local"
    UNKNOWN = "unknown"


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

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable manifest."""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "roles": [role.value for role in self.roles],
            "capabilities": [capability.value for capability in self.capabilities],
            "auth_mode": self.auth_mode.value,
            "is_official_api": self.is_official_api,
            "is_local_first": self.is_local_first,
            "docs_url": self.docs_url,
            "notes": self.notes,
            "extra": self.extra,
        }

