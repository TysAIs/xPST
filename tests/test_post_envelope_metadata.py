"""The post envelope must carry the uploader's verifiable metadata.

Card ``t_da4b72ab``. Before this file existed, every surface (CLI
``xpst post --json``, MCP ``xpst_post``, ``POST /api/post``, the dashboard)
reported a green post and nothing else: ``success``/``published`` plus an id or
a URL. The verifiable facts — how many carousel items published, in what order,
which tweet ids the thread produced — lived in ``UploadResult.metadata`` and
were dropped by the serializer.

That is the same fabricated-success class as the silent stitch, one layer up:
"a photo carousel must be a carousel with all N items in order" is only
checkable by the caller if the response says how many items published, in what
order.

Properties asserted here:

1. a real Instagram carousel / X thread result reaches the envelope with
   ``carousel_items`` / ``thread_items`` / ``item_order`` / ``tweet_ids`` intact;
2. the CLI ``post --json`` surface and the ``POST /api/post`` response both
   carry it (so an agent can verify a post from the response alone);
3. ``metadata`` is additive only — a destination that produced no upload, and a
   failure whose uploader reported nothing, gain no metadata key, and no
   failure is ever upgraded to success.

Fixtures are generated image headers and mocked platform clients: no network,
no personal media, no ffmpeg.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from xpst.cli import main as cli_main
from xpst.config import XPSTConfig
from xpst.engine import CrossPostEngine, CrossPostResult
from xpst.platforms.base import UploadResult
from xpst.platforms.x import XUploader
from xpst.services.post_service import serialize_post_attempt

from . import test_dashboard_first_run_api as dashboard_api
from .test_carousel_correctness import (
    _album_client,
    _carousel,
    _config,
    _instagram_uploader,
)


@pytest.fixture(autouse=True)
def _api_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bare-router app reads the API token from the env override.

    ``dashboard_api._open_app`` sends that module's token, so the env override
    has to carry exactly that value or every mutating route 401s.
    """
    monkeypatch.setenv("XPST_API_TOKEN", dashboard_api.API_TOKEN)


# ---------------------------------------------------------------------------
# (1) A real adapter's metadata reaches the envelope
# ---------------------------------------------------------------------------


def _instagram_carousel_result(tmp_path: Path, count: int = 3, code: str = "ENV1") -> CrossPostResult:
    """Drive the REAL engine -> Instagram carousel path with a mocked client."""
    config = _config(tmp_path)
    engine = CrossPostEngine(config)
    engine.upload_service.anti_bot = None
    engine._platforms["instagram"] = _instagram_uploader(config, client=_album_client(code=code))
    return asyncio.run(engine.post_manual_carousel(_carousel(tmp_path, count), "caption", ["instagram"]))


def test_engine_carousel_metadata_survives_into_the_envelope(tmp_path: Path) -> None:
    """N images -> envelope says N items, in request order. Not just 'green'."""
    result = _instagram_carousel_result(tmp_path, 3)
    expected_order = [f"xpst-carousel-{index}.jpg" for index in (1, 2, 3)]

    envelope = serialize_post_attempt(
        requested=["instagram"],
        results=dict(result.results),
        content_type="carousel",
    )

    row = envelope["destinations"][0]
    assert row["platform"] == "instagram"
    assert row["success"] is True
    assert row["metadata"]["carousel_items"] == 3
    assert row["metadata"]["item_order"] == expected_order


def test_envelope_item_count_matches_what_was_posted(tmp_path: Path) -> None:
    """A 4-item carousel must not report 3: the count is the adapter's, not a guess."""
    result = _instagram_carousel_result(tmp_path, 4, code="ENV4")

    row = serialize_post_attempt(
        requested=["instagram"], results=dict(result.results), content_type="carousel"
    )["destinations"][0]

    assert row["metadata"]["carousel_items"] == len(_carousel(tmp_path, 4))
    assert row["metadata"]["item_order"] == [f"xpst-carousel-{index}.jpg" for index in (1, 2, 3, 4)]


