"""Which TikTok publishing route is configured, and its adapter.

TikTok is the one destination with more than one real publishing path, and the
path is a configuration decision:

* ``provider: local`` (default) keeps xPST's own routes — the Content Posting
  API (``platforms/tiktok.py``), the opt-in browser-native publisher and the
  inbox-draft fallback.
* ``provider: zernio`` rents an audited app: video is published through the
  aggregator adapter in :mod:`xpst.platforms.zernio_tiktok`.

Every surface that needs an uploader for TikTok goes through
:func:`build_tiktok_uploader` instead of importing a class directly, so the
engine, ``xpst auth status``, readback and preflight cannot disagree about which
route is live. The platform key stays ``tiktok`` in both cases: state, quotas,
content limits and analytics are keyed on the platform name, and a "zernio"
platform would fork all of them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xpst.config import XPSTConfig
    from xpst.platforms.base import PlatformUploader

#: The two supported providers for the TikTok destination.
LOCAL_PROVIDER = "local"
ZERNIO_PROVIDER = "zernio"
SUPPORTED_PROVIDERS: tuple[str, ...] = (LOCAL_PROVIDER, ZERNIO_PROVIDER)


def tiktok_provider(config: XPSTConfig) -> str:
    """The configured TikTok publishing provider, normalised.

    An unknown value is returned as-is: :func:`build_tiktok_uploader` refuses it
    loudly rather than silently falling back to a different route, because a
    typo in ``provider`` must not quietly repoint a destination.
    """
    account = getattr(config, "tiktok", None)
    raw = str(getattr(account, "provider", LOCAL_PROVIDER) or LOCAL_PROVIDER)
    return raw.strip().lower()


def build_tiktok_uploader(config: XPSTConfig) -> PlatformUploader:
    """Build the uploader for the configured TikTok route.

    Raises:
        ValueError: when ``accounts.tiktok.provider`` is neither ``local`` nor
            ``zernio``. Callers that build uploaders defensively already catch
            and log; a caller that does not gets a clear failure instead of the
            wrong route.
    """
    provider = tiktok_provider(config)
    if provider == ZERNIO_PROVIDER:
        from xpst.platforms.zernio_tiktok import ZernioTikTokUploader

        return ZernioTikTokUploader(config)
    if provider == LOCAL_PROVIDER:
        from xpst.platforms.tiktok import TikTokUploader

        return TikTokUploader(config)
    raise ValueError(
        f"accounts.tiktok.provider is {provider!r}; choose one of: {', '.join(SUPPORTED_PROVIDERS)}."
    )


__all__ = [
    "LOCAL_PROVIDER",
    "SUPPORTED_PROVIDERS",
    "ZERNIO_PROVIDER",
    "build_tiktok_uploader",
    "tiktok_provider",
]
