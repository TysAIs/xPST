"""OPT-IN live lane for the TikTok browser-native publisher.

Skipped unless ``XPST_LIVE_TIKTOK_BROWSER=1``. When set, it runs the REAL
browser publish against the account the cookie jar belongs to, with a
distinct caption, then deletes the post (creator endpoint with a Studio row-
menu fallback, verified absent via the manage item_list) and asserts both the
publish receipt and the deletion. Keep this out of CI (no credentials); run it
manually per release, same policy as the canary protocol on the board.

    XPST_LIVE_TIKTOK_BROWSER=1 \
      XPST_LIVE_TIKTOK_VIDEO=/path/to/canary.mp4 \
      pytest tests/test_tiktok_browser_live.py -q
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("XPST_LIVE_TIKTOK_BROWSER") != "1",
    reason="opt-in live lane: set XPST_LIVE_TIKTOK_BROWSER=1 (needs a logged-in TikTok cookie jar)",
)


@pytest.mark.asyncio
async def test_live_browser_publish_and_delete():
    from xpst.config import XPSTConfig
    from xpst.platforms.base import UploadOutcome
    from xpst.platforms.tiktok import TikTokUploader

    video = os.getenv("XPST_LIVE_TIKTOK_VIDEO", "")
    assert video and Path(video).exists(), "set XPST_LIVE_TIKTOK_VIDEO to an existing mp4"

    config = XPSTConfig.load()
    config.tiktok.enabled = True
    config.tiktok.publish_mode = "browser_only"
    uploader = TikTokUploader(config)

    caption = f"xPST live-lane browser publish {int(time.time())} #xpst"
    result = await uploader.upload(Path(video), caption)
    assert result.success, f"live browser publish failed: {result.error}"
    assert result.outcome is UploadOutcome.PUBLISHED
    assert result.metadata.get("route") == "tiktok_browser"
    assert result.post_id and result.post_url

    # Receipt must be reachable
    cleanup = os.getenv("XPST_LIVE_TIKTOK_KEEP") != "1"
    if cleanup:
        delete = await uploader.delete(str(result.post_id))
        assert delete.ok, f"delete did not confirm: {delete.detail}"
