"""CLI ↔ MCP parity for the recovery operations an agent can drive.

Three operations used to differ between the two surfaces:

* cancelling a scheduled post existed only in the CLI (``schedule remove``),
* targeted retry existed only in the CLI (``failures retry``),
* ``xpst_delete`` looked like the CLI ``delete`` while dropping only the local
  record and still reporting ``success: true``.

An agent that believes it deleted a live post, or that a retry targeted the
item it named, is a fabricated-success machine. These tests drive BOTH surfaces
against the same stubbed state and assert they reach the same verdict.

Platform calls are stubbed (``CrossPostEngine.post_manual`` is replaced), so
nothing here touches the network or a real account.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from click.testing import CliRunner

pytest.importorskip("mcp", reason="mcp extra not installed")

from xpst.cli import main
from xpst.config import XPSTConfig
from xpst.engine import CrossPostEngine, CrossPostResult
from xpst.mcp import server as mcp_server
from xpst.platforms.base import UploadResult
from xpst.schedule_manager import ScheduleManager
from xpst.services import recovery_service

if TYPE_CHECKING:
    from pathlib import Path

VIDEO_ID = "tiktok:abc123"
PLATFORM = "x"

# The canonical verdict fields every surface must agree on.
VERDICT_KEYS = ("ok", "operation", "scope", "video_id", "platform", "attempted", "posted")
SCHEDULE_VERDICT_KEYS = ("ok", "operation", "scope", "dry_run", "entry_id", "found", "cancelled")


def _extract_json(output: str):
    for index, char in enumerate(output):
        if char in ("{", "["):
            return json.loads(output[index:])
    return json.loads(output)


def _mcp_payload(result) -> dict:
    return json.loads(result.content[0].text)


def _cli_payload(result) -> dict:
    """CLI JSON, ignoring any rich log lines the runner interleaves."""
    text = result.stdout.strip()
    if text.startswith(("{", "[")):
        return json.loads(text)
    return _extract_json(result.output)


@pytest.fixture(autouse=True)
def _clear_mutation_env(monkeypatch):
    """Start every test from the fail-closed default."""
    for var in ("XPST_MCP_READONLY", "XPST_MCP_ALLOW_MUTATIONS", "XPST_MCP_REQUIRE_CONFIRM"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def allow_mutations(monkeypatch):
    monkeypatch.setenv("XPST_MCP_ALLOW_MUTATIONS", "1")


@pytest.fixture
def schedule_dir(tmp_path, monkeypatch):
    """Force every ScheduleManager (CLI and MCP) onto one throwaway store."""
    forced = tmp_path / "schedulestore"
    original_init = ScheduleManager.__init__

    def patched_init(self, config_dir="~/.xpst"):
        original_init(self, config_dir=str(forced))

    monkeypatch.setattr(ScheduleManager, "__init__", patched_init)
    return forced


def _write_config(tmp_path: Path, downloads: Path) -> Path:
    """A real config file so the CLI resolves the same config_dir as the MCP side."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"video:\n  download_dir: {downloads}\n", encoding="utf-8"
    )
    return config_path


def _seed_state(config_dir: Path, video_id: str = VIDEO_ID, *, error: bool = True) -> None:
    from xpst.state_store import StateStore

    store = StateStore(config_dir)
    state = store.get()
    state["posted_videos"][video_id] = {
        "source_url": "/safe/source.mp4",
        "caption": "hello world",
        "posted_to": {},
        "errors": {PLATFORM: {"error": "rate limited", "retryable": True}} if error else {},
    }
    store.set(state)


def _fake_engine(config: XPSTConfig):
    """A real engine (tmp config dir) whose upload path is replaced by the caller."""
    engine = CrossPostEngine(config)
    return engine


class _FakeServer:
    def __init__(self, config: XPSTConfig, engine=None):
        self.config = config
        self._engine = engine

    async def initialize(self) -> None:  # get_server() awaits this
        return None

    def get_engine(self):
        return self._engine


def _patched_server(config: XPSTConfig, engine=None):
    return patch.object(mcp_server, "_server", _FakeServer(config, engine))


# ── schedule cancel ──────────────────────────────────────────────────────────


