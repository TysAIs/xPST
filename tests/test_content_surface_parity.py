"""One ``content_type`` contract across the CLI, MCP and HTTP surfaces.

The product promise is that humans and agents share one vocabulary: the same
request must be validated identically whether it arrives through ``xpst post``,
the MCP ``xpst_post`` tool, or ``POST /api/post`` — and the capability list a
human reads must be the list an agent plans against.

Two shipped defects motivate these tests:

* a destination could *declare* a content type it had no implementation for
  (Threads declared ``text`` while its adapter only builds a ``media_type:
  VIDEO`` container; Instagram declared ``image`` while its Graph path is
  REELS-only; X declared ``thread`` with no text-thread sender). An agent read
  that list and attempted an operation that cannot work — a fabricated success
  waiting to happen.
* each surface re-derived its own modality decision, so "can this be posted?"
  had more than one answer.

Nothing here uploads anything: the refusals happen before any uploader is
touched, and the video case runs as a dry run. The MCP engine is a stub whose
upload methods must never be called.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst.cli import main
from xpst.config import XPSTConfig
from xpst.content import (
    CONTENT_TYPES,
    DESTINATION_CONTENT_PROFILES,
    capability_document,
    coerce_content_type,
)
from xpst.dashboard.api import create_api_router
from xpst.mcp import server as mcp_server
from xpst.mcp.server import _handle_post
from xpst.platforms.base import PlatformRegistry

if TYPE_CHECKING:
    from pathlib import Path

# The mutating API route needs the dashboard token; MCP calls the handler
# in-process but still passes the mutation guardrail.
API_TOKEN = "content-parity-token"
API_HEADERS = {"X-API-Token": API_TOKEN}

TEXT_CASE: dict[str, Any] = {
    "caption": "hello from xPST",
    "content_type": "text",
    "platforms": ["youtube"],
}


@pytest.fixture(autouse=True)
def _surfaces_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XPST_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("XPST_MCP_ALLOW_MUTATIONS", "1")
    mcp_server._server = None
    yield
    mcp_server._server = None


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """An isolated config directory with a valid config file."""
    directory = tmp_path / "config"
    directory.mkdir()
    (directory / "config.yaml").write_text(
        f"video:\n  download_dir: {tmp_path / 'downloads'}\n", encoding="utf-8"
    )
    return directory


@pytest.fixture
def config_path(config_dir: Path) -> Path:
    return config_dir / "config.yaml"


@pytest.fixture
def config(config_dir: Path) -> XPSTConfig:
    loaded = XPSTConfig()
    loaded.config_dir = str(config_dir)
    return loaded


@pytest.fixture
def client(config_dir: Path) -> TestClient:
    app = FastAPI()
    app.include_router(create_api_router(str(config_dir)))
    with TestClient(app) as test_client:
        yield test_client


# ── surface helpers ─────────────────────────────────────────────────────────


def _cli_json(config_path: Path, argv: list[str]) -> tuple[int, dict[str, Any]]:
    """Run the CLI with ``--json`` and return (exit_code, payload)."""
    result = CliRunner().invoke(main, ["--config", str(config_path), *argv, "--json"])
    text = (result.stdout or result.output or "").strip()
    start = text.find("{")
    assert start >= 0, f"no JSON on stdout (exit {result.exit_code}): {text!r}"
    return result.exit_code, json.loads(text[start:])


def _stub_engine(config: XPSTConfig) -> MagicMock:
    """An engine stub whose upload methods must never be reached."""
    engine = MagicMock()
    engine._platforms = {"youtube": MagicMock()}
    engine.config = config
    return engine


def _mcp_post(config: XPSTConfig, payload: dict[str, Any], engine: MagicMock) -> dict[str, Any]:
    result = asyncio.run(_handle_post(engine, payload))
    return json.loads(result.content[0].text)


def _mcp_tool_payload(name: str, config: XPSTConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    mcp_server._server = mcp_server.XPSTMCPServer(config)
    result = asyncio.run(mcp_server.handle_call_tool(name, arguments or {}))
    assert not result.isError, result.content[0].text
    return json.loads(result.content[0].text)


# ── the declared capability table ───────────────────────────────────────────


def test_publishing_destinations_declare_exactly_what_they_implement() -> None:
    """Every declared content type has a working path — or is not declared.

    Iterates the declared/implemented table and fails on any false declaration,
    which is the defect that let an agent attempt an impossible post.
    """
    for platform, profile in DESTINATION_CONTENT_PROFILES.items():
        if not profile.is_publishing:
            continue
        assert profile.declared_but_unimplemented == frozenset(), (
            f"{platform} declares "
            f"{sorted(item.value for item in profile.declared_but_unimplemented)} with no "
            f"implementation: an agent reading the capability list would attempt a post "
            f"that cannot work"
        )
        assert profile.declared == profile.implemented, (
            f"{platform} declares {sorted(item.value for item in profile.declared)} but "
            f"implements {sorted(item.value for item in profile.implemented)}"
        )


def test_provider_manifests_declare_only_implementable_content_types() -> None:
    """The list agents actually read (``extra["content"]``) carries no false claim."""
    PlatformRegistry.auto_discover()
    manifests = {manifest.name: manifest for manifest in PlatformRegistry.list_manifests(XPSTConfig())}
    assert set(manifests) == set(DESTINATION_CONTENT_PROFILES)
    for platform, manifest in manifests.items():
        profile = DESTINATION_CONTENT_PROFILES[platform]
        for label in manifest.extra.get("content") or ():
            resolved = coerce_content_type(label)
            assert resolved in profile.implemented, (
                f"{platform}'s manifest declares {label!r} but the contract implements "
                f"{sorted(item.value for item in profile.implemented)}"
            )


def test_every_declared_content_type_has_a_publishing_route_or_is_absent() -> None:
    """The capability document a surface serves is self-consistent.

    A publishing destination that lists a content type must also have a real
    publishing route for it; anything else would be a declaration without a
    path. (A messaging destination's path is its DM sender, not a publish
    route, so it is excluded.)
    """
    document = capability_document()
    assert document["declared_but_unimplemented"] == {}
    for platform, profile in document["platforms"].items():
        if not profile["publishing"]:
            continue
        for content_type in profile["implemented"]:
            assert document["publish_routes"][content_type] != "unimplemented", (
                f"{platform} lists {content_type} as implemented but no publishing route exists"
            )
        assert profile["declared"] == profile["implemented"]
    # A content type with no route is exactly the set no destination implements.
    routed = {name for name, route in document["publish_routes"].items() if route != "unimplemented"}
    implemented = {
        content_type
        for profile in document["platforms"].values()
        if profile["publishing"]
        for content_type in profile["implemented"]
    }
    assert routed == implemented


# ── one capability list, three surfaces ─────────────────────────────────────


def test_cli_mcp_and_http_serve_the_same_capability_document(
    config: XPSTConfig, config_path: Path, client: TestClient
) -> None:
    code, cli_payload = _cli_json(config_path, ["capabilities"])
    assert code == 0, cli_payload

    mcp_payload = _mcp_tool_payload("xpst_capabilities", config)
    http_payload = client.get("/api/capabilities").json()

    canonical = capability_document()
    assert cli_payload == mcp_payload["content"] == http_payload == canonical
    # No surface offers a content type its destination cannot publish.
    assert cli_payload["declared_but_unimplemented"] == {}


def test_mcp_post_schema_states_the_canonical_vocabulary_and_allows_text() -> None:
    tool = next(item for item in mcp_server.TOOLS if item.name == "xpst_post")
    schema = tool.inputSchema
    assert schema["properties"]["content_type"]["enum"] == [item.value for item in CONTENT_TYPES]
    assert "video_path" not in schema["required"], "a text post carries no file"
    assert "caption" in schema["required"]


# ── the text case: refused identically, never as a success ──────────────────


def test_text_post_is_refused_identically_by_cli_mcp_and_http(
    config: XPSTConfig, config_path: Path, client: TestClient
) -> None:
    engine = _stub_engine(config)

    code, cli_payload = _cli_json(
        config_path,
        ["post", "-c", TEXT_CASE["caption"], "--content-type", "text", "-p", "youtube"],
    )
    mcp_payload = _mcp_post(config, dict(TEXT_CASE), engine)
    response = client.post("/api/post", json=dict(TEXT_CASE), headers=API_HEADERS)
    http_payload = response.json()

    assert code == 1, cli_payload
    assert response.status_code == 409, http_payload

    # The content verdict is byte-identical on all three surfaces.
    assert cli_payload["content"] == mcp_payload["content"] == http_payload["content"]
    verdict = cli_payload["content"]
    assert verdict["ok"] is False
    assert verdict["effective_content_type"] == "text"
    assert verdict["route"] == "unimplemented"
    assert verdict["blockers"], "a text post to YouTube must say why it cannot run"

    for surface, payload in (("cli", cli_payload), ("mcp", mcp_payload), ("http", http_payload)):
        assert payload["ok"] is False, surface
        assert payload["uploaded"] is False, surface
        assert payload["blocked"] is True, surface
        assert payload["content_type"] == "text", surface
        # Every surface leads with the same content blockers. (HTTP additionally
        # appends its environment readiness blockers, which are honest extras —
        # the content verdict itself is identical.)
        assert payload["blockers"][: len(verdict["blockers"])] == verdict["blockers"], surface
        assert payload["destinations"], f"{surface} dropped the requested destination"
        assert all(row["success"] is False for row in payload["destinations"]), surface
        assert not any(row["success"] is True for row in payload["destinations"]), surface

    # Nothing was attempted: a refusal is decided before any uploader exists.
    engine.post_manual.assert_not_called()
    engine.post_manual_carousel.assert_not_called()


def test_preflight_endpoint_answers_the_content_question(
    config: XPSTConfig, config_path: Path, client: TestClient
) -> None:
    """``POST /api/preflight`` accepts ``content_type`` and reports it."""
    response = client.post(
        "/api/preflight",
        json={"caption": "hello", "platforms": ["youtube"], "content_type": "text"},
        headers=API_HEADERS,
    )
    assert response.status_code == 200
    payload = response.json()

    mcp_payload = _mcp_tool_payload(
        "xpst_preflight", config, {"caption": "hello", "platforms": ["youtube"], "content_type": "text"}
    )
    assert payload["content"] == mcp_payload["content"]
    assert payload["content_type"] == mcp_payload["content_type"] == "text"
    assert payload["ready"] is False and mcp_payload["ready"] is False
    for blocker in payload["content"]["blockers"]:
        assert blocker in payload["blockers"]
        assert blocker in mcp_payload["blockers"]


# ── the video case: one verdict, one route ──────────────────────────────────


def test_video_post_is_validated_identically_by_cli_mcp_and_http(
    config: XPSTConfig, config_path: Path, client: TestClient, tmp_path: Path
) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 4096)
    engine = _stub_engine(config)

    code, cli_payload = _cli_json(
        config_path,
        ["post", "-v", str(clip), "-c", "hello", "-p", "youtube", "--dry-run"],
    )
    mcp_payload = _mcp_post(
        config,
        {"video_path": str(clip), "caption": "hello", "platforms": ["youtube"], "dry_run": True},
        engine,
    )
    response = client.post(
        "/api/post",
        json={
            "media_path": str(clip),
            "caption": "hello",
            "platforms": ["youtube"],
            "dry_run": True,
        },
        headers=API_HEADERS,
    )
    http_payload = response.json()

    assert code == 0, cli_payload
    assert response.status_code == 200, http_payload

    assert cli_payload["content"] == mcp_payload["content"] == http_payload["content"]
    verdict = cli_payload["content"]
    assert verdict["ok"] is True, verdict["blockers"]
    assert verdict["effective_content_type"] == "video"
    assert verdict["route"] == "video"
    for surface, payload in (("cli", cli_payload), ("mcp", mcp_payload), ("http", http_payload)):
        assert payload["content_type"] == "video", surface
        assert payload["dry_run"] is True, surface

    # A dry run never uploads.
    engine.post_manual.assert_not_called()
    engine.post_manual_carousel.assert_not_called()


def test_no_surface_claims_a_modality_the_contract_does_not_implement(
    config: XPSTConfig, config_path: Path, client: TestClient
) -> None:
    """Threads/text and Instagram/image are refused, not silently accepted."""
    for platform, content_type in (("threads", "text"), ("instagram", "image"), ("x", "thread")):
        request = {"caption": "hello", "content_type": content_type, "platforms": [platform]}
        engine = _stub_engine(config)
        mcp_payload = _mcp_post(config, dict(request), engine)
        response = client.post("/api/post", json=dict(request), headers=API_HEADERS)

        assert mcp_payload["ok"] is False, (platform, content_type)
        assert response.status_code == 409, (platform, content_type, response.json())
        assert mcp_payload["content"] == response.json()["content"]
        assert mcp_payload["blockers"], (platform, content_type)
        engine.post_manual.assert_not_called()
