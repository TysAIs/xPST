"""Regression: Direct Post consults creator_info/query before init (t_e6d94088).

TikTok's Content Posting API forces an UNAUDITED client to private privacy:
hardcoding ``privacy_level: PUBLIC_TO_EVERYONE`` at init means an unaudited
dev app's first publish dies with
``403 unaudited_client_can_only_post_to_private_accounts``. The uploader must
ask ``POST /v2/post/publish/creator_info/query/`` first and post at a level
the client is actually allowed to use.

These tests pin:
* unaudited options (no PUBLIC_TO_EVERYONE) downgrade init to SELF_ONLY;
* an audited response keeps PUBLIC_TO_EVERYONE;
* a failing/garbage query is fail-open (init still runs, public) so the
  pre-check can never block posting outright;
* the resolved level is cached (6 req/min rate limit) and reported in
  result metadata;
* 'always' draft mode skips Direct Post entirely — no query, no init.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from xpst.config import XPSTConfig
from xpst.platforms.tiktok import TikTokUploader

if TYPE_CHECKING:
    from pathlib import Path


def _uploader() -> TikTokUploader:
    config = XPSTConfig()
    config.tiktok.client_key = "key"
    config.tiktok.client_secret = "secret"  # noqa: S105 - test fixture
    config.tiktok.access_token = "token"  # noqa: S105 - test fixture
    return TikTokUploader(config)


def _video(tmp_path: Path) -> Path:
    p = tmp_path / "v.mp4"
    p.write_bytes(b"0" * 1024)
    return p


def _resp(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("POST", "https://open.tiktokapis.com/v2/post/publish/video/init/"),
    )


def _client(responses: list[httpx.Response]) -> tuple[MagicMock, MagicMock]:
    """One shared FIFO queue across client.post (query/init/status) and .put.

    Returns (client, context_manager): assert against ``client`` and patch
    ``httpx.AsyncClient`` with the context manager.
    """
    client = MagicMock()
    queue = list(responses)

    async def _next(*_a, **_k):
        return queue.pop(0)

    client.post = AsyncMock(side_effect=_next)
    client.put = AsyncMock(side_effect=_next)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return client, cm


def _creator_info(levels: list[str]) -> httpx.Response:
    return _resp({"data": {"privacy_level_options": [{"privacy_level": lv} for lv in levels]}})


_INIT = _resp({"data": {"publish_id": "pub-precheck", "upload_url": "https://up.example/v"}})
_STATUS_PROCESSING = _resp({"data": {"status": "PROCESSING_UPLOAD"}})


def _init_post_info(client: MagicMock) -> dict:
    """Return the post_info dict of the /video/init/ call in a mock transcript."""
    for call in client.post.await_args_list:
        url = call.args[0] if call.args else call.kwargs.get("url", "")
        if "video/init" in str(url):
            return call.kwargs["json"]["post_info"]
    raise AssertionError("no /video/init/ call was made")


@pytest.mark.asyncio
async def test_unaudited_options_downgrade_init_to_self_only(tmp_path: Path) -> None:
    """No PUBLIC_TO_EVERYONE offered -> init must post at SELF_ONLY, not 403."""
    client, cm = _client([
        _creator_info(["SELF_ONLY", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR"]),
        _INIT,
        _resp({}),  # PUT to upload_url
        _STATUS_PROCESSING,
    ])
    with patch("httpx.AsyncClient", return_value=cm):
        result = await _uploader().upload(_video(tmp_path), "cap")

    assert _init_post_info(client)["privacy_level"] == "SELF_ONLY"
    assert result.metadata["privacy_level"] == "SELF_ONLY"


@pytest.mark.asyncio
async def test_audited_options_keep_public(tmp_path: Path) -> None:
    client, cm = _client([
        _creator_info(["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"]),
        _INIT,
        _resp({}),  # PUT to upload_url
        _STATUS_PROCESSING,
    ])
    with patch("httpx.AsyncClient", return_value=cm):
        result = await _uploader().upload(_video(tmp_path), "cap")

    assert _init_post_info(client)["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert result.metadata["privacy_level"] == "PUBLIC_TO_EVERYONE"


@pytest.mark.asyncio
async def test_query_error_is_fail_open_and_public(tmp_path: Path) -> None:
    """A broken pre-check (404 here) must NOT block posting — init still runs public."""
    client, cm = _client([
        _resp({"error": {"code": "unknown_endpoint"}}, status=404),
        _INIT,
        _resp({}),  # PUT to upload_url
        _STATUS_PROCESSING,
    ])
    with patch("httpx.AsyncClient", return_value=cm):
        result = await _uploader().upload(_video(tmp_path), "cap")

    assert _init_post_info(client)["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert "TIKTOK_INIT_ERROR" not in (result.error or "")


@pytest.mark.asyncio
async def test_query_garbage_body_is_fail_open(tmp_path: Path) -> None:
    client, cm = _client([_resp({"data": {}}), _INIT, _resp({}), _STATUS_PROCESSING])
    with patch("httpx.AsyncClient", return_value=cm):
        await _uploader().upload(_video(tmp_path), "cap")

    assert _init_post_info(client)["privacy_level"] == "PUBLIC_TO_EVERYONE"


@pytest.mark.asyncio
async def test_query_happens_once_per_uploader(tmp_path: Path) -> None:
    """6 req/min rate limit: the resolved level is cached across uploads."""
    client, cm = _client([
        _creator_info(["SELF_ONLY"]), _INIT, _resp({}), _STATUS_PROCESSING,
        # second upload: no second query in the queue — it must reuse the cache
        _INIT, _resp({}), _STATUS_PROCESSING,
    ])
    uploader = _uploader()
    with patch("httpx.AsyncClient", return_value=cm):
        await uploader.upload(_video(tmp_path), "cap")
        await uploader.upload(_video(tmp_path), "cap")

    queries = [c for c in client.post.await_args_list if "creator_info" in str(c.args[0])]
    assert len(queries) == 1


@pytest.mark.asyncio
async def test_always_draft_mode_skips_query(tmp_path: Path) -> None:
    """Draft-only mode never Direct Posts, so it must not spend a query call."""
    uploader = _uploader()
    uploader.config.tiktok.draft_mode = "always"
    client, cm = _client([
        _resp({"data": {"publish_id": "draft-1", "upload_url": "https://up.example/v"}}),
        _resp({}),  # PUT to upload_url
    ])
    with patch("httpx.AsyncClient", return_value=cm):
        result = await uploader.upload(_video(tmp_path), "cap")

    queries = [c for c in client.post.await_args_list if "creator_info" in str(c.args[0])]
    assert queries == []
    assert result.metadata.get("draft_mode") is True