def test_schedule_cancel_mcp_and_cli_agree_on_a_real_cancellation(
    tmp_path, schedule_dir, allow_mutations
):
    """A known entry is cancelled, and both surfaces report it identically."""
    manager = ScheduleManager()
    cli_entry = manager.add("/tmp/cli.mp4", "cli entry", __import__("datetime").datetime(2026, 12, 1))
    mcp_entry = manager.add("/tmp/mcp.mp4", "mcp entry", __import__("datetime").datetime(2026, 12, 2))

    # CLI surface
    result = CliRunner().invoke(main, ["schedule", "remove", cli_entry["id"], "--json"])
    assert result.exit_code == 0, result.output
    cli_payload = _cli_payload(result)

    # MCP surface, through the real dispatch + guardrail
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    mcp_result = __import__("asyncio").run(
        mcp_server.handle_call_tool("xpst_schedule_cancel", {"entry_id": mcp_entry["id"]})
    )
    mcp_payload = _mcp_payload(mcp_result)

    for payload in (cli_payload, mcp_payload):
        assert payload["ok"] is True
        assert payload["cancelled"] is True
        assert payload["scope"] == recovery_service.SCHEDULE_CANCEL_SCOPE
        assert payload["operation"] == "schedule_cancel"
        assert payload["found"] is True
    assert {key: cli_payload[key] for key in SCHEDULE_VERDICT_KEYS if key != "entry_id"} == {
        key: mcp_payload[key] for key in SCHEDULE_VERDICT_KEYS if key != "entry_id"
    }
    assert cli_payload["entry_id"] == cli_entry["id"]
    assert mcp_payload["entry_id"] == mcp_entry["id"]
    # The legacy CLI key still ships for already-released consumers.
    assert cli_payload["removed"] == cli_entry["id"]

    # Both entries really left the store.
    remaining = {entry["id"] for entry in ScheduleManager().list()}
    assert cli_entry["id"] not in remaining
    assert mcp_entry["id"] not in remaining


def test_schedule_cancel_unknown_id_fails_identically_and_changes_nothing(
    tmp_path, schedule_dir, allow_mutations
):
    """A miss is a failure on both surfaces — never a silent no-op success."""
    ScheduleManager().add("/tmp/keep.mp4", "keep me", __import__("datetime").datetime(2026, 12, 3))
    before = ScheduleManager().list()

    cli_result = CliRunner().invoke(main, ["schedule", "remove", "deadbeef", "--json"])
    cli_payload = _cli_payload(cli_result)

    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    mcp_result = __import__("asyncio").run(
        mcp_server.handle_call_tool("xpst_schedule_cancel", {"entry_id": "deadbeef"})
    )
    mcp_payload = _mcp_payload(mcp_result)

    assert cli_result.exit_code == 1
    assert cli_payload["ok"] is False
    assert cli_payload["error"]["code"] == recovery_service.SCHEDULE_ENTRY_NOT_FOUND
    assert mcp_payload["ok"] is False
    assert mcp_payload["error"]["code"] == recovery_service.SCHEDULE_ENTRY_NOT_FOUND
    assert mcp_payload["cancelled"] is False
    assert mcp_result.isError is True

    # Nothing was written to the store.
    assert ScheduleManager().list() == before


def test_schedule_cancel_dry_run_predicts_the_real_verdict(tmp_path, schedule_dir, allow_mutations):
    """dry_run returns the verdict the executing path acts on, and writes nothing."""
    entry = ScheduleManager().add(
        "/tmp/planned.mp4", "planned", __import__("datetime").datetime(2026, 12, 4)
    )

    cli_dry = CliRunner().invoke(main, ["schedule", "remove", entry["id"], "--dry-run", "--json"])
    cli_payload = _cli_payload(cli_dry)

    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    mcp_result = __import__("asyncio").run(
        mcp_server.handle_call_tool(
            "xpst_schedule_cancel", {"entry_id": entry["id"], "dry_run": True}
        )
    )
    mcp_payload = _mcp_payload(mcp_result)

    for payload in (cli_payload, mcp_payload):
        assert payload["dry_run"] is True
        assert payload["ok"] is True
        assert payload["found"] is True
        assert payload["cancelled"] is False
    assert cli_payload["entry"] == mcp_payload["entry"]

    # And the entry is untouched, so the verdict was a prediction, not an act.
    assert entry["id"] in {e["id"] for e in ScheduleManager().list()}

    # The dry-run verdict for a missing entry matches the real failure code too.
    missing = CliRunner().invoke(main, ["schedule", "remove", "nosuch", "--dry-run", "--json"])
    assert _cli_payload(missing)["ok"] is False
    assert _cli_payload(missing)["error"]["code"] == recovery_service.SCHEDULE_ENTRY_NOT_FOUND


