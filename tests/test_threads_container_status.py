"""Threads video publishing waits for container status=FINISHED and reports error_message.

The Meta Threads API create-container call only acknowledges the container;
video fetching/processing happens asynchronously and the verdict is only
visible on ``GET /{container-id}?fields=status,error_message``. Publishing an
unprocessed container is what produces Meta's opaque ``400 media not found``,
and ``error_message`` (e.g. ``FAILED_DOWNLOADING_VIDEO``) — the only place Meta
says *why* processing failed — was previously swallowed.

These tests pin the upload() sequence on a stubbed client:
- publish fires only after the status call reports FINISHED (or PUBLISHED);
- an ERROR container fails with Meta's own error_message in the error text and
  is not marked retryable;
- an IN_PROGRESS container is polled again instead of publishing early;
- the poll is bounded (THREADS_CONTAINER_TIMEOUT, retryable) — the container
  survives 24h, so a retry can still succeed.

Nothing touches the network; the sleep is stubbed so the poll loop is instant.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from xpst.config import XPSTConfig
from xpst.platforms import threads as threads_module
from xpst.platforms.threads import ThreadsUploader

PUBLIC_VIDEO_URL = "https://cdn.example.com/clip.mp4"


class _FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://graph.threads.net/v1.0/container-1")
            response = httpx.Response(self.status_code, request=request, text=self.text)
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=request, response=response)


class _ScriptedThreadsClient:
    """Async-context-manager stand-in for httpx.AsyncClient.

    POSTs answer the container/publish flow; GETs (the status poll) draw from a
    scripted queue of status payloads so each poll iteration can report a
    different status. Call order is recorded verbatim.
    """

    def __init__(
        self,
        *,
        status_payloads: list[Any],
        container_id: str = "container-1",
        media_id: str = "media-1",
    ) -> None:
        self._status_payloads = list(status_payloads)
        self._container_id = container_id
        self._media_id = media_id
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def __aenter__(self) -> _ScriptedThreadsClient:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def post(self, url: str, params: Any = None) -> _FakeResponse:
        self.calls.append(("post", url, dict(params or {})))
        if url.endswith("/threads"):
            return _FakeResponse({"id": self._container_id})
        if url.endswith("/threads_publish"):
            return _FakeResponse({"id": self._media_id})
        raise AssertionError(f"unexpected Threads POST {url}")

    async def get(self, url: str, params: Any = None) -> _FakeResponse:
        self.calls.append(("get", url, dict(params or {})))
        if not self._status_payloads:
            raise AssertionError("status polled after the scripted responses ran out")
        payload = self._status_payloads.pop(0)
        if isinstance(payload, int):  # an HTTP failure on the status query
            return _FakeResponse({"error": {"message": "status query failed"}}, status_code=payload)
        return _FakeResponse(payload)


@pytest.fixture(autouse=True)
def _no_real_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant(_delay: float) -> None:
        return None

    monkeypatch.setattr(threads_module.asyncio, "sleep", _instant)


@pytest.fixture
def config(tmp_path: Any) -> XPSTConfig:
    cfg = XPSTConfig()
    cfg.config_dir = str(tmp_path)
    cfg.threads.enabled = True
    cfg.threads.graph_access_token = "threads-token"
    cfg.threads.threads_user_id = "17841400000000000"
    return cfg


def _run(config: XPSTConfig, client: _ScriptedThreadsClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("xpst.platforms.threads.httpx.AsyncClient", lambda **_: client)
    uploader = ThreadsUploader(config)
    return asyncio.run(uploader.upload(PUBLIC_VIDEO_URL, "caption"))


def test_publish_waits_for_finished_and_polls_the_status_fields(
    config: XPSTConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """create → status(FINISHED) → publish, in that order, with error_message asked for."""
    client = _ScriptedThreadsClient(status_payloads=[{"id": "container-1", "status": "FINISHED"}])

    result = _run(config, client, monkeypatch)

    assert result.success is True, result.error
    sequence = [
        "create" if url.endswith("/threads") else "publish" if url.endswith("/threads_publish") else "status"
        for kind, url, _ in client.calls
        if kind in ("post", "get") and "/threads_insights" not in url
    ]
    # permalink lookup rides the same get() recorder; trim to the publish flow
    assert sequence[:3] == ["create", "status", "publish"], client.calls

    status_call = next(call for call in client.calls if call[0] == "get" and call[2].get("fields") == "status,error_message")
    assert status_call[1].endswith("/container-1"), "the status poll must target the container id"


def test_finished_is_polled_through_in_progress_without_publishing_early(
    config: XPSTConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _ScriptedThreadsClient(
        status_payloads=[
            {"id": "container-1", "status": "IN_PROGRESS"},
            {"id": "container-1", "status": "FINISHED"},
        ]
    )

    result = _run(config, client, monkeypatch)

    assert result.success is True, result.error
    status_calls = [call for call in client.calls if call[0] == "get" and call[2].get("fields") == "status,error_message"]
    assert len(status_calls) == 2, "IN_PROGRESS must poll again, not publish"


def test_error_container_surfaces_metas_error_message_verbatim(
    config: XPSTConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: FAILED_DOWNLOADING_VIDEO reaches the user, publish never fires."""
    client = _ScriptedThreadsClient(
        status_payloads=[{"id": "container-1", "status": "ERROR", "error_message": "FAILED_DOWNLOADING_VIDEO"}]
    )

    result = _run(config, client, monkeypatch)

    assert result.success is False
    assert result.error is not None
    assert result.error.startswith("THREADS_CONTAINER_ERROR")
    assert "FAILED_DOWNLOADING_VIDEO" in result.error, "Meta's reason must be reported verbatim"
    assert result.retryable is False
    assert not any(url.endswith("/threads_publish") for kind, url, _ in client.calls if kind == "post"), (
        "a dead container must never be published"
    )


def test_expired_container_is_reported_as_expired(
    config: XPSTConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _ScriptedThreadsClient(status_payloads=[{"id": "container-1", "status": "EXPIRED"}])

    result = _run(config, client, monkeypatch)

    assert result.success is False
    assert result.error is not None and result.error.startswith("THREADS_CONTAINER_EXPIRED")
    assert not any(url.endswith("/threads_publish") for kind, url, _ in client.calls if kind == "post")


def test_published_status_is_accepted_as_ready(
    config: XPSTConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _ScriptedThreadsClient(status_payloads=[{"id": "container-1", "status": "PUBLISHED"}])

    result = _run(config, client, monkeypatch)

    assert result.success is True, result.error


def test_poll_is_bounded_and_retryable(
    config: XPSTConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A never-FINISHING container stops at the deadline with a retriable failure."""
    monkeypatch.setattr(threads_module, "_CONTAINER_POLL_MAX_SECONDS", 0.0)
    client = _ScriptedThreadsClient(status_payloads=[{"id": "container-1", "status": "IN_PROGRESS"}] * 3)

    result = _run(config, client, monkeypatch)

    assert result.success is False
    assert result.error is not None and result.error.startswith("THREADS_CONTAINER_TIMEOUT")
    assert result.retryable is True, "the container lives 24h — a retry can still publish it"


def test_transient_status_query_failure_does_not_abort_the_poll(
    config: XPSTConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One flaky status GET is retried; the post still publishes."""
    client = _ScriptedThreadsClient(
        status_payloads=[503, {"id": "container-1", "status": "FINISHED"}]
    )

    result = _run(config, client, monkeypatch)

    assert result.success is True, result.error
