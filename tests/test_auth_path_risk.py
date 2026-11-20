"""Ban-risk advisories for unofficial auth paths (the "no bans" gate).

The product gate is cross-posting that respects each platform's rules. Two
shipped integrations are not a provider's official API: Instagram
``auth_mode: session`` (instagrapi / private mobile API) and X
``auth_mode: cookies`` (twikit / web session). These tests pin the contract that
made the risk visible at the moment of choice and in readiness reporting:

* the registry decides the verdict (official modes produce nothing);
* a platform DISABLED in config is a deliberate pause and never raises;
* acknowledgement is the operator's recorded choice — owner-only file, bound to
  the auth mode it was given for, and never able to hide the advisory;
* doctor, readiness and preflight carry the same advisory text and code, and
  none of them turns it into a failure that changes ``all_clear`` or an exit
  code.

No test touches the network or a real account.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst import auth_path_risk as apr
from xpst.cli import main
from xpst.config import XPSTConfig
from xpst.dashboard.api import create_api_router
from xpst.readiness import build_readiness_report
from xpst.services.route_plan import build_route_plan


def _config(
    tmp_path: Path,
    *,
    instagram_mode: str = "session",
    instagram_enabled: bool = True,
    x_mode: str = "cookies",
    x_enabled: bool = True,
) -> XPSTConfig:
    """A config with only the platforms under test enabled."""
    config = XPSTConfig(config_dir=str(tmp_path / "state"))
    Path(config.config_dir).mkdir(parents=True, exist_ok=True)
    config.youtube.enabled = False
    config.tiktok.enabled = False
    config.threads.enabled = False
    config.facebook.enabled = False
    config.messenger.enabled = False
    config.instagram.enabled = instagram_enabled
    config.instagram.auth_mode = instagram_mode
    config.instagram.session_file = str(tmp_path / "instagram_session.json")
    config.x.enabled = x_enabled
    config.x.auth_mode = x_mode
    config.x.cookies_file = str(tmp_path / "x_cookies.json")
    config.video.download_dir = str(tmp_path / "downloads")
    config.monitoring.log_file = str(tmp_path / "logs" / "xpst.log")
    return config


# ── The registry verdict ────────────────────────────────────────────────────


def test_session_mode_is_the_private_api_ban_risk(tmp_path: Path) -> None:
    risk = apr.auth_path_risk(_config(tmp_path), "instagram")

    assert risk is not None
    assert risk.ban_risk == apr.BAN_RISK_PRIVATE_API
    assert risk.advisory_code == apr.ADVISORY_CODE_PRIVATE_API
    assert risk.severity == "high"
    assert risk.auth_mode == "session"
    assert risk.official_mode == "graph_api"
    assert risk.switch_command == "xpst connect instagram"
    assert "xpst connect instagram" in risk.message
    assert risk.acknowledged is False
    assert risk.advisory()["code"] == apr.ADVISORY_CODE_PRIVATE_API


def test_cookie_mode_is_the_web_client_ban_risk(tmp_path: Path) -> None:
    risk = apr.auth_path_risk(_config(tmp_path), "x")

    assert risk is not None
    assert risk.ban_risk == apr.BAN_RISK_WEB_CLIENT
    assert risk.severity == "advisory"
    assert risk.official_mode == "api_v2"


@pytest.mark.parametrize(
    ("platform", "mode"),
    [("instagram", "graph_api"), ("x", "api_v2")],
)
def test_official_modes_produce_no_advisory(tmp_path: Path, platform: str, mode: str) -> None:
    config = _config(tmp_path, instagram_mode="graph_api", x_mode="api_v2")
    assert getattr(config, platform).auth_mode == mode
    assert apr.auth_path_risk(config, platform) is None


def test_official_by_construction_platforms_never_warn(tmp_path: Path) -> None:
    config = _config(tmp_path, instagram_mode="graph_api")
    for platform in ("youtube", "tiktok", "threads", "facebook", "messenger"):
        assert apr.auth_path_risk(config, platform) is None


def test_disabled_platform_is_a_pause_not_a_banner(tmp_path: Path) -> None:
    """The configured Meta pause must not raise: reported, never active."""
    config = _config(tmp_path, instagram_mode="session", instagram_enabled=False)

    assert apr.auth_path_risk(config, "instagram") is not None
    assert "instagram" not in {risk.platform for risk in apr.active_auth_path_risks(config)}
    assert apr.route_plan_advisory(config, "instagram") is None


# ── The operator's recorded choice ──────────────────────────────────────────


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_acknowledgement_is_recorded_owner_only(tmp_path: Path) -> None:
    config = _config(tmp_path)

    record = apr.record_acknowledgement(
        config, "instagram", surface="test", auth_mode="session", when="2026-01-01T00:00:00"
    )

    path = Path(config.config_dir) / apr.ACK_FILE_NAME
    assert path.exists()
    if not sys.platform.startswith("win"):
        assert _mode(path) == 0o600
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["acknowledgements"]["instagram"]["auth_mode"] == "session"
    assert stored["acknowledgements"]["instagram"]["surface"] == "test"
    assert stored["acknowledgements"]["instagram"]["ban_risk"] == apr.BAN_RISK_PRIVATE_API
    assert record["operator_choice"] is True

    risk = apr.auth_path_risk(config, "instagram")
    assert risk is not None
    assert risk.acknowledged is True
    assert risk.acknowledgement is not None
    assert risk.acknowledgement["acknowledged_at"] == "2026-01-01T00:00:00"
    # A recorded choice is stated, never hidden.
    assert "acknowledged this path" in risk.message


def test_acknowledgement_is_bound_to_the_mode_it_was_given_for(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = Path(config.config_dir) / apr.ACK_FILE_NAME
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "acknowledgements": {
                    "instagram": {
                        "platform": "instagram",
                        "auth_mode": "cookies",
                        "ban_risk": apr.BAN_RISK_WEB_CLIENT,
                        "acknowledged_at": "2026-01-01T00:00:00",
                        "surface": "hand-edited",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    risk = apr.auth_path_risk(config, "instagram")
    assert risk is not None
    assert risk.acknowledged is False


def test_malformed_acknowledgement_file_never_breaks_reporting(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (Path(config.config_dir) / apr.ACK_FILE_NAME).write_text("{not json", encoding="utf-8")

    risk = apr.auth_path_risk(config, "instagram")
    assert risk is not None and risk.acknowledged is False
    assert build_readiness_report(config).to_dict()["ready"] is not None


# ── readiness ───────────────────────────────────────────────────────────────


def _checks(config: XPSTConfig) -> dict[str, object]:
    return {check.id: check for check in build_readiness_report(config).checks}


def test_readiness_warns_for_an_unacknowledged_private_api_path(tmp_path: Path) -> None:
    config = _config(tmp_path, x_enabled=False)

    report = build_readiness_report(config)
    check = next(c for c in report.checks if c.id == "instagram_ban_risk")

    assert check.ok is False
    assert check.severity == "warning"
    assert check.details["ban_risk"] == apr.BAN_RISK_PRIVATE_API
    assert check.details["auth_mode"] == "session"
    assert check.details["acknowledged"] is False
    assert "instagram_ban_risk" in {c["id"] for c in report.to_dict()["warnings"]}
    assert report.summary != "Ready to post."


def test_readiness_drops_the_warning_once_the_choice_is_acknowledged(tmp_path: Path) -> None:
    config = _config(tmp_path, x_enabled=False)
    apr.record_acknowledgement(config, "instagram", surface="test")

    report = build_readiness_report(config)
    check = next(c for c in report.checks if c.id == "instagram_ban_risk")

    assert check.ok is True
    assert check.details["acknowledged"] is True
    assert "instagram_ban_risk" not in {c["id"] for c in report.to_dict()["warnings"]}


def test_readiness_is_unchanged_for_the_official_path(tmp_path: Path) -> None:
    config = _config(tmp_path, instagram_mode="graph_api", x_enabled=False)
    assert "instagram_ban_risk" not in _checks(config)


def test_readiness_reports_the_paused_platform_as_nothing_to_fix(tmp_path: Path) -> None:
    config = _config(tmp_path, instagram_enabled=False, x_enabled=False)
    assert "instagram_ban_risk" not in _checks(config)


def test_readiness_x_cookies_is_informational_so_the_summary_is_unchanged(
    tmp_path: Path,
) -> None:
    """The documented X default is reported, but does not nag the setup verdict."""
    config = _config(tmp_path, instagram_mode="graph_api", instagram_enabled=False)

    report = build_readiness_report(config)
    check = next(c for c in report.checks if c.id == "x_ban_risk")

    assert check.ok is True
    assert check.severity == "info"
    assert check.details["ban_risk"] == apr.BAN_RISK_WEB_CLIENT
    assert "x_ban_risk" not in {c["id"] for c in report.to_dict()["warnings"]}


# ── preflight / route plan ──────────────────────────────────────────────────


def test_preflight_destination_carries_the_advisory(tmp_path: Path) -> None:
    config = _config(tmp_path, x_enabled=False)
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"\x00" * 64)

    plan = build_route_plan(
        config,
        media_paths=[str(media)],
        caption="hello",
        content_type="video",
        requested=["instagram"],
        include_transform=False,
        include_readiness=False,
    )
    payload = plan.to_dict()
    destination = payload["destinations"][0]

    assert destination["state"] == "posted"  # the advisory never blocks
    codes = [warning["code"] for warning in destination["warnings"]]
    assert apr.ADVISORY_CODE_PRIVATE_API in codes
    message = next(
        w["message"] for w in destination["warnings"] if w["code"] == apr.ADVISORY_CODE_PRIVATE_API
    )
    assert "ban_risk: unofficial_private_api" in message
    assert "xpst connect instagram" in message


def test_preflight_official_path_carries_no_advisory(tmp_path: Path) -> None:
    config = _config(tmp_path, instagram_mode="graph_api", x_enabled=False)
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"\x00" * 64)

    plan = build_route_plan(
        config,
        media_paths=[str(media)],
        caption="hello",
        content_type="video",
        requested=["instagram"],
        include_transform=False,
        include_readiness=False,
    )
    codes = [w["code"] for w in plan.to_dict()["destinations"][0]["warnings"]]
    assert apr.ADVISORY_CODE_PRIVATE_API not in codes


def test_preflight_does_not_raise_the_advisory_for_a_disabled_platform(tmp_path: Path) -> None:
    config = _config(tmp_path, instagram_enabled=False, x_enabled=False)

    plan = build_route_plan(
        config,
        media_paths=[],
        caption="hello",
        content_type="text",
        requested=["instagram"],
        include_transform=False,
        include_readiness=False,
    )
    destination = plan.to_dict()["destinations"][0]
    assert apr.ADVISORY_CODE_PRIVATE_API not in [w["code"] for w in destination["warnings"]]


# ── doctor ──────────────────────────────────────────────────────────────────


def _doctor_config_file(tmp_path: Path, *, instagram_mode: str) -> str:
    cfg = tmp_path / "doctor" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        yaml.safe_dump(
            {
                "accounts": {
                    "instagram": {
                        "enabled": True,
                        "auth_mode": instagram_mode,
                        "session_file": str(cfg.parent / "instagram_session.json"),
                    },
                    "x": {"enabled": False, "auth_mode": "api_v2"},
                    "youtube": {"enabled": True, "client_secrets": "", "token_file": ""},
                },
                "video": {"download_dir": str(tmp_path / "downloads")},
                "monitoring": {"log_level": "INFO", "log_file": str(tmp_path / "logs" / "xpst.log")},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return str(cfg)


@pytest.fixture
def stub_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Doctor's live probe answered without network I/O."""

    async def _fake(config: object) -> dict[str, bool]:
        return {p: True for p in ("youtube", "tiktok", "x", "instagram", "threads", "facebook", "messenger")}

    import xpst.connect as connect_mod

    monkeypatch.setattr(connect_mod, "test_connections", _fake)