def test_x_thread_metadata_reaches_the_envelope(tmp_path: Path) -> None:
    """An X thread reports its item count, order and tweet ids — not just a URL."""
    config = _config(tmp_path)
    config.x.auth_mode = "cookies"
    client = MagicMock()
    client.create_tweet = AsyncMock(side_effect=[MagicMock(id=str(100 + index)) for index in range(3)])
    client.upload_media = AsyncMock(side_effect=["m1", "m2", "m3"])
    uploader = XUploader(config)
    uploader._get_client = AsyncMock(return_value=client)
    items = _carousel(tmp_path, 3)

    upload = asyncio.run(uploader.upload_carousel(items, "caption"))
    assert upload.success is True, upload.error

    row = serialize_post_attempt(
        requested=["x"], results={"x": upload}, content_type="thread"
    )["destinations"][0]

    assert row["metadata"]["thread_items"] == 3
    assert row["metadata"]["item_order"] == [path.name for path in items]
    assert row["metadata"]["tweet_ids"] == ["100", "101", "102"]


# ---------------------------------------------------------------------------
# (2) The additive rule: never invent metadata, never upgrade a failure
# ---------------------------------------------------------------------------


def test_destination_with_no_upload_gains_no_metadata_key(tmp_path: Path) -> None:
    """A requested destination that produced nothing is a failure with no metadata."""
    envelope = serialize_post_attempt(requested=["instagram"], results={}, content_type="carousel")

    row = envelope["destinations"][0]
    assert row["success"] is False
    assert row["published"] is False
    assert row["attempted"] is True
    assert "metadata" not in row, "no uploader reported anything, so there is nothing to report"


def test_failure_without_uploader_metadata_gains_no_metadata_key() -> None:
    """The key is omitted, not filled with an empty dict or an invented shape."""
    upload = UploadResult(success=False, error="INSTAGRAM_REJECTED: nope", platform="instagram")

    row = serialize_post_attempt(
        requested=["instagram"], results={"instagram": upload}, content_type="carousel"
    )["destinations"][0]

    assert row["success"] is False
    assert row["error"] == "INSTAGRAM_REJECTED: nope"
    assert "metadata" not in row


def test_a_metadata_bearing_failure_stays_a_failure() -> None:
    """Real diagnostics travel with a failed row; success is not upgraded by them."""
    upload = UploadResult(
        success=False,
        error="YOUTUBE_UPLOAD_FAILED: quota",
        platform="youtube",
        retryable=False,
        metadata={"content_type": "carousel", "items": 3, "unsupported": True},
    )

    row = serialize_post_attempt(
        requested=["youtube"], results={"youtube": upload}, content_type="carousel"
    )["destinations"][0]

    assert row["success"] is False
    assert row["published"] is False
    assert row["retryable"] is False
    assert row["metadata"]["unsupported"] is True
    assert row["metadata"]["items"] == 3


def test_envelope_metadata_is_a_copy_not_the_uploaders_dict() -> None:
    """Mutating the response must not mutate the uploader's own result."""
    upload = UploadResult(
        success=True,
        post_id="1",
        post_url="https://example.invalid/p/1",
        platform="instagram",
        metadata={"carousel_items": 3, "item_order": ["a.jpg", "b.jpg", "c.jpg"]},
    )

    row = serialize_post_attempt(requested=["instagram"], results={"instagram": upload})["destinations"][0]
    row["metadata"]["item_order"].append("tampered.jpg")

    assert upload.metadata["item_order"] == ["a.jpg", "b.jpg", "c.jpg"]


# ---------------------------------------------------------------------------
# (3) CLI: `xpst post --json` reads the item count and order from the output
# ---------------------------------------------------------------------------


def _cli_config(tmp_path: Path) -> XPSTConfig:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.video.download_dir = str(tmp_path / "downloads")
    (tmp_path / "downloads").mkdir(parents=True, exist_ok=True)
    config.monitoring.log_file = str(tmp_path / "xpst.log")
    config.instagram.enabled = True
    return config


