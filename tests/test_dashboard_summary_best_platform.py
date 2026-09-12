"""The summary endpoint must not invent a placeholder for missing data.

`best_platform` used to come back as an em dash ("—") when nothing had been
posted, which is indistinguishable from a platform named "—". The Home screen's
own "None yet" fallback was therefore dead code, and the dashboard rendered an
em dash that reads as a broken value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst.config import XPSTConfig
from xpst.dashboard.api import create_api_router

if TYPE_CHECKING:
    from pathlib import Path


def _client(tmp_path: Path) -> TestClient:
    XPSTConfig().save(str(tmp_path / "config.yaml"))
    app = FastAPI()
    app.include_router(create_api_router(str(tmp_path)))
    return TestClient(app)


def test_summary_reports_no_best_platform_as_null(tmp_path: Path) -> None:
    payload = _client(tmp_path).get("/api/summary").json()

    assert payload["best_platform"] is None, (
        f"expected null for 'no data', got {payload['best_platform']!r}"
    )
    assert payload["total_posts"] == 0


def test_summary_reports_a_real_platform_once_posts_exist(tmp_path: Path) -> None:
    from xpst.state import StateManager

    state = StateManager(str(tmp_path))
    state.mark_video_posted("vid-1", "youtube", post_id="p1", content_hash="h1")
    state.save()

    payload = _client(tmp_path).get("/api/summary").json()

    assert payload["best_platform"] == "youtube"
    assert payload["total_posts"] == 1
