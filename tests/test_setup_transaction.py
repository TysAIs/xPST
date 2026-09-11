"""Contract tests for the shared resumable setup transaction."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from xpst.cli import main
from xpst.setup_transaction import SetupTransactionService


def _selected() -> list[dict[str, str]]:
    return [
        {"role": "source", "capability": "tiktok"},
        {"role": "video_destination", "capability": "youtube"},
    ]


def test_transaction_resumes_after_process_restart(tmp_path: Path) -> None:
    first = SetupTransactionService(tmp_path).start(_selected())
    step_id = first["steps"][0]["step_id"]

    resumed = SetupTransactionService(tmp_path).resume(
        step_id=step_id,
        step_state="waiting_human",
    )
    restarted = SetupTransactionService(tmp_path).status()

    assert restarted is not None
    assert restarted["transaction_id"] == first["transaction_id"]
    assert restarted["steps"][0]["state"] == "waiting_human"
    assert resumed["completion"]["resumable"] is True


def test_atomic_corrupt_state_recovers_last_valid_transaction(tmp_path: Path) -> None:
    first = SetupTransactionService(tmp_path).start(_selected())
    state_path = tmp_path / "setup_transaction.json"
    backup_path = tmp_path / "setup_transaction.json.bak"
    assert state_path.exists()
    assert backup_path.exists()

    state_path.write_text("{truncated", encoding="utf-8")
    recovered = SetupTransactionService(tmp_path).status()

    assert recovered is not None
    assert recovered["transaction_id"] == first["transaction_id"]
    assert recovered["errors"]
    assert recovered["errors"][-1]["code"] == "STATE_RECOVERED"
    assert json.loads(state_path.read_text(encoding="utf-8"))["transaction_id"] == first["transaction_id"]


def test_transaction_serialization_contains_no_secrets_or_machine_paths(tmp_path: Path) -> None:
    service = SetupTransactionService(tmp_path)
    transaction = service.start(_selected())
    transaction = service.resume(
        error={"code": "AUTH_REQUIRED", "message": "token=do-not-store", "action": "Connect the account"},
        readiness={
            "sources": [{"capability": "tiktok", "verified": False, "account_id": "do-not-store"}],
            "video_destinations": [],
        },
    )
    serialized = json.dumps(transaction, sort_keys=True).lower()

    assert "do-not-store" not in serialized
    assert "token=" not in serialized
    assert "account_id" not in serialized
    assert str(tmp_path).lower() not in serialized
    assert "config_dir" not in serialized


def test_skipping_steps_never_completes_and_finish_later_is_resumable(tmp_path: Path) -> None:
    service = SetupTransactionService(tmp_path)
    transaction = service.start(_selected())
    for step in transaction["steps"]:
        transaction = service.resume(step_id=step["step_id"], step_state="skipped")

    assert transaction["completion"]["complete"] is False
    assert transaction["completion"]["state"] != "completed"

    later = service.finish_later()
    assert later["completion"]["state"] == "finish_later"
    assert later["completion"]["resumable"] is True
    assert later["pending_human_actions"]
    assert SetupTransactionService(tmp_path).status()["completion"]["state"] == "finish_later"


def test_verified_source_and_live_ready_destination_complete(tmp_path: Path) -> None:
    service = SetupTransactionService(tmp_path)
    service.start(_selected())

    completed = service.record_readiness(
        sources=["tiktok"],
        video_destinations=["youtube"],
    )

    assert completed["completion"] == {
        "state": "completed",
        "complete": True,
        "resumable": False,
        "reason": None,
    }
    assert completed["readiness"]["state"] == "ready"
    assert completed["readiness"]["video_destinations"][0]["live_ready"] is True


def test_structured_json_methods_expose_actionable_transaction(tmp_path: Path) -> None:
    service = SetupTransactionService(tmp_path)
    started = service.start_json(_selected())
    status = service.status_json()
    resumed = service.resume_json(finish_later=True)

    assert started["ok"] is True
    assert started["operation"] == "start"
    assert status["transaction_id"] == started["transaction_id"]
    assert resumed["completion"]["state"] == "finish_later"
    assert resumed["pending_human_actions"]


def test_legacy_cli_aliases_share_one_transaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    # Windows production uses APPDATA; isolate the native root for this test.
    monkeypatch.setenv("APPDATA", str(tmp_path))
    runner = CliRunner()

    outputs = []
    for command in ("setup", "onboard", "wizard"):
        result = runner.invoke(main, [command, "--json"])
        assert result.exit_code in (0, 2, 3), result.output
        outputs.append(json.loads(result.stdout))

    ids = {payload["transaction_id"] for payload in outputs}
    assert len(ids) == 1
    assert all(payload["pending_human_actions"] for payload in outputs)
    assert all(payload["alias"]["alias_of"] == "setup" for payload in outputs)


def test_non_tty_json_never_prompts_or_opens_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    # Windows production appends ``xPST`` below APPDATA; keep the native
    # contract isolated and assert the canonical path below.
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: pytest.fail("input() called"))
    monkeypatch.setattr("webbrowser.open", lambda *args, **kwargs: pytest.fail("browser opened"))

    result = CliRunner().invoke(main, ["setup", "--json"])
    payload = json.loads(result.stdout)

    assert payload["completion"]["complete"] is False
    assert payload["pending_human_actions"]
    expected_root = tmp_path / "xPST" if sys.platform == "win32" else tmp_path / ".xpst"
    assert (expected_root / "setup_transaction.json").exists()


def test_status_reset_and_restart_are_explicit(tmp_path: Path) -> None:
    service = SetupTransactionService(tmp_path)
    started = service.start(_selected())
    assert service.status_json()["transaction_id"] == started["transaction_id"]

    reset = service.reset_json()
    assert reset["ok"] is True
    assert reset["operation"] == "reset"
    assert service.status() is None
    assert not (tmp_path / "setup_transaction.json").exists()


def test_mcp_setup_methods_are_registered_and_structured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    from xpst.config import XPSTConfig
    from xpst.mcp import server as mcp_server

    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    fake_server = mcp_server.XPSTMCPServer(config)
    monkeypatch.setattr(mcp_server, "_server", fake_server)
    monkeypatch.setattr(fake_server, "initialize", AsyncMock())

    names = {tool.name for tool in mcp_server.TOOLS}
    assert {"xpst_setup_start", "xpst_setup_status", "xpst_setup_resume", "xpst_setup_reset"} <= names

    started = asyncio.run(mcp_server.handle_call_tool("xpst_setup_start", {"selected_role_capabilities": _selected()}))
    started_payload = json.loads(started.content[0].text)
    assert started_payload["ok"] is True
    assert started_payload["operation"] == "start"

    later = asyncio.run(mcp_server.handle_call_tool("xpst_setup_resume", {"finish_later": True}))
    later_payload = json.loads(later.content[0].text)
    assert later_payload["completion"]["state"] == "finish_later"
    assert later_payload["pending_human_actions"]

    status = asyncio.run(mcp_server.handle_call_tool("xpst_setup_status", {}))
    assert json.loads(status.content[0].text)["transaction_id"] == started_payload["transaction_id"]