def _doctor_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, instagram_mode: str) -> dict:
    import shutil

    monkeypatch.setenv("XPST_FFMPEG_PATH", "/usr/local/bin/ffmpeg")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/" + name)
    from click.testing import CliRunner

    result = CliRunner().invoke(
        main, ["--config", _doctor_config_file(tmp_path, instagram_mode=instagram_mode), "doctor", "--json"]
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output[result.output.find("{") :])


def test_doctor_reports_the_risk_for_a_session_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_healthy: None
) -> None:
    data = _doctor_json(tmp_path, monkeypatch, instagram_mode="session")

    advisories = {item["platform"]: item for item in data["ban_risk_advisories"]}
    assert "instagram" in advisories
    assert advisories["instagram"]["ban_risk"] == apr.BAN_RISK_PRIVATE_API
    assert advisories["instagram"]["auth_mode"] == "session"
    assert data["platforms"]["instagram"]["ban_risk"]["ban_risk"] == apr.BAN_RISK_PRIVATE_API
    # Advisory only: it must never be smuggled into the failure list.
    assert data["all_clear"] is True
    assert data["issues"] == []
    assert [item["platform"] for item in data["ban_risk_advisories"]] == ["instagram"]


def test_doctor_reports_nothing_for_a_graph_api_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_healthy: None
) -> None:
    data = _doctor_json(tmp_path, monkeypatch, instagram_mode="graph_api")

    assert data["ban_risk_advisories"] == []
    assert data["platforms"]["instagram"]["ban_risk"] is None
    assert data["all_clear"] is True


