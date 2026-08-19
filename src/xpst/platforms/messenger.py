"""Messenger (Facebook Messenger Platform) adapter for xPST — OPT-IN.

Implements a ManyChat-style auto-reply option on top of the official
Meta Graph API (direct ``httpx`` calls, mirroring ``platforms/threads.py``).
It is disabled by default: it only does anything when
``accounts.messenger.enabled`` is set and a Page Access Token exists.

Security model (per the xPST-Messenger design doc):
- Static Page Access Token, stored in the encrypted CredentialStore via
  ``SessionManager.get_messenger_token()`` (no refresh flow — page tokens
  are long-lived). ``MessengerAccountConfig.page_access_token`` is a
  convenience fallback.
- App Secret (from CredentialStore or config) is used for:
  - ``appsecret_proof`` (HMAC-SHA256 of the page token with the app secret)
    appended to every outbound call, and
  - ``X-Hub-Signature-256`` verification on inbound webhooks.
- Developer-chosen webhook ``verify_token`` proves the GET handshake.

Docs: https://developers.facebook.com/docs/messenger-platform
"""

from __future__ import annotations

import hashlib
import hmac
from typing import TYPE_CHECKING, Any

import httpx

from xpst.platforms.base import PlatformHealth, PlatformRegistry, PlatformUploader, UploadResult
from xpst.providers import AuthMode, ProviderCapability, ProviderManifest, ProviderRole
from xpst.utils.logger import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from xpst.config import XPSTConfig

logger = get_logger(__name__)

# Meta Graph API base + version pin (matches the design doc / IG Graph pin policy).
MESSENGER_API_BASE = "https://graph.facebook.com"
MESSENGER_API_VERSION = "v22.0"
# Hard text limit per Messenger message.
MESSENGER_MAX_TEXT_LENGTH = 640


class MessengerError(RuntimeError):
    """Raised on hard Messenger API failures (auth, network, rate limit, API error)."""


def appsecret_proof(page_token: str, app_secret: str | None) -> str | None:
    """Compute the ``appsecret_proof`` HMAC-SHA256 of the page token.

    Returns None when no app secret is configured (call still works without
    it, just with weaker protection).
    """
    if not app_secret:
        return None
    return hmac.new(app_secret.encode("utf-8"), page_token.encode("utf-8"), hashlib.sha256).hexdigest()