# ── targeted failure retry ───────────────────────────────────────────────────


def _stub_upload(monkeypatch, *, success: bool, post_url: str | None = "https://x.example/1",
                 error: str | None = None) -> list:
    calls: list[tuple[str, str, list[str]]] = []

    async def fake_post_manual(self, video_path, caption, platforms=None):
        calls.append((str(video_path), caption, list(platforms or [])))
        result = CrossPostResult(video_id=str(video_path), caption=caption)
        target = (platforms or [""])[0]
        result.results[target] = UploadResult(
            platform=target, success=success, post_url=post_url, error=error
        )
        result.update_status()
        return result

    monkeypatch.setattr(CrossPostEngine, "post_manual", fake_post_manual)
    return calls


def _retry_setup(tmp_path: Path, monkeypatch, *, success: bool = True):
    downloads = tmp_path / "videos"
    downloads.mkdir(exist_ok=True)
    (downloads / "abc123.mp4").write_bytes(b"not-a-real-video")
    config_path = _write_config(tmp_path, downloads)
    _seed_state(tmp_path)
    calls = _stub_upload(monkeypatch, success=success)
    return config_path, calls


def test_targeted_retry_mcp_and_cli_hit_the_same_target(tmp_path, monkeypatch, allow_mutations):
    """Both surfaces retry the named (video, platform) and agree on the verdict.

    The two surfaces are driven one after the other on the same recorded
    failure, re-seeding state in between: a successful retry clears the failure
    (which is itself part of the contract).
    """
    config_path, calls = _retry_setup(tmp_path, monkeypatch)

    cli_result = CliRunner().invoke(
        main,
        ["--config", str(config_path), "failures", "retry", VIDEO_ID, "-p", PLATFORM, "--json"],
    )
    assert cli_result.exit_code == 0, cli_result.output
    cli_payload = _cli_payload(cli_result)
    assert len(calls) == 1

    # A landed retry clears the recorded failure so the dead-letter queue drains.
    from xpst.state_store import StateStore

    assert not (StateStore(tmp_path).get()["posted_videos"][VIDEO_ID].get("errors") or {})

    _seed_state(tmp_path)
    config = XPSTConfig.load(str(config_path))
    with _patched_server(config, _fake_engine(config)):
        mcp_result = __import__("asyncio").run(
            mcp_server.handle_call_tool(
                "xpst_failures_retry", {"video_id": VIDEO_ID, "platform": PLATFORM}
            )
        )
    mcp_payload = _mcp_payload(mcp_result)
    assert len(calls) == 2

    # Both targeted exactly the item that was named — same file, same single platform.
    for path, caption, platforms in calls:
        assert path.endswith("abc123.mp4")
        assert caption == "hello world"
        assert platforms == [PLATFORM]

    assert {key: cli_payload[key] for key in VERDICT_KEYS} == {
        key: mcp_payload[key] for key in VERDICT_KEYS
    }
    for payload in (cli_payload, mcp_payload):
        assert payload["ok"] is True
        assert payload["attempted"] is True
        assert payload["posted"] is True
        assert payload["post_url"] == "https://x.example/1"
        assert payload["scope"] == recovery_service.FAILURE_RETRY_SCOPE
        assert payload["error"] is None


@pytest.mark.parametrize(
    ("video_id", "seed_error", "expected_code"),
    [
        ("missing:999", True, "VIDEO_NOT_FOUND"),
        (VIDEO_ID, False, "NO_RECORDED_FAILURE"),
    ],
)
def test_targeted_retry_refusals_agree_and_never_upload(
    tmp_path, monkeypatch, allow_mutations, video_id, seed_error, expected_code
):
    """Every refusal carries the same error code on both surfaces, and no upload."""
    downloads = tmp_path / "videos"
    downloads.mkdir()
    config_path = _write_config(tmp_path, downloads)
    _seed_state(tmp_path, error=seed_error)
    calls = _stub_upload(monkeypatch, success=True)

    cli_result = CliRunner().invoke(
        main, ["--config", str(config_path), "failures", "retry", video_id, "-p", PLATFORM, "--json"]
    )
    cli_payload = _cli_payload(cli_result)

    config = XPSTConfig.load(str(config_path))
    with _patched_server(config, _fake_engine(config)):
        mcp_result = __import__("asyncio").run(
            mcp_server.handle_call_tool(
                "xpst_failures_retry", {"video_id": video_id, "platform": PLATFORM}
            )
        )
    mcp_payload = _mcp_payload(mcp_result)

    assert cli_result.exit_code == 1
    assert cli_payload["error"]["code"] == expected_code
    assert mcp_payload["error"]["code"] == expected_code
    assert cli_payload["ok"] is False and mcp_payload["ok"] is False
    assert mcp_payload["attempted"] is False and mcp_payload["posted"] is False
    assert mcp_result.isError is True
    # Nothing was uploaded for a retry that was refused.
    assert calls == []