# ── xpst connect instagram: the moment of choice ───────────────────────────


def _fake_instagrapi() -> tuple[object, object]:
    """A MagicMock instagrapi module + the client it hands out."""
    from unittest.mock import MagicMock

    client = MagicMock()
    client.get_settings.return_value = {"authorization_data": {"sessionid": "sid"}}
    account = MagicMock()
    account.username = "tester"
    account.full_name = "Test User"
    account.is_business = False
    account.business_category_name = ""
    client.account_info.return_value = account
    module = MagicMock()
    module.Client.return_value = client
    return module, client


def test_connect_states_the_risk_and_records_the_operator_choice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Choosing instagrapi prints the risk, then records that choice (0600)."""
    import sys
    from unittest.mock import patch

    from xpst import connect

    config = _config(tmp_path, x_enabled=False)
    config.instagram.auth_mode = "graph_api"  # the shipped default, before choosing
    module, _client = _fake_instagrapi()

    answers = iter(["n", "y", "tester"])  # Graph API? no -> continue? yes -> username

    def mock_input(*_a: object, **_k: object) -> str:
        return next(answers)

    def mock_confirm(_message: str, default: bool = True) -> bool:
        return next(answers).lower() in ("y", "yes")

    with patch.dict(sys.modules, {"instagrapi": module}), patch.object(
        connect.console, "input", mock_input
    ), patch.object(connect, "_input_secret", lambda *_a, **_k: "pw"), patch.object(
        connect, "_confirm", mock_confirm
    ):
        assert connect.connect_instagram(config) is True

    out = capsys.readouterr().out
    assert "BAN RISK" in out
    assert "unofficial_private_api" in out
    assert "Recorded operator choice" in out

    assert config.instagram.auth_mode == "session"
    ack_path = Path(config.config_dir) / apr.ACK_FILE_NAME
    assert ack_path.exists()
    stored = json.loads(ack_path.read_text(encoding="utf-8"))
    assert stored["acknowledgements"]["instagram"]["auth_mode"] == "session"
    assert stored["acknowledgements"]["instagram"]["surface"] == "xpst connect instagram"
    if not sys.platform.startswith("win"):
        assert _mode(ack_path) == 0o600


def test_connect_refuses_the_unofficial_path_without_confirmation(tmp_path: Path) -> None:
    import sys
    from unittest.mock import patch

    from xpst import connect

    config = _config(tmp_path, x_enabled=False)
    config.instagram.auth_mode = "graph_api"
    module, _client = _fake_instagrapi()
    answers = iter(["n", "n"])  # Graph API? no -> continue? no

    def mock_confirm(_message: str, default: bool = True) -> bool:
        return next(answers).lower() in ("y", "yes")

    with patch.dict(sys.modules, {"instagrapi": module}), patch.object(
        connect, "_confirm", mock_confirm
    ):
        assert connect.connect_instagram(config) is False

    assert config.instagram.auth_mode == "graph_api"
    assert not (Path(config.config_dir) / apr.ACK_FILE_NAME).exists()
    assert not (Path(config.instagram.session_file)).exists()


def test_run_connect_honours_the_explicit_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`xpst connect --config X` must not write its audit record into ~/.xpst.

    Regression: the wizard called ``XPSTConfig.load()`` with no path, so the
    session file and the operator-choice record landed in the default config
    directory even when the operator named another one.
    """
    import os
    import sys
    from unittest.mock import patch

    from xpst import connect

    cfg_path = tmp_path / "explicit.yaml"
    seed = _config(tmp_path, x_enabled=False)
    seed.instagram.auth_mode = "graph_api"
    seed.save(str(cfg_path))
    module, _client = _fake_instagrapi()
    answers = iter(["n", "y", "tester"])

    def mock_input(*_a: object, **_k: object) -> str:
        return next(answers)

    def mock_confirm(_message: str, default: bool = True) -> bool:
        return next(answers).lower() in ("y", "yes")

    with patch.dict(sys.modules, {"instagrapi": module}), patch.object(
        connect.console, "input", mock_input
    ), patch.object(connect, "_input_secret", lambda *_a, **_k: "pw"), patch.object(
        connect, "_confirm", mock_confirm
    ):
        assert connect.run_connect(platforms=["instagram"], config_path=str(cfg_path)) is True

    assert (tmp_path / apr.ACK_FILE_NAME).exists()
    isolated = Path(os.environ["XPST_CONFIG_DIR"])
    assert not (isolated / apr.ACK_FILE_NAME).exists()