class MessengerAdapter(PlatformUploader):
    """Official Messenger Platform adapter — text send + ManyChat-lite auto-reply.

    Registered in ``PlatformRegistry`` under the name ``messenger`` and
    auto-discovered from the ``platforms/`` package. Idle when disabled.
    """

    MAX_CAPTION_LENGTH = MESSENGER_MAX_TEXT_LENGTH

    def __init__(self, config: XPSTConfig) -> None:
        """Initialize the adapter with lazy token caching."""
        super().__init__(config)
        self._page_token: str | None = None
        self._app_secret: str | None = None

    # ── Manifest ────────────────────────────────────────────────────────
    @property
    def manifest(self) -> ProviderManifest:
        """Return Messenger destination capabilities."""
        return ProviderManifest(
            name="messenger",
            display_name="Messenger",
            roles=(ProviderRole.DESTINATION,),
            capabilities=(
                ProviderCapability.HEALTH,
                ProviderCapability.OFFICIAL_API,
                ProviderCapability.RATE_LIMITS,
                ProviderCapability.UPLOAD,
            ),
            auth_mode=AuthMode.OAUTH,
            is_official_api=True,
            docs_url="https://developers.facebook.com/docs/messenger-platform",
            notes=(
                "Opt-in Messenger auto-reply (ManyChat-lite). Static Page Access Token, "
                "appsecret_proof on everything outbound, X-Hub-Signature-256 on inbound."
            ),
            extra={
                "content": ("text",),
                "max_caption_length": self.MAX_CAPTION_LENGTH,
            },
        )

    # ── Auth (static token; no refresh) ────────────────────────────────
    async def _get_page_token(self) -> str:
        """Return the static Page Access Token.

        Delegates to SessionManager (encrypted CredentialStore) when
        available, otherwise falls back to config.page_access_token.

        Returns:
            Page Access Token string.

        Raises:
            ValueError: If no token is configured.
            MessengerError: If token resolution fails.
        """
        if self._page_token:
            return self._page_token

        token: str | None = None
        if self._session_manager is not None:
            try:
                token = await self._session_manager.get_messenger_token()
            except Exception as e:
                logger.warning("Messenger token lookup failed: %s", e)
        if not token:
            token = self.config.messenger.page_access_token or None
        if not token:
            raise ValueError(
                "MESSENGER_NOT_CONFIGURED: Set a Page Access Token via 'xpst auth messenger' "
                "or accounts.messenger.page_access_token in config, and set enabled: true."
            )
        self._page_token = token
        return self._page_token

    async def _get_app_secret(self) -> str | None:
        """Return the App Secret (CredentialStore first, then config)."""
        if self._app_secret is not None:
            return self._app_secret or None
        secret: str | None = None
        if self._session_manager is not None:
            try:
                secret = await self._session_manager.get_messenger_secret()
            except Exception as e:
                logger.warning("Messenger app secret lookup failed: %s", e)
        if not secret:
            secret = self.config.messenger.app_secret or None
        self._app_secret = secret or ""
        return self._app_secret or None

    # ── Low-level Graph API ────────────────────────────────────────────
    async def _post_to_graph(
        self,
        path: str,
        *,
        params: dict[str, Any],
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST to the Graph API with token + appsecret_proof attached.

        Raises:
            ValueError: If no page token is configured.
            MessengerError: On HTTP/network/API errors.
        """
        token = await self._get_page_token()
        proof = appsecret_proof(token, await self._get_app_secret())
        payload = dict(params)
        payload["access_token"] = token
        if proof:
            payload["appsecret_proof"] = proof

        url = f"{MESSENGER_API_BASE}/{MESSENGER_API_VERSION}/{path}"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, params=payload, json=data)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code if e.response else 0
            body = e.response.text[:300] if e.response else str(e)
            if status_code == 401:
                raise MessengerError(
                    f"MESSENGER_AUTH_EXPIRED: Page Access Token invalid or expired. {body}"
                ) from e
            if status_code == 429:
                raise MessengerError(
                    "MESSENGER_RATE_LIMITED: Messenger API rate limit exceeded."
                ) from e
            raise MessengerError(f"MESSENGER_HTTP_ERROR: {body}") from e
        except httpx.HTTPError as e:
            raise MessengerError(f"MESSENGER_NETWORK_ERROR: {str(e)[:200]}") from e

    # ── Send methods ───────────────────────────────────────────────────
    async def send_text(
        self,
        recipient: str,
        text: str,
        *,
        messaging_type: str = "RESPONSE",
    ) -> dict[str, Any]:
        """Send a text message to a page-scoped PSID (recipient).

        Args:
            recipient: Page-scoped PSID of the user (from a webhook event).
            text: Message body (truncated to 640 chars).
            messaging_type: One of RESPONSE / UPDATE / MESSAGE_TAG.

        Returns:
            The Graph API response dict (contains ``message_id``).

        Raises:
            ValueError: If no page token is configured.
            MessengerError: On Graph API/network/rate-limit errors.
        """
        if not recipient:
            raise ValueError("MESSENGER_NO_RECIPIENT: recipient PSID is required.")
        text = self._truncate(text)
        return await self._post_to_graph(
            "me/messages",
            params={"messaging_type": messaging_type},
            data={"recipient": {"id": recipient}, "message": {"text": text}},
        )

    async def send(
        self,
        recipient: str,
        text: str,
        *,
        messaging_type: str = "RESPONSE",
    ) -> dict[str, Any]:
        """Alias for :meth:`send_text`."""
        return await self.send_text(recipient, text, messaging_type=messaging_type)

    async def send_action(self, recipient: str, action: str = "typing_on") -> dict[str, Any]:
        """Send a sender-action (typing_on/typing_off/mark_seen).

        Args:
            recipient: Page-scoped PSID.
            action: One of typing_on / typing_off / mark_seen.

        Returns:
            The Graph API response dict.
        """
        return await self._post_to_graph(
            "me/messages",
            params={"messaging_type": "RESPONSE"},
            data={"recipient": {"id": recipient}, "sender_action": action},
        )

    async def send_quick_replies(
        self,
        recipient: str,
        text: str,
        quick_replies: list[dict[str, Any]],
        *,
        messaging_type: str = "RESPONSE",
    ) -> dict[str, Any]:
        """Send text with quick-reply buttons.

        Args:
            recipient: Page-scoped PSID.
            text: Message body.
            quick_replies: List of ``{content_type, title, payload}`` dicts.
            messaging_type: Messenger messaging_type.

        Returns:
            The Graph API response dict.
        """
        data = {
            "recipient": {"id": recipient},
            "message": {"text": self._truncate(text), "quick_replies": quick_replies},
        }
        return await self._post_to_graph(
            "me/messages", params={"messaging_type": messaging_type}, data=data
        )

    async def delete(self, post_id: str) -> bool:
        """No-op (Messenger sends have no deletable post). ``post_id`` accepted for contract."""
        return True

    # ── Contract: upload() thin wrapper ────────────────────────────────
    async def upload(self, video_path: Path, caption: str) -> UploadResult:
        """Deliver ``caption`` as a text message (Messenger is text-first).

        The message is sent to the configured ``page_id``; if unset, no live
        call is made and a text/config note is returned. Video is not posted
        in v1.
        """
        try:
            await self._get_page_token()  # raises ValueError if not configured
            recipient = self.config.messenger.page_id or ""
            if not recipient:
                return UploadResult(
                    success=False,
                    error="MESSENGER_NO_RECIPIENT: set accounts.messenger.page_id for direct upload().",
                    platform="messenger",
                )
            data = await self.send_text(recipient, caption)
            return UploadResult(
                success=True,
                post_id=str(data.get("message_id", "")),
                post_url="https://www.messenger.com/",
                platform="messenger",
                metadata={"recipient": recipient, "message_recipient_id": data.get("recipient_id", "")},
            )
        except (ValueError, MessengerError, httpx.HTTPError) as e:
            logger.warning("Messenger upload (send) failed: %s", e)
            return UploadResult(success=False, error=str(e)[:300], platform="messenger")

    # ── Health ─────────────────────────────────────────────────────────
    async def check_health(self) -> PlatformHealth:
        """Check Messenger authentication health against GET /me.

        Returns:
            PlatformHealth with authentication status.
        """
        try:
            token = await self._get_page_token()
            proof = appsecret_proof(token, await self._get_app_secret())
            params: dict[str, Any] = {"fields": "id,name", "access_token": token}
            if proof:
                params["appsecret_proof"] = proof
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{MESSENGER_API_BASE}/{MESSENGER_API_VERSION}/me", params=params
                )
                resp.raise_for_status()
                data = resp.json()
                if not data.get("id"):
                    return PlatformHealth(
                        platform="messenger",
                        authenticated=False,
                        session_valid=False,
                        error="No page data returned — token may be invalid",
                    )
                return PlatformHealth(
                    platform="messenger",
                    authenticated=True,
                    session_valid=True,
                    details={"id": str(data.get("id", "")), "name": str(data.get("name", ""))},
                )
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code if e.response else 0
            if status_code == 401:
                return PlatformHealth(
                    platform="messenger",
                    authenticated=False,
                    session_valid=False,
                    error="MESSENGER_AUTH_EXPIRED: Page Access Token invalid. Set it via 'xpst auth messenger'",
                )
            return PlatformHealth(
                platform="messenger",
                authenticated=False,
                session_valid=False,
                error=f"Health check failed: {str(e)[:200]}",
            )
        except (ValueError, MessengerError) as e:
            return PlatformHealth(
                platform="messenger",
                authenticated=False,
                session_valid=False,
                error=str(e),
            )
        except Exception as e:
            return PlatformHealth(
                platform="messenger",
                authenticated=False,
                session_valid=False,
                error=f"Health check failed: {str(e)[:200]}",
            )

    # ── Inbound payload (ManyChat-lite) ────────────────────────────────
    async def handle_webhook_payload(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Process a verified Messenger webhook payload and send auto-replies.

        Only ``message`` events with visible text are handled; echoes and
        non-message events are skipped. When ``auto_reply`` is on, the text is
        matched against ``reply_rules`` (longest keyword wins, ``*`` is the
        default catch-all) and the matching response is sent back to the sender.

        Args:
            payload: Verified webhook body from Meta.

        Returns:
            List of per-event result dicts (``event``, ``sent``, ``response``,
            ``error``) for logging/testing.
        """
        results: list[dict[str, Any]] = []
        try:
            rules = self._reply_rules()
            auto_reply = bool(self.config.messenger.auto_reply)
        except Exception as e:
            logger.error("Messenger rule config load failed: %s", e)
            return results

        for entry in payload.get("entry", []):
            for messaging in entry.get("messaging", []):
                event = messaging.get("message")
                if not event:
                    continue
                if event.get("is_echo"):
                    continue
                sender = messaging.get("sender", {}).get("id", "")
                text = str(event.get("text", "")).strip()
                if not text:
                    continue
                if not auto_reply:
                    results.append({"event": "message", "sent": False, "response": None})
                    continue
                response = self._match_rule(text, rules)
                if not response:
                    results.append({"event": "message", "sent": False, "response": None})
                    continue
                try:
                    await self.send_text(sender, response)
                    results.append({"event": "message", "sent": True, "response": response, "sender": sender})
                except Exception as e:
                    logger.error("Messenger auto-reply failed to %s: %s", sender, e)
                    results.append(
                        {"event": "message", "sent": False, "response": response, "sender": sender, "error": str(e)[:200]}
                    )
        return results

    def _reply_rules(self) -> dict[str, str]:
        """Return the sanitized ManyChat-lite keyword → reply map.

        Keys are lowercased. ``*`` is treated as the catch-all.
        """
        rules = self.config.messenger.reply_rules or {}
        return {str(k).lower(): str(v) for k, v in rules.items() if str(v)}

    def _match_rule(self, text: str, rules: dict[str, str]) -> str | None:
        """Return the response for ``text``, longest keyword match wins.

        ``*`` serves as the catch-all default reply; keywords match as
        case-insensitive substrings.
        """
        lowered = text.lower()
        matches: list[tuple[int, str]] = []
        for keyword, response in rules.items():
            if keyword == "*":
                continue
            if keyword and keyword in lowered:
                matches.append((len(keyword), response))
        if matches:
            matches.sort(key=lambda pair: pair[0], reverse=True)
            return matches[0][1]
        return rules.get("*")

    @staticmethod
    def _truncate(text: str) -> str:
        """Truncate text to the Messenger 640-char limit."""
        if len(text) <= MESSENGER_MAX_TEXT_LENGTH:
            return text
        return text[: MESSENGER_MAX_TEXT_LENGTH - 3] + "..."


PlatformRegistry.register("messenger", MessengerAdapter)