def test_targeted_retry_without_a_local_file_agrees(tmp_path, monkeypatch, allow_mutations):
    """A recorded failure with no surviving media is NO_LOCAL_FILE on both surfaces."""
    downloads = tmp_path / "videos"
    downloads.mkdir()
    config_path = _write_config(tmp_path, downloads)
    _seed_state(tmp_path)
    calls = _stub_upload(monkeypatch, success=True)

    cli_result = CliRunner().invoke(
        main, ["--config", str(config_path), "failures", "retry", VIDEO_ID, "-p", PLATFORM, "--json"]
    )
    cli_payload = _cli_payload(cli_result)

    config = XPSTConfig.load(str(config_path))
    with _patched_server(config, _fake_engine(config)):
        mcp_result = __import__("asyncio").run(
            mcp_server.handle_call_tool(
                "xpst_failures_retry", {"video_id": VIDEO_ID, "platform": PLATFORM}
            )
        )
    mcp_payload = _mcp_payload(mcp_result)

    assert cli_result.exit_code == 1
    for payload in (cli_payload, mcp_payload):
        assert payload["error"]["code"] == "NO_LOCAL_FILE"
        assert payload["ok"] is False
        assert payload["attempted"] is False
    assert calls == []

    # dry_run reports the same verdict without an upload.
    with _patched_server(config, _fake_engine(config)):
        dry = _mcp_payload(
            __import__("asyncio").run(
                mcp_server.handle_call_tool(
                    "xpst_failures_retry",
                    {"video_id": VIDEO_ID, "platform": PLATFORM, "dry_run": True},
                )
            )
        )
    assert dry["error"]["code"] == "NO_LOCAL_FILE"
    assert dry["dry_run"] is True
    assert calls == []


def test_targeted_retry_failed_upload_is_not_reported_as_success(tmp_path, monkeypatch, allow_mutations):
    """A refused upload is ok=false/attempted=true — never a fabricated success."""
    config_path, calls = _retry_setup(tmp_path, monkeypatch, success=False)

    cli_result = CliRunner().invoke(
        main, ["--config", str(config_path), "failures", "retry", VIDEO_ID, "-p", PLATFORM, "--json"]
    )
    cli_payload = _cli_payload(cli_result)

    config = XPSTConfig.load(str(config_path))
    with _patched_server(config, _fake_engine(config)):
        mcp_result = __import__("asyncio").run(
            mcp_server.handle_call_tool(
                "xpst_failures_retry", {"video_id": VIDEO_ID, "platform": PLATFORM}
            )
        )
    mcp_payload = _mcp_payload(mcp_result)

    assert cli_result.exit_code == 1
    assert len(calls) == 2
    for payload in (cli_payload, mcp_payload):
        assert payload["ok"] is False
        assert payload["attempted"] is True
        assert payload["posted"] is False
        assert payload["error"]["code"] == "RETRY_FAILED"


def test_retry_failed_post_never_uploads_an_unplannable_target():
    """The one-shot service entry point refuses before it reaches the engine."""
    plan_owner = SimpleNamespace(
        config=SimpleNamespace(video=SimpleNamespace(download_dir="/nonexistent")),
        state=SimpleNamespace(get_video=lambda _vid: None),
    )
    payload = __import__("asyncio").run(
        recovery_service.retry_failed_post(plan_owner, "ghost", PLATFORM)
    )
    assert payload["ok"] is False
    assert payload["attempted"] is False
    assert payload["error"]["code"] == "VIDEO_NOT_FOUND"


# ── one implementation, two surfaces ─────────────────────────────────────────


