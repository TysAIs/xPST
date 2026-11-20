"""Ask a platform whether a recorded post actually exists (readback).

The engine records a platform id when an upload returns. That is evidence the
*submission* happened, not proof the post is live: a container can be accepted
and still be processing, and a platform can reject a post after returning an id.
``docs/PHONE-FIRST-UX.md`` §2 step 6 therefore requires a verification state per
destination, and §2.2 says "submitted, unconfirmed" is a first-class outcome.

This module is the one implementation of that readback. It asks the platform
adapter (`PlatformUploader.readback`, base contract in
:mod:`xpst.platforms.base`) and maps the answer into the plan's vocabulary:

* ``verified`` — the platform returned the post when asked;
* ``submitted_unconfirmed`` — no adapter could answer (or the read failed), so
  the recorded id stands but is *not* claimed as confirmed;
* ``not_found`` — the platform answered and the post is gone (a real problem).

It is network-only and opt-in: a preview, a dry run and a plan readback without
``verify`` never call it.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from xpst.utils.logger import get_logger

if TYPE_CHECKING:
    from xpst.config import XPSTConfig

logger = get_logger(__name__)

VERIFICATION_VERIFIED = "verified"
VERIFICATION_UNCONFIRMED = "submitted_unconfirmed"
VERIFICATION_NOT_FOUND = "not_found"


def _uploader(config: XPSTConfig, platform: str) -> Any | None:
    """Build the adapter for one platform, or ``None`` when it has none."""
    from xpst.platforms.base import PlatformRegistry

    PlatformRegistry.auto_discover()
    try:
        return PlatformRegistry.get(platform, config)
    except KeyError:
        return None


def verify_post(config: XPSTConfig, platform: str, post_id: str) -> dict[str, Any]:
    """Ask ``platform`` whether ``post_id`` exists; never raises.

    Returns ``{verification, state, url, reason}``. An adapter that is missing,
    unsupported, or fails to answer yields ``submitted_unconfirmed`` with the
    reason — the post is never silently reported as verified or as failed.
    """
    name = str(platform or "").strip().lower()
    post = str(post_id or "").strip()
    if not name or not post:
        return {
            "verification": VERIFICATION_UNCONFIRMED,
            "state": "unknown",
            "url": None,
            "reason": "no platform id to verify",
        }
    uploader = _uploader(config, name)
    if uploader is None:
        return {
            "verification": VERIFICATION_UNCONFIRMED,
            "state": "unsupported",
            "url": None,
            "reason": f"{name} has no adapter to read the post back with",
        }
    try:
        result = asyncio.run(uploader.readback(post))
    except RuntimeError:
        # A caller inside a running loop: run the read in its own thread.
        box: dict[str, Any] = {}

        def _run() -> None:
            box["value"] = asyncio.run(uploader.readback(post))

        import threading

        thread = threading.Thread(target=_run, name=f"xpst-readback-{name}", daemon=True)
        thread.start()
        thread.join()
        result = box.get("value")
    except Exception as exc:  # noqa: BLE001 - a readback failure is never a publish failure
        logger.warning("Readback for %s/%s failed: %s", name, post, exc)
        return {
            "verification": VERIFICATION_UNCONFIRMED,
            "state": "unknown",
            "url": None,
            "reason": f"the readback could not be completed: {str(exc)[:200]}",
        }

    state = getattr(getattr(result, "state", None), "value", None) or "unknown"
    detail = getattr(result, "detail", None) or ""
    url = getattr(result, "url", None)
    if state == "verified":
        return {
            "verification": VERIFICATION_VERIFIED,
            "state": state,
            "url": url,
            "reason": detail or "the platform returned this post",
        }
    if state == "not_found":
        return {
            "verification": VERIFICATION_NOT_FOUND,
            "state": state,
            "url": url,
            "reason": detail or "the platform answered and did not find this post",
        }
    return {
        "verification": VERIFICATION_UNCONFIRMED,
        "state": state,
        "url": url,
        "reason": detail or "the platform could not confirm this post",
    }


__all__ = [
    "VERIFICATION_NOT_FOUND",
    "VERIFICATION_UNCONFIRMED",
    "VERIFICATION_VERIFIED",
    "verify_post",
]
