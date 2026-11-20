"""Home readiness API contract tests."""

from pathlib import Path

from fastapi.testclient import TestClient

from xpst.config import XPSTConfig
from xpst.dashboard.server import _create_app


def test_health_status_exposes_next_action_for_unconfigured_destination(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    XPSTConfig().save(str(config_path))
    app = _create_app(config_dir=str(tmp_path))

    with TestClient(app) as client:
        response = client.get("/api/health-status")

    assert response.status_code == 200
    data = response.json()
    assert data["next_action"]["kind"] in {"connect", "review", "configure"}
    assert data["next_action"]["role"] in {"source", "video_destination", "messaging", "analytics"}
    assert data["readiness"]["ready"] is False


def test_health_status_does_not_offer_create_post_when_destination_is_unready(tmp_path: Path) -> None:
    XPSTConfig().save(str(tmp_path / "config.yaml"))
    app = _create_app(config_dir=str(tmp_path))

    with TestClient(app) as client:
        data = client.get("/api/health-status").json()

    assert data["can_create_post"] is False
    assert data["readiness"]["blockers"]