# ── HTTP API: /api/connect/{platform} ──────────────────────────────────────


@pytest.fixture(autouse=True)
def _api_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XPST_API_TOKEN", "ban-risk-token")


def _api_config_dir(tmp_path: Path, *, instagram_mode: str, enabled: bool = True) -> str:
    cfg_dir = tmp_path / "api-cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 4,
                "accounts": {
                    "instagram": {
                        "enabled": enabled,
                        "auth_mode": instagram_mode,
                        "session_file": str(cfg_dir / "instagram_session.json"),
                    },
                    "x": {"enabled": False, "auth_mode": "api_v2"},
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return str(cfg_dir)


def _client(config_dir: str) -> TestClient:
    app = FastAPI()
    app.include_router(create_api_router(config_dir))
    return TestClient(app, headers={"X-API-Token": "ban-risk-token"})


def test_api_connect_states_the_risk_before_any_write(tmp_path: Path) -> None:
    client = _client(_api_config_dir(tmp_path, instagram_mode="session"))

    data = client.post(
        "/api/connect/instagram", json={"dry_run": True, "verify": False}
    ).json()

    assert data["ban_risk"]["ban_risk"] == apr.BAN_RISK_PRIVATE_API
    assert data["ban_risk"]["auth_mode"] == "session"
    assert data["ban_risk"]["acknowledged"] is False
    assert data["ban_risk_acknowledged"] is False
    assert data["ban_risk_would_record"] is False
    # A dry run records nothing.
    assert not (tmp_path / "api-cfg" / apr.ACK_FILE_NAME).exists()


def test_api_connect_dry_run_reports_a_pending_acknowledgement(tmp_path: Path) -> None:
    """A dry run must not write, but must say the acknowledgement was asked for."""
    client = _client(_api_config_dir(tmp_path, instagram_mode="session"))

    data = client.post(
        "/api/connect/instagram",
        json={"dry_run": True, "verify": False, "acknowledge_ban_risk": True},
    ).json()

    assert data["ban_risk_acknowledged"] is False
    assert data["ban_risk_would_record"] is True
    assert not (tmp_path / "api-cfg" / apr.ACK_FILE_NAME).exists()


def test_api_connect_reports_nothing_for_the_official_path(tmp_path: Path) -> None:
    client = _client(_api_config_dir(tmp_path, instagram_mode="graph_api"))

    data = client.post(
        "/api/connect/instagram", json={"dry_run": True, "verify": False}
    ).json()

    assert data["ban_risk"] is None
    assert data["ban_risk_acknowledged"] is False


def test_api_connect_records_the_operator_choice(tmp_path: Path) -> None:
    client = _client(_api_config_dir(tmp_path, instagram_mode="session"))

    data = client.post(
        "/api/connect/instagram",
        json={"dry_run": False, "verify": False, "acknowledge_ban_risk": True},
    ).json()

    assert data["ban_risk_acknowledged"] is True
    assert data["ban_risk"]["acknowledged"] is True
    ack_path = tmp_path / "api-cfg" / apr.ACK_FILE_NAME
    assert ack_path.exists()
    stored = json.loads(ack_path.read_text(encoding="utf-8"))
    assert stored["acknowledgements"]["instagram"]["surface"] == "POST /api/connect/instagram"


def test_api_connect_acknowledges_nothing_on_the_official_path(tmp_path: Path) -> None:
    client = _client(_api_config_dir(tmp_path, instagram_mode="graph_api"))

    data = client.post(
        "/api/connect/instagram",
        json={"dry_run": False, "verify": False, "acknowledge_ban_risk": True},
    ).json()

    assert data["ban_risk_acknowledged"] is False
    assert not (tmp_path / "api-cfg" / apr.ACK_FILE_NAME).exists()
