"""Rumble uploader — capability-flagged, deliberately integration-uncertain.

Rumble is a long-form video platform, and its upload API is **partner-gated**:
keys are issued to partners and there is no self-serve developer console
comparable to Meta's or Google's. Advertising Rumble as "ready" because a config
file mentions it is exactly the kind of claim this project refuses to make, so
this adapter has two honest paths and states which one it used:

``api``
    An ``api_key`` is configured. The rendition is POSTed to the partner
    endpoint (``api_url`` — configurable because Rumble issues a per-partner
    URL) through :class:`RumbleTransport`. The transport is a seam: tests inject
    a fake one, so the whole adapter is exercised with zero network calls.

``manual_handoff``
    No API key. The rendition and a caption sidecar are copied into
    ``manual_handoff_dir`` and the result reports a **handoff**, carrying the
    exported file paths and the Rumble upload page — never a fake success.
    A handoff is not a publication, and the caller is told so in the error text.

Advertising rules:

* ``manifest.extra["integration"] == "integration_uncertain"`` and
  ``manifest.extra["verified_live"] == False`` until a maintainer records one
  successful live upload (``LIVE_VERIFIED = True`` after that upload is
  confirmed on the Rumble side). The capability document carries the same
  ``integration_uncertain`` status from ``xpst.content``, so CLI/MCP/HTTP cannot
  disagree.
* No duration or size ceiling is enforced here: Rumble's limits are per-plan and
  the registry stores ``None`` rather than an invented number.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from xpst.platforms.base import (
    DeleteOutcome,
    DeleteResult,
    PlatformHealth,
    PlatformRegistry,
    PlatformUploader,
    UploadResult,
)
from xpst.providers import AuthMode, ProviderCapability, ProviderManifest, ProviderRole
from xpst.utils.logger import get_logger

if TYPE_CHECKING:
    from xpst.config import XPSTConfig

logger = get_logger(__name__)

#: Stable error code for "exported for a human, not uploaded".
RUMBLE_HANDOFF_CODE = "RUMBLE_MANUAL_HANDOFF"
#: Stable error code for "the partner API answered with something unusable".
RUMBLE_UPLOAD_CODE = "RUMBLE_UPLOAD_ERROR"


class RumbleTransport(Protocol):
    """The one seam between this adapter and Rumble's partner API.

    A transport takes the rendition plus metadata and returns a mapping with at
    least ``id`` (or ``video_id``) and optionally ``url``. Anything it raises is
    reported as :data:`RUMBLE_UPLOAD_CODE` — never silently swallowed into a
    success.
    """

    async def upload(
        self,
        video_path: Path,
        *,
        title: str,
        description: str,
        api_url: str,
        api_key: str,
        visibility: str = "public",
    ) -> dict[str, Any]: ...


@dataclass
class HttpRumbleTransport:
    """Best-effort partner-API transport (multipart POST + bearer key).

    Rumble issues the endpoint and key per partner, so both come from config.
    This is intentionally thin: the seam exists so a partner with a different
    shape can supply its own transport without touching the uploader, and so
    tests never need the network.
    """

    timeout: float = 600.0

    async def upload(
        self,
        video_path: Path,
        *,
        title: str,
        description: str,
        api_url: str,
        api_key: str,
        visibility: str = "public",
    ) -> dict[str, Any]:
        import httpx

        headers = {"Authorization": f"Bearer {api_key}"}
        data = {
            "title": title,
            "description": description,
            "visibility": visibility,
        }
        with open(video_path, "rb") as handle:
            files = {"video": (video_path.name, handle, "video/mp4")}
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(api_url, headers=headers, data=data, files=files)
                response.raise_for_status()
                payload = response.json()
        return payload if isinstance(payload, dict) else {"id": None, "raw": payload}


class RumbleUploader(PlatformUploader):
    """Rumble destination: partner API when available, manual handoff otherwise."""

    #: Flipped to True only after one live upload has been confirmed on Rumble.
    #: Until then the manifest says ``verified_live: False`` and the capability
    #: registry says ``integration_uncertain``.
    LIVE_VERIFIED = False

    def __init__(
        self,
        config: XPSTConfig,
        *,
        transport: RumbleTransport | None = None,
    ) -> None:
        super().__init__(config)
        self._transport_override = transport

    # ── configuration ───────────────────────────────────────────────────

    @property
    def _account(self):
        account = getattr(self.config, "rumble", None)
        if account is None:
            # A config object without a rumble section (older callers, plugin
            # tests) still gets the documented defaults instead of an
            # AttributeError.
            from xpst.config import RumbleAccountConfig

            account = RumbleAccountConfig()
        return account

    @property
    def api_key(self) -> str:
        return str(getattr(self._account, "api_key", "") or "").strip()

    @property
    def api_url(self) -> str:
        return str(getattr(self._account, "api_url", "") or "").strip()

    @property
    def handoff_dir(self) -> Path:
        configured = str(getattr(self._account, "manual_handoff_dir", "") or "~/.xpst/rumble-handoff")
        return Path(configured).expanduser()

    @property
    def upload_page_url(self) -> str:
        return str(getattr(self._account, "upload_page_url", "") or "https://rumble.com/upload.php")

    @property
    def mode(self) -> str:
        """``api`` when a key is configured, else ``manual_handoff``."""
        return "api" if self.api_key else "manual_handoff"

    def transport(self) -> RumbleTransport:
        """The injected transport for tests, else the HTTP one."""
        return self._transport_override or HttpRumbleTransport()

    # ── provider contract ───────────────────────────────────────────────

    @property
    def manifest(self) -> ProviderManifest:
        """Rumble capability declaration — integration-uncertain by design."""
        return ProviderManifest(
            name="rumble",
            display_name="Rumble",
            roles=(ProviderRole.VIDEO_DESTINATION,),
            capabilities=(
                ProviderCapability.UPLOAD,
                ProviderCapability.HEALTH,
            ),
            auth_mode=AuthMode.UNKNOWN if not self.api_key else AuthMode.API_V2,
            is_official_api=True,
            docs_url="https://rumble.com/",
            notes=(
                "Long-form video. Upload API is partner-gated with no self-serve developer console: "
                "xPST publishes through it when an API key is configured, and otherwise exports the "
                "rendition for a manual handoff. Not advertised as ready until one live upload succeeds."
            ),
            extra={
                "content": ("video",),
                "integration": "integration_uncertain",
                "verified_live": self.LIVE_VERIFIED,
                "mode": self.mode,
                "manual_handoff": True,
                "upload_page_url": self.upload_page_url,
            },
        )

    async def check_health(self) -> PlatformHealth:
        """Report which path this destination would use, without a network call.

        A manual handoff is a usable operating mode, not a fault, so it is
        reported as available while ``details["integration"]`` keeps the
        uncertainty visible.
        """
        details = {
            "integration": "integration_uncertain",
            "mode": self.mode,
            "verified_live": self.LIVE_VERIFIED,
            "manual_handoff_dir": str(self.handoff_dir),
            "upload_page_url": self.upload_page_url,
        }
        if self.api_key:
            return PlatformHealth(
                platform="rumble",
                authenticated=True,
                session_valid=True,
                details=details,
            )
        return PlatformHealth(
            platform="rumble",
            authenticated=True,
            session_valid=True,
            details=details,
            error=None,
        )

    async def upload(
        self,
        video_path: Path,
        caption: str,
        *,
        visibility: str | None = None,
    ) -> UploadResult:
        """Publish (or hand off) one long-form rendition.

        Returns ``success=True`` only for an acknowledged API upload. A handoff
        returns ``success=False`` with ``RUMBLE_MANUAL_HANDOFF`` and the exported
        paths in ``metadata["manual_handoff"]`` — honest about what happened.
        """
        self._validate_video(video_path)
        title, description = self._split_caption(caption)

        if not self.api_key:
            return self._hand_off(video_path, title=title, description=description, caption=caption)

        try:
            payload = await self.transport().upload(
                video_path,
                title=title,
                description=description,
                api_url=self.api_url,
                api_key=self.api_key,
                visibility=(visibility or "public"),
            )
        except Exception as exc:  # noqa: BLE001 - report the provider's answer, never a fake success
            logger.error("Rumble upload failed: %s", exc)
            return UploadResult(
                success=False,
                error=f"{RUMBLE_UPLOAD_CODE}: {str(exc)[:200]}",
                platform="rumble",
                retryable=True,
                metadata={"integration": "integration_uncertain", "mode": "api"},
            )

        post_id = None
        if isinstance(payload, dict):
            post_id = payload.get("id") or payload.get("video_id") or payload.get("slug")
        if not isinstance(post_id, str) or not post_id.strip():
            logger.error("Rumble upload response carried no video id; publication is unverified")
            return UploadResult(
                success=False,
                error=f"RUMBLE_UPLOAD_UNVERIFIED: response had no video id ({payload!r})",
                platform="rumble",
                retryable=False,
                metadata={"integration": "integration_uncertain", "mode": "api"},
            )

        url = None
        if isinstance(payload, dict):
            url = payload.get("url") or payload.get("video_url")
        if not url:
            url = f"https://rumble.com/v{post_id}.html"

        return UploadResult(
            success=True,
            post_id=post_id.strip(),
            post_url=str(url),
            platform="rumble",
            metadata={"integration": "integration_uncertain", "mode": "api", "raw": payload},
        )

    # ── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _split_caption(caption: str) -> tuple[str, str]:
        lines = (caption or "").split("\n", 1)
        title = (lines[0] or "New video").strip()[:100]
        description = lines[1] if len(lines) > 1 else (caption or "")
        return title, description

    def _hand_off(
        self,
        video_path: Path,
        *,
        title: str,
        description: str,
        caption: str,
    ) -> UploadResult:
        """Export the rendition + a caption sidecar and report the handoff."""
        target_dir = self.handoff_dir
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            exported = target_dir / video_path.name
            if video_path.resolve() != exported.resolve():
                shutil.copy2(video_path, exported)
            sidecar = exported.with_suffix(".md")
            sidecar.write_text(
                f"# {title}\n\n{description}\n\n---\n"
                f"Upload to: {self.upload_page_url}\n"
                f"Caption as given:\n\n{caption}\n",
                encoding="utf-8",
            )
            record = {
                "video": str(exported),
                "caption_file": str(sidecar),
                "upload_page_url": self.upload_page_url,
                "title": title,
                "integration": "integration_uncertain",
            }
            (target_dir / f"{exported.stem}.handoff.json").write_text(
                json.dumps(record, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            logger.error("Rumble handoff export failed: %s", exc)
            return UploadResult(
                success=False,
                error=f"{RUMBLE_HANDOFF_CODE}: could not export the rendition to {target_dir}: {exc}",
                platform="rumble",
                retryable=True,
                metadata={"integration": "integration_uncertain", "mode": "manual_handoff"},
            )

        logger.info("Rumble handoff exported to %s (nothing uploaded)", exported)
        return UploadResult(
            success=False,
            error=(
                f"{RUMBLE_HANDOFF_CODE}: no Rumble API key configured, so nothing was uploaded. "
                f"The rendition was exported to {exported} with a caption file "
                f"({sidecar.name}); upload it at {self.upload_page_url}, or set "
                f"accounts.rumble.api_key to publish through the partner API."
            ),
            platform="rumble",
            retryable=False,
            metadata={
                "integration": "integration_uncertain",
                "mode": "manual_handoff",
                "manual_handoff": record,
            },
        )

    async def delete(self, post_id: str, *, soft: bool = False, visibility: str | None = None) -> DeleteResult:
        """Rumble's partner API exposes no delete in the contract xPST relies on.

        Reported as ``PENDING`` (a human action), not pretended deleted — the
        same honesty the upload path owes.
        """
        return DeleteResult(
            outcome=DeleteOutcome.PENDING,
            platform="rumble",
            post_id=post_id,
            detail="Rumble delete is a manual action in the Rumble Studio; xPST does not remove it.",
        )


PlatformRegistry.register("rumble", RumbleUploader)
