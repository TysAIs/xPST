"""Ingest-key fetching: the four vendor shapes, and the refusals around them.

Nothing here touches a network. Twitch, Kick and Facebook are driven through
``httpx.MockTransport`` and YouTube through a fake service object, which is the
point of the design: every branch — a key, a 401, a 403, an empty answer, a
malformed answer, missing credentials — is provable on a box with no app
credentials at all (this repository has none for Twitch or Kick, and Kick's
endpoint has never been exercised live anywhere we run).

What the tests are guarding, beyond the happy path:

* a refusal carries a machine-readable ``code`` and a next step, never silence;
* no stream key or access token appears in a repr, a masked view, a status
  payload, or a vendor error message that got echoed;
* the registry's own text cannot drift from the endpoint the code actually
  calls — the Kick row used to name ``/public/v1/stream/key``, which is not in
  Kick's published API index.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst.stream.destinations import STREAM_INGEST_PATHS
from xpst.stream.ingest import (
    CREDENTIALS_MISSING,
    INVALID_RESPONSE,
    MANUAL_REQUIRED,
    NETWORK_ERROR,
    NOT_SUPPORTED,
    PLATFORM_ERROR,
    AppCredentials,
    FetchedKey,
    IngestKeyError,
    clear_app_credentials,
    fetch_and_store_key,
    fetch_facebook_key,
    fetch_ingest_key,
    fetch_kick_key,
    fetch_readiness,
    fetch_twitch_key,
    fetch_youtube_key,
    live_status,
    load_app_credentials,
    registration,
    store_app_credentials,
    stream_report,
)
from xpst.stream.keys import destination_status, load_destinations
from xpst.stream.relay import build_relay_config

if TYPE_CHECKING:
    from pathlib import Path

ACCESS_TOKEN = "tw-user-tok-abc"  # below the scanner's 24-char credential horizon: fixture, not a secret
CLIENT_ID = "tw-client-id-1234"
BROADCASTER_ID = "987654321"
TWITCH_KEY = "live_12345_secretsecretsecret"
KICK_KEY = "kick_secret_key_value_0001"
KICK_INGEST = "rtmps://fa723fc1.global-contribute.live-video.net:443/app"
FB_KEY = "FB-1234567890-abcdef"
FB_INGEST = "rtmps://live-api-s.facebook.com:443/rtmp"


def _config_dir(tmp_path: Path) -> Path:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text("accounts:\n  youtube:\n    enabled: false\n", encoding="utf-8")
    return config_dir


def _twitch_creds(**kwargs: Any) -> AppCredentials:
    values = {
        "platform": "twitch",
        "client_id": CLIENT_ID,
        "access_token": ACCESS_TOKEN,
        "user_id": BROADCASTER_ID,
        "source": "store",
    }
    values.update(kwargs)
    return AppCredentials(**values)


def _json_transport(payload: Any, *, status: int = 200, record: dict[str, Any] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record["method"] = request.method
            record["url"] = str(request.url)
            record["headers"] = dict(request.headers)
            record["content"] = request.content.decode("utf-8", "replace")
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


# ── Twitch ───────────────────────────────────────────────────────────────────


def test_twitch_fetch_sends_the_user_token_and_keeps_the_key_out_of_views() -> None:
    record: dict[str, Any] = {}
    fetched = fetch_twitch_key(_twitch_creds(), transport=_json_transport({"data": [{"stream_key": TWITCH_KEY}]}, record=record))

    assert record["url"].startswith("https://api.twitch.tv/helix/streams/key?")
    assert f"broadcaster_id={BROADCASTER_ID}" in record["url"]
    assert record["headers"]["authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert record["headers"]["client-id"] == CLIENT_ID

    assert fetched.stream_key == TWITCH_KEY
    assert fetched.ingest_url == "rtmps://live.twitch.tv:443/app"
    assert TWITCH_KEY not in repr(fetched)
    assert TWITCH_KEY not in json.dumps(fetched.masked())
    assert fetched.masked()["stream_key"].startswith("…")


def test_twitch_key_becomes_a_relay_destination(tmp_path: Path) -> None:
    config_dir = _config_dir(tmp_path)
    store_app_credentials(
        config_dir, "twitch", client_id=CLIENT_ID, access_token=ACCESS_TOKEN, user_id=BROADCASTER_ID
    )
    fetched = fetch_and_store_key(
        config_dir,
        "twitch",
        transport=_json_transport({"data": [{"stream_key": TWITCH_KEY}]}),
    )
    assert isinstance(fetched, FetchedKey)

    stored = load_destinations(config_dir)
    assert [dest.platform for dest in stored] == ["twitch"]
    config = build_relay_config(stored)
    forward = config["paths"]["xpst"]["forward"]
    assert forward == [{"dest": f"rtmps://live.twitch.tv:443/app#{TWITCH_KEY}"}]


def test_twitch_without_credentials_names_the_registration_step(tmp_path: Path) -> None:
    config_dir = _config_dir(tmp_path)
    with pytest.raises(IngestKeyError) as excinfo:
        fetch_ingest_key(config_dir, "twitch")
    error = excinfo.value
    assert error.code == CREDENTIALS_MISSING
    assert "dev.twitch.tv/console/apps" in error.action
    assert "channel:read:stream_key" in str(error)
    assert error.docs_url == "https://dev.twitch.tv/console/apps"


def test_twitch_401_is_a_platform_error_with_the_scope_action() -> None:
    transport = _json_transport({"message": "Unauthorized"}, status=401)
    with pytest.raises(IngestKeyError) as excinfo:
        fetch_twitch_key(_twitch_creds(), transport=transport)
    error = excinfo.value
    assert error.code == PLATFORM_ERROR
    assert error.status == 401
    assert "channel:read:stream_key" in error.action


def test_twitch_vendor_error_never_echoes_the_access_token() -> None:
    transport = _json_transport({"message": f"bad token {ACCESS_TOKEN}"}, status=401)
    with pytest.raises(IngestKeyError) as excinfo:
        fetch_twitch_key(_twitch_creds(), transport=transport)
    assert ACCESS_TOKEN not in str(excinfo.value)


def test_twitch_empty_data_is_refused_not_read_as_an_empty_key() -> None:
    with pytest.raises(IngestKeyError) as excinfo:
        fetch_twitch_key(_twitch_creds(), transport=_json_transport({"data": []}))
    assert excinfo.value.code == INVALID_RESPONSE


def test_transport_failure_is_a_network_error() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("no route", request=request)

    with pytest.raises(IngestKeyError) as excinfo:
        fetch_twitch_key(_twitch_creds(), transport=httpx.MockTransport(boom))
    assert excinfo.value.code == NETWORK_ERROR


# ── Kick ─────────────────────────────────────────────────────────────────────


def test_kick_fetch_uses_the_channels_endpoint_and_splits_the_returned_url() -> None:
    record: dict[str, Any] = {}
    transport = _json_transport(
        {"data": [{"broadcaster_user_id": int(BROADCASTER_ID), "stream": {"key": KICK_KEY, "url": f"{KICK_INGEST}/{KICK_KEY}"}}]},
        record=record,
    )
    fetched = fetch_kick_key(
        AppCredentials(platform="kick", client_id="kick-client", access_token="kick-token-value", user_id=BROADCASTER_ID),
        transport=transport,
    )
    assert "/public/v1/channels" in record["url"]
    assert "broadcaster_user_id" in record["url"]
    assert fetched.ingest_url == KICK_INGEST
    assert fetched.stream_key == KICK_KEY


def test_kick_without_stream_key_names_the_scope() -> None:
    transport = _json_transport({"data": [{"broadcaster_user_id": 1, "stream": {"is_live": False}}]})
    with pytest.raises(IngestKeyError) as excinfo:
        fetch_kick_key(
            AppCredentials(platform="kick", client_id="c", access_token="t", user_id="1"), transport=transport
        )
    assert excinfo.value.code == INVALID_RESPONSE
    assert "streamkey:read" in excinfo.value.action


def test_kick_missing_url_is_an_invalid_response() -> None:
    transport = _json_transport({"data": [{"stream": {"key": KICK_KEY}}]})
    with pytest.raises(IngestKeyError) as excinfo:
        fetch_kick_key(
            AppCredentials(platform="kick", client_id="c", access_token="t", user_id="1"), transport=transport
        )
    assert excinfo.value.code == INVALID_RESPONSE
    assert "ingest host" in str(excinfo.value)


# ── Facebook ─────────────────────────────────────────────────────────────────


def _store_facebook_page(config_dir: Path) -> None:
    from xpst.platforms.facebook import FACEBOOK_CRED_KEYS
    from xpst.utils.credentials import CredentialStore

    store = CredentialStore(str(config_dir))
    store.store(FACEBOOK_CRED_KEYS["page_id"], "102938475610293")
    store.store(FACEBOOK_CRED_KEYS["page_access_token"], "EAAG-page-token-value")


def test_facebook_creates_an_unpublished_live_video(tmp_path: Path) -> None:
    config_dir = _config_dir(tmp_path)
    _store_facebook_page(config_dir)
    record: dict[str, Any] = {}
    transport = _json_transport(
        {"id": "live-1", "secure_stream_url": f"{FB_INGEST}/{FB_KEY}", "stream_url": f"rtmp://x/{FB_KEY}"},
        record=record,
    )
    fetched = fetch_facebook_key(config_dir, transport=transport, title="Relay test")

    assert record["method"] == "POST"
    assert "/102938475610293/live_videos" in record["url"]
    assert "published=false" in record["content"]
    assert fetched.ingest_url == FB_INGEST
    assert fetched.stream_key == FB_KEY
    assert fetched.resource_id == "live-1"


def test_facebook_without_a_page_token_refuses(tmp_path: Path) -> None:
    config_dir = _config_dir(tmp_path)
    with pytest.raises(IngestKeyError) as excinfo:
        fetch_ingest_key(config_dir, "facebook")
    assert excinfo.value.code == CREDENTIALS_MISSING
    assert "xpst auth facebook" in excinfo.value.action


# ── YouTube ──────────────────────────────────────────────────────────────────


class _FakeInsertRequest:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error

    def execute(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._response


class _FakeLiveStreams:
    def __init__(self, recorder: dict[str, Any], response: Any, error: Exception | None) -> None:
        self._recorder = recorder
        self._response = response
        self._error = error

    def insert(self, *, part: str, body: dict[str, Any]) -> _FakeInsertRequest:
        self._recorder["part"] = part
        self._recorder["body"] = body
        return _FakeInsertRequest(self._response, self._error)


class _FakeYouTube:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.recorder: dict[str, Any] = {}
        self._response = response
        self._error = error

    def liveStreams(self) -> _FakeLiveStreams:  # noqa: N802 - mimics the Google client API
        return _FakeLiveStreams(self.recorder, self._response, self._error)

    def liveBroadcasts(self) -> Any:  # noqa: N802 - mimics the Google client API
        raise AssertionError("fetching an ingest key must not touch liveBroadcasts")


def test_youtube_inserts_a_stream_resource_and_never_a_broadcast() -> None:
    service = _FakeYouTube(
        response={
            "id": "stream-1",
            "cdn": {"ingestionInfo": {"ingestionAddress": "rtmps://a.rtmps.youtube.com:443/live2", "streamName": "abcd-1234"}},
        }
    )
    fetched = fetch_youtube_key(service=service, title="Slot 1")

    assert service.recorder["part"] == "snippet,cdn,contentDetails"
    assert service.recorder["body"]["cdn"]["ingestionType"] == "rtmp"
    assert service.recorder["body"]["contentDetails"]["isReusable"] is False
    assert service.recorder["body"]["snippet"]["title"] == "Slot 1"
    assert fetched.ingest_url == "rtmps://a.rtmps.youtube.com:443/live2"
    assert fetched.stream_key == "abcd-1234"
    assert fetched.resource_id == "stream-1"


def test_youtube_http_error_is_a_platform_error() -> None:
    class _HttpError(Exception):
        class resp:  # noqa: N801 - mimics googleapiclient's shape
            status = 403

    service = _FakeYouTube(error=_HttpError("insufficientPermissions"))
    with pytest.raises(IngestKeyError) as excinfo:
        fetch_youtube_key(service=service)
    assert excinfo.value.code == PLATFORM_ERROR
    assert excinfo.value.status == 403
    assert "youtube.force-ssl" in excinfo.value.action


def test_youtube_channel_without_live_access_is_its_own_refusal() -> None:
    """Measured live 2026-11-16: this channel answers 403 'not enabled for live
    streaming'. That is a channel setting, so telling the operator to
    re-authorise would be the wrong next step."""
    from xpst.stream.ingest import LIVE_NOT_ENABLED

    class _HttpError(Exception):
        class resp:  # noqa: N801
            status = 403

    service = _FakeYouTube(error=_HttpError("The user is not enabled for live streaming."))
    with pytest.raises(IngestKeyError) as excinfo:
        fetch_youtube_key(service=service)
    assert excinfo.value.code == LIVE_NOT_ENABLED
    assert "youtube.com/features" in excinfo.value.action
    assert "not an OAuth problem" in excinfo.value.action


def test_youtube_response_without_ingestion_info_is_refused() -> None:
    with pytest.raises(IngestKeyError) as excinfo:
        fetch_youtube_key(service=_FakeYouTube(response={"id": "stream-1"}))
    assert excinfo.value.code == INVALID_RESPONSE


# ── Refusals by class ────────────────────────────────────────────────────────


def test_manual_platform_refuses_with_the_page_to_copy_from(tmp_path: Path) -> None:
    with pytest.raises(IngestKeyError) as excinfo:
        fetch_ingest_key(_config_dir(tmp_path), "instagram")
    error = excinfo.value
    assert error.code == MANUAL_REQUIRED
    assert "Live Producer" in error.action


def test_unsupported_platform_refuses(tmp_path: Path) -> None:
    with pytest.raises(IngestKeyError) as excinfo:
        fetch_ingest_key(_config_dir(tmp_path), "x")
    assert excinfo.value.code == NOT_SUPPORTED


def test_unknown_platform_is_a_bug_not_a_verdict(tmp_path: Path) -> None:
    with pytest.raises(IngestKeyError) as excinfo:
        fetch_ingest_key(_config_dir(tmp_path), "myspace")
    assert excinfo.value.code == NOT_SUPPORTED
    assert "known:" in str(excinfo.value)


def test_registry_endpoint_text_matches_the_endpoint_the_code_calls() -> None:
    """The row and the call site must not drift again.

    Kick's row named ``/public/v1/stream/key``; Kick's published API index has no
    such path, and the key actually arrives on ``/public/v1/channels``.
    """
    assert "/public/v1/channels" in STREAM_INGEST_PATHS["kick"].endpoint
    assert "/public/v1/stream/key" not in STREAM_INGEST_PATHS["kick"].endpoint
    assert "streams/key" in STREAM_INGEST_PATHS["twitch"].endpoint
    assert "liveStreams" in STREAM_INGEST_PATHS["youtube"].endpoint
    assert "live_videos" in STREAM_INGEST_PATHS["facebook"].endpoint
    for platform, row in STREAM_INGEST_PATHS.items():
        if row.api_available:
            assert row.endpoint, f"{platform} is an api row with no documented endpoint"


# ── App credentials ─────────────────────────────────────────────────────────


def test_app_credentials_round_trip_is_encrypted_and_masked(tmp_path: Path) -> None:
    config_dir = _config_dir(tmp_path)
    stored = store_app_credentials(
        config_dir, "twitch", client_id=CLIENT_ID, access_token=ACCESS_TOKEN, user_id=BROADCASTER_ID
    )
    assert stored.complete
    assert ACCESS_TOKEN not in repr(stored)
    assert ACCESS_TOKEN not in json.dumps(stored.masked())

    raw = b"".join(path.read_bytes() for path in config_dir.rglob("*") if path.is_file())
    assert ACCESS_TOKEN.encode() not in raw, "the access token must never be plaintext on disk"

    reloaded = load_app_credentials(config_dir, "twitch")
    assert reloaded.access_token == ACCESS_TOKEN
    assert reloaded.user_id == BROADCASTER_ID
    assert reloaded.source == "store"


def test_env_overrides_the_stored_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = _config_dir(tmp_path)
    store_app_credentials(config_dir, "twitch", client_id=CLIENT_ID, access_token=ACCESS_TOKEN, user_id=BROADCASTER_ID)
    monkeypatch.setenv("XPST_STREAM_TWITCH_ACCESS_TOKEN", "env-token")
    reloaded = load_app_credentials(config_dir, "twitch")
    assert reloaded.access_token == "env-token"
    assert reloaded.source == "env"


def test_clear_app_credentials_removes_them(tmp_path: Path) -> None:
    config_dir = _config_dir(tmp_path)
    store_app_credentials(config_dir, "kick", client_id="c", access_token="t", user_id="1")
    assert clear_app_credentials(config_dir, "kick") is True
    assert clear_app_credentials(config_dir, "kick") is False
    assert load_app_credentials(config_dir, "kick").complete is False


def test_store_refuses_an_empty_pair(tmp_path: Path) -> None:
    with pytest.raises(IngestKeyError) as excinfo:
        store_app_credentials(_config_dir(tmp_path), "twitch", client_id="", access_token="")
    assert excinfo.value.code == CREDENTIALS_MISSING


# ── Fetch readiness as one verdict ──────────────────────────────────────────


def test_fetch_readiness_separates_manual_from_missing_credentials(tmp_path: Path) -> None:
    config_dir = _config_dir(tmp_path)
    assert fetch_readiness(config_dir, "twitch").missing == ["client_id", "access_token", "user_id"]
    assert fetch_readiness(config_dir, "instagram").missing == ["manual_handoff"]
    assert fetch_readiness(config_dir, "x").missing == ["no_ingest"]

    store_app_credentials(config_dir, "twitch", client_id="c", access_token="t", user_id="1")
    ready = fetch_readiness(config_dir, "twitch")
    assert ready.credentials_present is True
    assert ready.missing == []


def test_youtube_readiness_follows_the_configured_token_path(tmp_path: Path) -> None:
    """A token file outside the config dir is still a configured connection."""
    from xpst.config import XPSTConfig

    config_dir = _config_dir(tmp_path)
    assert fetch_readiness(config_dir, "youtube").credentials_present is False

    token = tmp_path / "elsewhere" / "yt_token.json"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("{}", encoding="utf-8")
    config = XPSTConfig()
    config.config_dir = str(config_dir)
    config.youtube.token_file = str(token)

    assert fetch_readiness(config_dir, "youtube", config=config).credentials_present is True
    assert stream_report(config_dir, config=config)["stream"]["fetchable"] == ["youtube"]


def test_destination_status_carries_the_fetch_verdict_without_a_secret(tmp_path: Path) -> None:
    config_dir = _config_dir(tmp_path)
    store_app_credentials(config_dir, "kick", client_id="kick-c", access_token="kick-secret-token", user_id="42")
    status = destination_status(config_dir)
    rows = {row["platform"]: row for row in status["platforms"]}

    assert rows["kick"]["fetch"]["credentials_present"] is True
    assert rows["twitch"]["fetch"]["credentials_present"] is False
    assert rows["instagram"]["fetch"] is None
    assert "kick" in status["fetchable"]
    assert {row["platform"] for row in status["missing_credentials"]} >= {"twitch", "facebook"}
    assert "kick-secret-token" not in json.dumps(status)


# ── Live status ─────────────────────────────────────────────────────────────


def test_live_status_reports_offline_and_missing_credentials(tmp_path: Path) -> None:
    config_dir = _config_dir(tmp_path)
    store_app_credentials(config_dir, "twitch", client_id=CLIENT_ID, access_token=ACCESS_TOKEN, user_id=BROADCASTER_ID)
    report = live_status(config_dir, transport=_json_transport({"data": []}))

    assert report["live"] == []
    twitch = next(row for row in report["platforms"] if row["platform"] == "twitch")
    assert twitch["live"] is False
    kick = next(row for row in report["unavailable"] if row["platform"] == "kick")
    assert kick["code"] == CREDENTIALS_MISSING
    assert "kick-apps-setup" in kick["docs_url"]


def test_live_status_reports_a_live_channel(tmp_path: Path) -> None:
    config_dir = _config_dir(tmp_path)
    store_app_credentials(config_dir, "twitch", client_id=CLIENT_ID, access_token=ACCESS_TOKEN, user_id=BROADCASTER_ID)
    transport = _json_transport(
        {"data": [{"id": "1", "type": "live", "title": "Building", "viewer_count": 12, "started_at": "2026-11-16T01:00:00Z"}]}
    )
    report = live_status(config_dir, platforms=("twitch",), transport=transport)
    assert report["live"][0]["title"] == "Building"
    assert report["live"][0]["viewer_count"] == 12
    assert ACCESS_TOKEN not in json.dumps(report)


# ── One payload for CLI, MCP and HTTP ───────────────────────────────────────


def test_stream_report_is_one_payload_and_never_holds_a_key(tmp_path: Path) -> None:
    config_dir = _config_dir(tmp_path)
    store_app_credentials(config_dir, "twitch", client_id=CLIENT_ID, access_token=ACCESS_TOKEN, user_id=BROADCASTER_ID)
    from xpst.stream.keys import store_destination

    store_destination(config_dir, "twitch", "rtmps://live.twitch.tv:443/app", TWITCH_KEY)

    payload = stream_report(config_dir)
    text = json.dumps(payload)
    assert payload["stream"]["ready_to_relay"] is True
    assert TWITCH_KEY not in text
    assert ACCESS_TOKEN not in text
    assert payload["stream"]["fetchable"] == ["twitch"]


def test_stream_report_unknown_platform_sets_a_code(tmp_path: Path) -> None:
    payload = stream_report(_config_dir(tmp_path), platform="myspace")
    assert payload["ok"] is False
    assert payload["code"] == "unknown_platform"
    assert "twitch" in payload["known"]


def test_mcp_stream_tool_serves_the_same_payload(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    import asyncio

    from xpst.config import XPSTConfig
    from xpst.mcp import server as mcp_server

    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    result = asyncio.run(mcp_server._handle_stream(config, {}))
    payload = json.loads(result.content[0].text)

    assert payload["stream"]["platforms"] == stream_report(tmp_path)["stream"]["platforms"]
    assert payload["contract_version"] == 1

    missing = asyncio.run(mcp_server._handle_stream(config, {"platform": "myspace"}))
    assert mcp_server.result_is_error(missing)
    assert "xpst_stream" in [tool.name for tool in mcp_server.TOOLS]
    assert "xpst_stream" not in mcp_server._MUTATING_TOOLS


def test_http_stream_route_serves_the_same_payload(tmp_path: Path) -> None:
    from xpst.dashboard.api import create_api_router

    app = FastAPI()
    app.include_router(create_api_router(str(tmp_path)))
    with TestClient(app) as client:
        response = client.get("/api/stream")
        found = client.get("/api/stream", params={"platform": "twitch"})
        unknown = client.get("/api/stream", params={"platform": "myspace"})

    assert response.status_code == 200
    assert response.json()["stream"]["platforms"] == stream_report(tmp_path)["stream"]["platforms"]
    assert found.json()["platform"]["platform"] == "twitch"
    assert unknown.json()["code"] == "unknown_platform"


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_fetch_key_reports_the_refusal_as_json(tmp_path: Path) -> None:
    from xpst.cli import EXIT_CONFIG_ERROR, main

    config_dir = _config_dir(tmp_path)
    cfg = config_dir / "config.yaml"
    result = CliRunner().invoke(main, ["--config", str(cfg), "stream", "fetch-key", "twitch", "--json"])
    assert result.exit_code == EXIT_CONFIG_ERROR
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["ok"] is False
    assert payload["code"] == CREDENTIALS_MISSING
    assert "dev.twitch.tv/console/apps" in payload["action"]


def test_cli_fetch_key_stores_a_fetched_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from xpst.cli import main
    from xpst.stream import ingest

    config_dir = _config_dir(tmp_path)
    cfg = config_dir / "config.yaml"

    def fake_fetch(*args: Any, **kwargs: Any) -> FetchedKey:
        return FetchedKey(
            platform="twitch",
            ingest_url="rtmps://live.twitch.tv:443/app",
            stream_key=TWITCH_KEY,
            endpoint="GET https://api.twitch.tv/helix/streams/key",
        )

    monkeypatch.setattr(ingest, "fetch_ingest_key", fake_fetch)
    result = CliRunner().invoke(main, ["--config", str(cfg), "stream", "fetch-key", "twitch", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["ok"] is True
    assert payload["stored"] is True
    assert TWITCH_KEY not in result.output
    assert [dest.platform for dest in load_destinations(config_dir)] == ["twitch"]


def test_cli_app_credentials_status_lists_the_registration_steps(tmp_path: Path) -> None:
    from xpst.cli import main

    config_dir = _config_dir(tmp_path)
    cfg = config_dir / "config.yaml"
    result = CliRunner().invoke(main, ["--config", str(cfg), "stream", "app-credentials", "kick", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["ok"] is True
    assert payload["credentials"]["complete"] is False
    assert payload["registration"]["scopes"] == ["streamkey:read", "channel:read"]
    assert payload["registration"]["steps"]


def test_cli_app_credentials_stores_and_clears(tmp_path: Path) -> None:
    from xpst.cli import main

    config_dir = _config_dir(tmp_path)
    cfg = config_dir / "config.yaml"
    stored = CliRunner().invoke(
        main,
        [
            "--config",
            str(cfg),
            "stream",
            "app-credentials",
            "twitch",
            "--client-id",
            CLIENT_ID,
            "--access-token",
            ACCESS_TOKEN,
            "--user-id",
            BROADCASTER_ID,
            "--json",
        ],
    )
    assert stored.exit_code == 0, stored.output
    assert ACCESS_TOKEN not in stored.output
    assert load_app_credentials(config_dir, "twitch").access_token == ACCESS_TOKEN

    cleared = CliRunner().invoke(
        main, ["--config", str(cfg), "stream", "app-credentials", "twitch", "--clear", "--json"]
    )
    assert cleared.exit_code == 0, cleared.output
    assert load_app_credentials(config_dir, "twitch").complete is False


def test_cli_live_reports_missing_credentials_rather_than_nothing(tmp_path: Path) -> None:
    from xpst.cli import main

    config_dir = _config_dir(tmp_path)
    cfg = config_dir / "config.yaml"
    result = CliRunner().invoke(main, ["--config", str(cfg), "stream", "live", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert {row["platform"] for row in payload["unavailable"]} == {"twitch", "kick"}
    assert all(row["code"] == CREDENTIALS_MISSING for row in payload["unavailable"])


def test_registration_entries_name_a_free_self_serve_app() -> None:
    for platform in ("twitch", "kick"):
        spec = registration(platform)
        assert spec is not None
        assert spec.create_url.startswith("https://")
        assert spec.scopes
        assert len(spec.steps) >= 2
