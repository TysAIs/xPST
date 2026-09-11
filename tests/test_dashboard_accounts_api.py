"""Accounts capability API contract tests."""

from pathlib import Path

from fastapi.testclient import TestClient

from xpst.config import XPSTConfig
from xpst.dashboard.server import _create_app


def test_health_status_keeps_role_aware_capabilities_for_accounts(tmp_path: Path) -> None:
    XPSTConfig().save(str(tmp_path / "config.yaml"))
    app = _create_app(config_dir=str(tmp_path))

    with TestClient(app) as client:
        response = client.get("/api/providers")

    assert response.status_code == 200
    providers = response.json()["by_name"]
    assert providers["tiktok"]["roles"] == ["source", "video_destination", "analytics"]
    assert providers["messenger"]["roles"] == ["messaging"]
    assert "video_destination" not in providers["messenger"]["roles"]
    assert set(providers["tiktok"]["role_status"]) == {"source", "video_destination", "analytics"}
    assert all("ready" in role for role in providers["tiktok"]["role_status"].values())


def test_accounts_catalog_marks_blocked_tiktok_destination_without_claiming_ready(tmp_path: Path) -> None:
    config = XPSTConfig()
    config.tiktok.enabled = True
    config.tiktok.sandbox = True
    config.save(str(tmp_path / "config.yaml"))
    app = _create_app(config_dir=str(tmp_path))

    with TestClient(app) as client:
        provider = client.get("/api/providers").json()["by_name"]["tiktok"]

    destination = provider["role_status"]["video_destination"]
    assert destination["state"] in {"blocked_external_review", "unconfigured", "degraded"}
    assert destination["ready"] is False