def test_cli_post_json_reports_carousel_items_and_order(tmp_path: Path) -> None:
    """The carousel metadata is readable from the CLI's own JSON output.

    The engine is real (only the instagrapi client is mocked), so this exercises
    the whole CLI post path: engine -> Instagram adapter -> ``_result_to_dict``.
    """
    items = _carousel(tmp_path, 3)
    config = _cli_config(tmp_path)

    def _real_engine(_config: Any):  # noqa: ANN202
        """A real engine whose Instagram client is mocked — no network, real pipeline."""
        engine = CrossPostEngine(config)
        engine.upload_service.anti_bot = None
        engine._platforms["instagram"] = _instagram_uploader(config, client=_album_client(code="CLI1"))
        return engine

    with (
        patch("xpst.cli.load_config", return_value=config),
        patch("xpst.cli.CrossPostEngine", _real_engine),
    ):
        result = CliRunner().invoke(
            cli_main,
            [
                "post",
                "-v",
                str(items[0]),
                "-v",
                str(items[1]),
                "-v",
                str(items[2]),
                "-c",
                "caption",
                "-p",
                "instagram",
                "--json",
            ],
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    entry = payload["platforms"]["instagram"]
    assert entry["success"] is True
    assert entry["metadata"]["carousel_items"] == 3
    assert entry["metadata"]["item_order"] == [path.name for path in items]


def test_cli_post_json_omits_metadata_for_a_metadata_less_failure(tmp_path: Path) -> None:
    """The additive rule holds on the CLI surface too."""
    items = _carousel(tmp_path, 3)
    config = _cli_config(tmp_path)
    fake_engine = MagicMock()
    fake_engine.post_manual_carousel = AsyncMock(
        return_value=CrossPostResult(
            video_id="carousel_cli",
            caption="caption",
            results={
                "instagram": UploadResult(success=False, error="INSTAGRAM_REJECTED: nope", platform="instagram")
            },
            all_success=False,
            partial_success=False,
        )
    )

    with (
        patch("xpst.cli.load_config", return_value=config),
        patch("xpst.cli.CrossPostEngine", return_value=fake_engine),
    ):
        result = CliRunner().invoke(
            cli_main,
            ["post", "-v", str(items[0]), "-v", str(items[1]), "-v", str(items[2]), "-c", "caption",
             "-p", "instagram", "--json"],
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    entry = payload["platforms"]["instagram"]
    assert entry["success"] is False
    assert "metadata" not in entry


# ---------------------------------------------------------------------------
# (4) MCP: the agent surface already reports it — pin that so it cannot regress
# ---------------------------------------------------------------------------


def test_mcp_post_envelope_carries_carousel_metadata(tmp_path: Path) -> None:
    """The MCP ``xpst_post`` payload must keep the same verifiable facts.

    ``_serialize_result`` uses ``asdict(UploadResult)``, which already includes
    ``metadata``; there is no serializer change here, but an agent reading the
    MCP envelope is exactly the caller this card exists for, so the property is
    pinned rather than assumed.
    """
    mcp_server = pytest.importorskip("xpst.mcp.server")

    items = _carousel(tmp_path, 3)
    adapter_upload = asyncio.run(
        _instagram_uploader(_config(tmp_path), client=_album_client(code="MCP1")).upload_carousel(items, "caption")
    )
    assert adapter_upload.success is True, adapter_upload.error

    engine = MagicMock()
    engine.post_manual_carousel = AsyncMock(
        return_value=CrossPostResult(
            video_id="carousel_mcp",
            caption="caption",
            results={"instagram": adapter_upload},
            all_success=True,
            partial_success=True,
        )
    )

    result = asyncio.run(
        mcp_server._handle_post(
            engine,
            {
                "video_path": str(items[0]),
                "carousel_paths": [str(path) for path in items[1:]],
                "caption": "caption",
                "platforms": ["instagram"],
            },
        )
    )

    payload = json.loads(result.content[0].text)
    entry = payload["results"]["instagram"]
    assert entry["success"] is True
    assert entry["metadata"]["carousel_items"] == 3
    assert entry["metadata"]["item_order"] == [path.name for path in items]


# ---------------------------------------------------------------------------
# (5) HTTP: POST /api/post carries the same facts
# ---------------------------------------------------------------------------


def _image_carousel(tmp_path: Path, count: int = 3) -> list[str]:
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    return [str(path) for path in _carousel(media, count)]


def _carousel_config_dir(tmp_path: Path) -> str:
    session = tmp_path / "ig-session.json"
    session.write_text("{}", encoding="utf-8")
    return dashboard_api._post_config(
        tmp_path,
        instagram={
            "enabled": True,
            "auth_mode": "session",
            "session_file": str(session),
            "username": "fixture-user",
        },
    )


def _real_engine_factory(uploader_client: Any):
    """A real engine whose Instagram client is mocked — no network, real pipeline."""

    def factory(config: Any):  # noqa: ANN202
        engine = CrossPostEngine(config)
        engine.upload_service.anti_bot = None
        engine._platforms["instagram"] = _instagram_uploader(config, client=uploader_client)
        return engine

    return factory


def test_api_post_reports_carousel_item_count_and_order(tmp_path: Path) -> None:
    """A caller of POST /api/post can verify all N items landed, in order."""
    config_dir = _carousel_config_dir(tmp_path)
    media = _image_carousel(tmp_path, 3)

    with dashboard_api._open_app(
        tmp_path, config_dir, engine_factory=_real_engine_factory(_album_client(code="API1"))
    ) as client:
        response = client.post(
            "/api/post",
            json={
                "media_paths": media,
                "caption": "carousel caption",
                "platforms": ["instagram"],
                "content_type": "carousel",
            },
        )

    data = response.json()
    assert response.status_code == 200, data
    assert data["ok"] is True, data.get("blockers")
    row = next(item for item in data["destinations"] if item["platform"] == "instagram")
    assert row["success"] is True
    assert row["metadata"]["carousel_items"] == 3
    assert row["metadata"]["item_order"] == [Path(path).name for path in media]


def test_api_post_failure_row_carries_no_invented_metadata(tmp_path: Path) -> None:
    """A destination that uploaded nothing reports a failure and no metadata."""
    config_dir = _carousel_config_dir(tmp_path)
    media = _image_carousel(tmp_path, 3)

    class RefusingEngine:
        async def post_manual_carousel(self, media_paths, caption, platforms=None):  # noqa: ANN001, ANN201
            return CrossPostResult(
                video_id="carousel_api",
                caption=caption,
                results={
                    platform: UploadResult(
                        success=False,
                        error=f"INSTAGRAM_REJECTED: refused for {platform}",
                        platform=platform,
                    )
                    for platform in (platforms or [])
                },
                all_success=False,
                partial_success=False,
            )

        async def post_manual(self, *args: Any, **kwargs: Any):  # noqa: ANN201
            raise AssertionError("a carousel request must not take the video path")

    with dashboard_api._open_app(tmp_path, config_dir, engine_factory=lambda _config: RefusingEngine()) as client:
        data = client.post(
            "/api/post",
            json={
                "media_paths": media,
                "caption": "carousel caption",
                "platforms": ["instagram"],
                "content_type": "carousel",
            },
        ).json()

    row = data["destinations"][0]
    assert data["ok"] is False
    assert row["success"] is False
    assert "metadata" not in row


def test_api_dry_run_invents_no_metadata(tmp_path: Path) -> None:
    """A dry run attempts nothing, so no destination may carry uploader metadata."""
    config_dir = _carousel_config_dir(tmp_path)
    media = _image_carousel(tmp_path, 3)

    with dashboard_api._open_app(
        tmp_path, config_dir, engine_factory=_real_engine_factory(_album_client(code="API1"))
    ) as client:
        data = client.post(
            "/api/post",
            json={
                "media_paths": media,
                "caption": "carousel caption",
                "platforms": ["instagram"],
                "content_type": "carousel",
                "dry_run": True,
            },
        ).json()

    assert data["dry_run"] is True
    for row in data["destinations"]:
        assert row["attempted"] is False
        assert row["success"] is None
        assert "metadata" not in row
