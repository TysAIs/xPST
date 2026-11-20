"""Ban-risk advisories for platforms driven by an unofficial auth path.

The product gate is "cross-posting that respects each platform's rules (no
bans)". Two shipped integrations are *not* a provider's official API:

* Instagram ``auth_mode: session`` — instagrapi drives Instagram's private
  mobile API. Instagram's terms forbid automated access outside the official
  API and enforcement is account action-blocks and suspension.
* X ``auth_mode: cookies`` — twikit replays the logged-in web session outside
  the official X API.

Neither path is hidden in the code (``xpst providers`` already reports
``is_official_api: false``), but nothing at the *decision point* said "this is
the ban-risky path", and readiness reporting called such an account simply
"ready". This module is the single registry and reporter for that fact:

* :func:`auth_path_risk` — the risk for one platform, or ``None`` when that
  platform is on its official path (or is official by construction);
* :func:`active_auth_path_risks` — the risks for platforms the operator has
  ENABLED. A platform disabled in config is a deliberate pause, not a warning;
  the configured Meta pause must never raise a banner;
* :func:`record_acknowledgement` / :func:`read_acknowledgements` — the durable
  record that the operator knowingly chose the unofficial path.

Severity vocabulary is deliberately two-tier:

``high``
    The provider's terms forbid the path outright (Instagram's private API).
    Readiness reports it as a warning row; doctor lists it; preflight prints
    it as an advisory on every destination that would use it.
``advisory``
    The path is unofficial but is xPST's documented check-in default
    (X cookies/twikit). It is reported identically everywhere, but it does not
    turn readiness into "ready with warnings" — the X free-tier official API
    is a far lower ceiling, and nagging about the default path in a setup
    report would train operators to ignore the section.

Nothing here may raise the risk as a *failure*: doctor keeps advisories out of
``issues`` (so they cannot flip ``all_clear`` or the exit code), preflight adds
them as warnings (never blockers), and readiness caps them at warning severity.
The unofficial path still works — it is simply no longer silent.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xpst.utils.secure_io import write_text_0600

logger = logging.getLogger(__name__)

#: Ban-risk codes carried in every payload. These are the values a caller
#: matches on; the human message is never parsed.
BAN_RISK_PRIVATE_API = "unofficial_private_api"
BAN_RISK_WEB_CLIENT = "unofficial_web_client"

#: Advisory codes used on preflight/route-plan warnings. One per ban-risk so a
#: consumer can tell the private-API case from the web-client case without
#: reading prose.
ADVISORY_CODE_PRIVATE_API = "UNOFFICIAL_PRIVATE_API"
ADVISORY_CODE_WEB_CLIENT = "UNOFFICIAL_WEB_CLIENT"

#: Operator-choice record. Not a secret, but it names an account auth decision,
#: so it lives beside the credentials with owner-only permissions.
ACK_FILE_NAME = "ban_risk_acknowledgements.json"
ACK_FILE_VERSION = 1


@dataclass(frozen=True)
class UnofficialAuthPath:
    """One static description of a non-official integration.

    This is configuration-independent: it says what the path *is* and what the
    provider does about it. Whether it currently applies is decided by
    :func:`auth_path_risk` from the loaded config.
    """

    platform: str
    display_name: str
    auth_mode: str
    ban_risk: str
    advisory_code: str
    severity: str
    official_mode: str
    official_label: str
    switch_command: str
    docs_url: str
    consequence: str
    requirement: str


#: The registry. Adding a platform whose integration automates a web/mobile
#: client outside the provider's API means adding an entry here — that is what
#: makes doctor/readiness/preflight report it without further wiring.
UNOFFICIAL_AUTH_PATHS: dict[str, UnofficialAuthPath] = {
    "instagram": UnofficialAuthPath(
        platform="instagram",
        display_name="Instagram",
        auth_mode="session",
        ban_risk=BAN_RISK_PRIVATE_API,
        advisory_code=ADVISORY_CODE_PRIVATE_API,
        severity="high",
        official_mode="graph_api",
        official_label="Meta Graph API",
        switch_command="xpst connect instagram",
        docs_url="docs/setup-instagram.md",
        consequence=(
            "Instagram's terms forbid automated access outside the official API, "
            "and enforcement is account action-blocks, challenges and suspension — "
            "not a polite error."
        ),
        requirement=(
            "an Instagram Creator/Business account linked to a Facebook Page"
        ),
    ),
    "x": UnofficialAuthPath(
        platform="x",
        display_name="X",
        auth_mode="cookies",
        ban_risk=BAN_RISK_WEB_CLIENT,
        advisory_code=ADVISORY_CODE_WEB_CLIENT,
        severity="advisory",
        official_mode="api_v2",
        official_label="X API v2",
        switch_command="xpst connect x",
        docs_url="docs/setup-x.md",
        consequence=(
            "twikit replays the logged-in web session: X rate-limits and can "
            "suspend automated clients that use it at volume."
        ),
        requirement="X API v2 access (free tier: 17 posts/day)",
    ),
}


@dataclass(frozen=True)
class AuthPathRisk:
    """The advisory for one platform, as this config currently stands."""

    platform: str
    display_name: str
    auth_mode: str
    ban_risk: str
    advisory_code: str
    severity: str
    message: str
    official_mode: str
    official_label: str
    switch_command: str
    docs_url: str
    consequence: str
    requirement: str
    enabled: bool
    acknowledged: bool = False
    acknowledgement: dict[str, Any] | None = None

    @property
    def is_high(self) -> bool:
        return self.severity == "high"

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "display_name": self.display_name,
            "auth_mode": self.auth_mode,
            "ban_risk": self.ban_risk,
            "severity": self.severity,
            "message": self.message,
            "official_mode": self.official_mode,
            "official_label": self.official_label,
            "switch_command": self.switch_command,
            "docs_url": self.docs_url,
            "consequence": self.consequence,
            "requirement": self.requirement,
            "enabled": self.enabled,
            "acknowledged": self.acknowledged,
            "acknowledgement": dict(self.acknowledgement or {}) or None,
            # Preflight-shaped advisory: the same text a route plan carries, so
            # doctor and preflight cannot disagree about the wording.
            "advisory": self.advisory(),
        }

    def advisory(self) -> dict[str, str]:
        """The ``{code, message}`` pair preflight/route plans carry."""
        return {"code": self.advisory_code, "message": self.message}


def _ack_path(config: Any) -> Path:
    return Path(str(getattr(config, "config_dir", "") or "")).expanduser() / ACK_FILE_NAME


def _configured_auth_mode(config: Any, platform: str) -> str:
    account = getattr(config, platform, None)
    return str(getattr(account, "auth_mode", "") or "").strip().lower()


def _platform_enabled(config: Any, platform: str) -> bool:
    account = getattr(config, platform, None)
    return bool(getattr(account, "enabled", False))


def read_acknowledgements(config: Any) -> dict[str, dict[str, Any]]:
    """Read the operator-choice record; never raises.

    A missing file is the normal state (nothing acknowledged yet). A corrupt
    file is reported as empty rather than aborting a doctor run — the advisory
    simply reads as unacknowledged again, which is the safe direction.
    """
    path = _ack_path(config)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:  # unreadable is not "acknowledged"
        logger.debug("Could not read %s: %s", path, exc)
        return {}
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        logger.debug("Ignoring malformed %s: %s", path, exc)
        return {}
    if not isinstance(payload, dict):
        return {}
    entries = payload.get("acknowledgements")
    if not isinstance(entries, dict):
        return {}
    return {
        str(name): dict(entry)
        for name, entry in entries.items()
        if isinstance(entry, dict)
    }


def acknowledgement_for(config: Any, platform: str) -> dict[str, Any] | None:
    """The acknowledgement that covers this platform's CURRENT auth mode.

    An acknowledgement is bound to the mode it was given for: an operator who
    accepted ``session`` has not thereby accepted some other unofficial mode
    that a later config edit introduced.
    """
    entry = read_acknowledgements(config).get(str(platform).strip().lower())
    if not entry:
        return None
    current = _configured_auth_mode(config, platform)
    if str(entry.get("auth_mode", "")).strip().lower() != current:
        return None
    return entry


def record_acknowledgement(
    config: Any,
    platform: str,
    *,
    surface: str,
    auth_mode: str | None = None,
    when: str | None = None,
) -> dict[str, Any]:
    """Persist that the operator knowingly chose this unofficial path.

    Args:
        config: The loaded config (``config_dir`` decides the file location).
        platform: Platform name, e.g. ``instagram``.
        surface: The command/endpoint that recorded the choice. Audit text.
        auth_mode: Mode accepted; defaults to what the config now says.
        when: ISO timestamp override (tests only).

    Returns:
        The stored record.
    """
    key = str(platform).strip().lower()
    entry = UNOFFICIAL_AUTH_PATHS.get(key)
    mode = str(auth_mode or _configured_auth_mode(config, key)).strip().lower()
    record: dict[str, Any] = {
        "platform": key,
        "auth_mode": mode,
        "ban_risk": entry.ban_risk if entry else "",
        "acknowledged_at": when or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "surface": surface,
        "operator_choice": True,
    }
    records = read_acknowledgements(config)
    records[key] = record
    path = _ack_path(config)
    payload = {"version": ACK_FILE_VERSION, "acknowledgements": records}
    write_text_0600(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return record


def auth_path_risk(config: Any, platform: str) -> AuthPathRisk | None:
    """Return the ban-risk advisory for ``platform``, or ``None``.

    ``None`` means "this platform is on its official path" — the caller must
    not render an advisory for it. Official-by-construction platforms (YouTube,
    TikTok Content Posting API, Threads, Facebook, Messenger) have no registry
    entry and therefore never produce one.
    """
    key = str(platform).strip().lower()
    entry = UNOFFICIAL_AUTH_PATHS.get(key)
    if entry is None:
        return None
    if _configured_auth_mode(config, key) != entry.auth_mode:
        return None

    acknowledged = acknowledgement_for(config, key)
    ack_at = str(acknowledged.get("acknowledged_at", "")) if acknowledged else ""
    tail = (
        f" The operator acknowledged this path on {ack_at}."
        if ack_at
        else " Nothing has recorded that the operator accepted this risk."
    )
    message = (
        f"{entry.display_name} is on its unofficial auth path (auth_mode: "
        f"'{entry.auth_mode}', ban_risk: {entry.ban_risk}): {entry.consequence} "
        f"The ban-safe path is {entry.official_label} (auth_mode: "
        f"'{entry.official_mode}'); it needs {entry.requirement}. Switch with "
        f"'{entry.switch_command}'.{tail}"
    )
    return AuthPathRisk(
        platform=key,
        display_name=entry.display_name,
        auth_mode=entry.auth_mode,
        ban_risk=entry.ban_risk,
        advisory_code=entry.advisory_code,
        severity=entry.severity,
        message=message,
        official_mode=entry.official_mode,
        official_label=entry.official_label,
        switch_command=entry.switch_command,
        docs_url=entry.docs_url,
        consequence=entry.consequence,
        requirement=entry.requirement,
        enabled=_platform_enabled(config, key),
        acknowledged=bool(acknowledged),
        acknowledgement=acknowledged,
    )


def auth_path_risks(config: Any) -> list[AuthPathRisk]:
    """Every risk that currently applies, including disabled platforms.

    Callers that render user-facing warnings should use
    :func:`active_auth_path_risks`; this one exists so an audit payload can
    show the full picture without pretending a paused platform is active.
    """
    risks = [auth_path_risk(config, name) for name in UNOFFICIAL_AUTH_PATHS]
    return [risk for risk in risks if risk is not None]


def active_auth_path_risks(config: Any) -> list[AuthPathRisk]:
    """Risks for ENABLED platforms only — the ones a post could actually use.

    A platform disabled in config is a configured pause (Instagram/Threads/
    Facebook/Messenger are deliberately off). Raising a ban-risk banner for a
    paused platform is noise that trains people to ignore the real advisory.
    """
    return [risk for risk in auth_path_risks(config) if risk.enabled]


def route_plan_advisory(config: Any, platform: str) -> dict[str, str] | None:
    """The preflight warning a destination on an unofficial path must carry."""
    risk = auth_path_risk(config, platform)
    if risk is None or not risk.enabled:
        return None
    return risk.advisory()


__all__ = [
    "ACK_FILE_NAME",
    "ADVISORY_CODE_PRIVATE_API",
    "ADVISORY_CODE_WEB_CLIENT",
    "BAN_RISK_PRIVATE_API",
    "BAN_RISK_WEB_CLIENT",
    "AuthPathRisk",
    "UNOFFICIAL_AUTH_PATHS",
    "UnofficialAuthPath",
    "acknowledgement_for",
    "active_auth_path_risks",
    "auth_path_risk",
    "auth_path_risks",
    "read_acknowledgements",
    "record_acknowledgement",
    "route_plan_advisory",
]
