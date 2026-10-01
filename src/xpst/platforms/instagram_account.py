"""Instagram account-type truth: detection at connect time + switch guidance.

Meta's official publishing APIs refuse a **personal** Instagram account: an
account must be a Business or Creator (professional) account before a token
can publish to it. A personal account does not fail loudly at publish time —
it fails as an opaque Graph API refusal, which reads to a new user as a
mystery bug. This module turns that into a 30-second guided step.

Design rules (mirrored by tests/test_instagram_account_type.py):

* Detection NEVER invents a type. If the probe could not answer, the type is
  ``unknown`` — surfaces must not render a green publish-ready claim from an
  unprobed account.
* The guidance names the exact in-app screens (the same path
  ``docs/setup-instagram.md`` Step 1 documents, so the two can be diffed).
* No personal data: findings carry the type and non-identifying evidence only
  (Meta's own error strings), never usernames or token material.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

GRAPH_API_BASE = os.environ.get("XPST_GRAPH_API_BASE", "https://graph.facebook.com/v21.0")

#: The exact in-app path to switch a personal account to a Creator account.
#: Kept in one place so the CLI, the UI guidance string, and
#: docs/setup-instagram.md ("Step 1 — Convert to a Creator or Business
#: account") cannot drift into three different sets of screens.
SWITCH_STEPS: tuple[str, ...] = (
    "Open the Instagram app and go to your profile.",
    "Tap the menu (≡), then Settings and privacy.",
    "Tap Account type and tools.",
    "Tap Switch to professional account and follow the prompts.",
    "Choose Creator, then tap Done.",
)

#: Meta's own refusal text for a personal account (observed verbatim when a
#: publishing call is attempted, and documented as the access-denied reason).
PERSONAL_REFUSAL_MARKERS: tuple[str, ...] = (
    "account type is not business or creator",
    "switched to a personal account",
)

_PUBLISHABLE = ("BUSINESS", "CREATOR", "MEDIA_CREATOR")


class InstagramAccountType(str, Enum):
    """The account type Meta's publishing API can see.

    ``UNKNOWN`` is deliberately distinct from ``PERSONAL``: an unprobed or
    unreachable account is not a personal account, and surfaces must not say
    it is.
    """

    BUSINESS = "business"
    CREATOR = "creator"
    PERSONAL = "personal"
    UNKNOWN = "unknown"


@dataclass
class AccountTypeFinding:
    """What we learned about one Instagram account's type, and how we learned it.

    Attributes:
        account_type: The verdict (never guessed; ``UNKNOWN`` when unproven).
        source: Provenance label for the verdict — ``graph_field``,
            ``graph_error``, ``session_account_info``, or
            ``unknown:<reason>``. Surfaced so a human can audit the claim.
        evidence: Non-identifying support for the verdict (Meta's own field
            value or refusal text). Never a username, id, or token.
        raw: Extra non-secret fields worth keeping (masked by callers if not).
    """

    account_type: InstagramAccountType
    source: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def publish_ready(self) -> bool:
        """True only when Meta can publish to this account type."""
        return self.account_type in (InstagramAccountType.BUSINESS, InstagramAccountType.CREATOR)

    @property
    def requires_creator_switch(self) -> bool:
        """True when the account is proven personal — the guided switch applies."""
        return self.account_type is InstagramAccountType.PERSONAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_type": self.account_type.value,
            "source": self.source,
            "evidence": dict(self.evidence),
        }


def classify_graph_payload(payload: Mapping[str, Any]) -> AccountTypeFinding:
    """Classify a Graph API ``/{ig-user-id}?fields=account_type`` response.

    The verdict ladder, in order:

    1. A 200 payload carrying ``account_type`` decides directly
       (``BUSINESS`` → business, ``MEDIA_CREATOR``/``CREATOR`` → creator,
       ``PERSONAL`` → personal).
    2. An error payload whose message matches Meta's personal-account refusal
       (see ``PERSONAL_REFUSAL_MARKERS``) decides ``personal`` — this is the
       shape a personal account actually answers with, because the field is
       only served to professional accounts.
    3. Everything else is ``unknown``. Callers must not upgrade it.
    """
    raw = payload.get("account_type")
    if isinstance(raw, str) and raw.strip():
        value = raw.strip().upper()
        if value == "BUSINESS":
            return AccountTypeFinding(
                InstagramAccountType.BUSINESS, "graph_field", {"account_type": value}
            )
        if value in ("CREATOR", "MEDIA_CREATOR"):
            return AccountTypeFinding(
                InstagramAccountType.CREATOR, "graph_field", {"account_type": value}
            )
        if value == "PERSONAL":
            return AccountTypeFinding(
                InstagramAccountType.PERSONAL, "graph_field", {"account_type": value}
            )
        return AccountTypeFinding(
            InstagramAccountType.UNKNOWN,
            "unknown:unrecognised_field_value",
            {"account_type": value},
        )

    error = payload.get("error")
    message = ""
    if isinstance(error, Mapping):
        message = str(error.get("message", "") or "")
    elif isinstance(error, str):
        message = error
    lowered = message.lower()
    if any(marker in lowered for marker in PERSONAL_REFUSAL_MARKERS):
        return AccountTypeFinding(
            InstagramAccountType.PERSONAL,
            "graph_error",
            {"error_message": message[:200]},
        )
    reason = "error_payload_without_personal_marker" if message else "no_account_type_field"
    return AccountTypeFinding(InstagramAccountType.UNKNOWN, f"unknown:{reason}", {"error_message": message[:200]} if message else {})


def classify_session_account_info(account_info: Mapping[str, Any]) -> AccountTypeFinding:
    """Classify from an instagrapi ``account_info()``-shaped mapping.

    The private API works for personal accounts too, so a ``False`` here is
    informational, not a publish block on this auth mode. The private payload
    distinguishes business from creator inconsistently, so both professional
    states map to ``business`` — enough to tell the user "professional
    account" without inventing a creator verdict.
    """
    if "is_business" not in account_info:
        return AccountTypeFinding(InstagramAccountType.UNKNOWN, "unknown:session_field_missing")
    is_business = bool(account_info.get("is_business"))
    category = str(account_info.get("business_category_name") or "")
    evidence: dict[str, Any] = {"is_business": is_business}
    if category:
        evidence["business_category_name"] = category
    return AccountTypeFinding(
        InstagramAccountType.BUSINESS if is_business else InstagramAccountType.PERSONAL,
        "session_account_info",
        evidence,
    )


def unknown_finding(reason: str) -> AccountTypeFinding:
    """An honest 'we could not tell' — for paths where no probe could answer."""
    return AccountTypeFinding(InstagramAccountType.UNKNOWN, f"unknown:{reason}")


def persist_account_type(config: Any, finding: AccountTypeFinding) -> None:
    """Store a verified verdict on ``config.instagram``; save only on change.

    Status collectors run on every dashboard probe, so the config write
    happens only when the verdict actually moved — a steady professional
    account never touches the file, and an ``unknown`` verdict overwrites
    nothing (the last verified fact survives an unreachable probe).
    """
    if finding.account_type is InstagramAccountType.UNKNOWN:
        return
    ig = getattr(config, "instagram", None)
    if ig is None:
        return
    if ig.account_type == finding.account_type.value and ig.account_type_source == finding.source:
        return
    ig.account_type = finding.account_type.value
    ig.account_type_source = str(finding.source)
    save = getattr(config, "save", None)
    if callable(save):
        try:
            save()
        except Exception:  # noqa: BLE001 — a failed write must not fake a probe failure
            pass


def detect_graph_account_type(
    ig_user_id: str,
    access_token: str,
    timeout: float = 15.0,
) -> AccountTypeFinding:
    """Ask Meta directly what the account type is (one read-only GET).

    Requests ``fields=account_type`` for the connected IG user. Any transport
    or API failure that does not match the personal-refusal wording classifies
    as ``unknown`` — a network timeout must never be reported as "personal".
    """
    import httpx

    if not ig_user_id or not access_token:
        return unknown_finding("missing_id_or_token")
    try:
        response = httpx.get(
            f"{GRAPH_API_BASE}/{ig_user_id}",
            params={"fields": "id,account_type", "access_token": access_token},
            timeout=timeout,
        )
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 — a failed probe says "unknown", nothing stronger
        return unknown_finding(f"probe_failed:{type(exc).__name__}")
    if not isinstance(payload, dict):
        return unknown_finding("non_mapping_payload")
    return classify_graph_payload(payload)


def guidance_message(finding: AccountTypeFinding, auth_mode: str = "graph_api") -> str | None:
    """The single plain-language message a user sees for this finding.

    Returns ``None`` for publish-ready types (no warning for a healthy
    account). For personal accounts, names the exact in-app screens and the
    re-check action. For unknown, says so without claiming anything.

    ``auth_mode`` keeps the claim honest per path: on the official Graph API a
    personal account cannot publish at all; on the private session path it can
    still publish today, but the switch is the precondition for ever moving to
    the ban-safe official path.
    """
    if finding.publish_ready:
        return None
    if finding.account_type is InstagramAccountType.PERSONAL:
        steps = "\n".join(f"  {i}. {step}" for i, step in enumerate(SWITCH_STEPS, 1))
        lead = (
            "Instagram publishing requires a Creator (or Business) account — "
            "Meta's API cannot publish from a personal account."
            if auth_mode == "graph_api"
            else "Your Instagram account is personal. The session path can still "
            "post to it, but that private-API route risks account bans, and "
            "switching to the official ban-safe API requires a Creator account first."
        )
        return (
            f"{lead}\n"
            "Switching takes about 30 seconds in the Instagram app and keeps "
            "all your followers, likes and posts:\n"
            f"{steps}\n"
            "Then re-check by running: xpst connect instagram "
            "(or press 'Refresh accounts' in the app)."
        )
    return (
        "xPST could not confirm this Instagram account's type. Publishing may "
        "fail until a Creator/Business account is confirmed — re-run "
        "xpst connect instagram to check again."
    )