def test_both_surfaces_call_the_same_service_functions(tmp_path, monkeypatch, schedule_dir, allow_mutations):
    """Parity is structural: every surface delegates to recovery_service."""
    seen: list[str] = []

    def fake_cancel(config_dir, entry_id, *, dry_run=False):
        seen.append("cancel")
        return {"ok": True, "operation": "schedule_cancel", "scope": "stub",
                "dry_run": dry_run, "entry_id": entry_id, "found": True,
                "cancelled": False, "entry": None, "error": None}

    async def fake_retry(engine, video_id, platform, *, dry_run=False):
        seen.append("retry")
        return {"ok": False, "operation": "failure_retry", "scope": "stub",
                "dry_run": dry_run, "video_id": video_id, "platform": platform,
                "failure": None, "media_path": None, "attempted": False,
                "posted": False, "post_url": None,
                "error": {"code": "STUB", "message": "stub"}}

    monkeypatch.setattr(recovery_service, "cancel_scheduled_post", fake_cancel)
    monkeypatch.setattr(recovery_service, "retry_failed_post", fake_retry)

    CliRunner().invoke(main, ["schedule", "remove", "abc", "--json"])

    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    __import__("asyncio").run(
        mcp_server.handle_call_tool("xpst_schedule_cancel", {"entry_id": "abc"})
    )
    with _patched_server(config, _fake_engine(config)):
        __import__("asyncio").run(
            mcp_server.handle_call_tool(
                "xpst_failures_retry", {"video_id": VIDEO_ID, "platform": PLATFORM}
            )
        )

    assert seen == ["cancel", "cancel", "retry"]


# ── xpst_delete scope honesty ────────────────────────────────────────────────


def test_delete_tool_description_states_its_scope():
    """tools/list must advertise xpst_delete as a local-record operation."""
    tools = __import__("asyncio").run(mcp_server.list_tools()).tools
    delete = next(tool for tool in tools if tool.name == "xpst_delete")
    description = delete.description or ""

    assert "RECORD" in description
    assert "local xPST state" in description
    assert "does NOT delete" in description
    assert "platform_deleted=false" in description
    assert "`xpst delete" in description  # points at the surface that does delete
    # The schema must not promise a platform deletion it cannot perform.
    assert "record" in delete.inputSchema["properties"]["video_id"]["description"]
    assert "record" in delete.inputSchema["properties"]["platform"]["description"]


def test_new_parity_tools_are_registered_and_gated(allow_mutations):
    names = {tool.name for tool in mcp_server.TOOLS}
    for name in ("xpst_schedule_cancel", "xpst_failures_retry"):
        assert name in names, f"{name} missing from the served registry"
        assert name in mcp_server._MUTATING_TOOLS, f"{name} mutates and must be gated"


def test_delete_payload_states_its_scope():
    """The response payload is where an agent actually reads what happened."""
    video = {"posted_to": {"youtube": {"id": "yt-9"}}}
    removed: list[tuple[str, str]] = []

    class _State:
        def get_video(self, _vid):
            return video

        def remove_post(self, vid, plat):
            removed.append((vid, plat))

        def save(self):
            pass

    result = __import__("asyncio").run(
        mcp_server._handle_delete(SimpleNamespace(state=_State()), {"video_id": "v1"})
    )
    payload = _mcp_payload(result)

    assert payload["success"] is True
    assert payload["removed"] == ["youtube"]
    assert payload["operation"] == "delete_record"
    assert payload["scope"] == recovery_service.DELETE_RECORD_SCOPE
    assert payload["platform_deleted"] is False
    assert "NOT deleted" in payload["note"]
    assert removed == [("v1", "youtube")]


def test_delete_reports_failure_when_it_removed_nothing():
    """A record for a platform the video was never posted to is not a success."""
    class _State:
        def get_video(self, _vid):
            return {"posted_to": {"youtube": {"id": "yt-9"}}}

        def remove_post(self, _vid, _plat):  # pragma: no cover - must not be called
            raise AssertionError("nothing should be removed")

        def save(self):  # pragma: no cover - must not be called
            raise AssertionError("nothing should be saved")

    result = __import__("asyncio").run(
        mcp_server._handle_delete(SimpleNamespace(state=_State()), {"video_id": "v1", "platform": "tiktok"})
    )
    payload = _mcp_payload(result)

    assert payload["ok"] is False
    assert payload["success"] is False
    assert payload["removed"] == []
    assert payload["platform_deleted"] is False
    assert "tiktok" in payload["error"]
    assert result.isError is True
