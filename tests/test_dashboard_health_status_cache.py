"""The Home screen must not block on a live auth probe.

`/api/health-status` runs live platform probes and took ~5.6s per call on this
machine, so the Dashboard rendered its loading skeleton for the whole duration —
which reads to a user as "the app is broken". The probe is now cached for a
short TTL and the response says whether the answer came from cache.
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst.dashboard import api as api_module
from xpst.dashboard.api import create_api_router


def _client(tmp_path) -> TestClient:
    # The handler loads <config_dir>/config.yaml, so the fixture must exist --
    # otherwise the request takes the error path and never probes.
    from xpst.config import XPSTConfig

    XPSTConfig().save(str(tmp_path / "config.yaml"))
    app = FastAPI()
    app.include_router(create_api_router(str(tmp_path)))
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    monkeypatch.setenv("XPST_DISABLE_AUTH_WARM", "1")  # tests stay offline
    api_module._AUTH_STATUS_CACHE.clear()
    api_module._AUTH_STATUS_REFRESHING.clear()
    yield
    api_module._AUTH_STATUS_CACHE.clear()
    api_module._AUTH_STATUS_REFRESHING.clear()


def _stub_probe(monkeypatch, calls: dict[str, int], payload: dict | None = None):
    """Replace the live probe with a counter + fixed payload."""
    fake = payload or {"youtube": {"state": "ready"}}

    def fake_collect(config):  # noqa: ANN001, ARG001
        calls["probe"] = calls.get("probe", 0) + 1
        return fake

    monkeypatch.setattr("xpst.auth_status.collect_live_auth_status", fake_collect)
    monkeypatch.setattr(
        "xpst.provider_truth.canonical_status_report",
        lambda config, auth: {"providers": {"youtube": {"role_status": {"video_destination": {"state": "ready", "enabled": True}}}}, "platforms": {}, "roles": []},
    )


def test_second_call_within_ttl_skips_the_live_probe(tmp_path, monkeypatch) -> None:
    calls: dict[str, int] = {}
    _stub_probe(monkeypatch, calls)
    monkeypatch.setenv("XPST_AUTH_STATUS_TTL", "60")

    client = _client(tmp_path)
    first = client.get("/api/health-status").json()
    second = client.get("/api/health-status").json()

    assert calls["probe"] == 1, "the live probe ran twice inside the TTL"
    assert first["auth_cached"] is False
    assert second["auth_cached"] is True
    assert second["auth_age_seconds"] is not None


def test_ttl_zero_disables_the_cache(tmp_path, monkeypatch) -> None:
    calls: dict[str, int] = {}
    _stub_probe(monkeypatch, calls)
    monkeypatch.setenv("XPST_AUTH_STATUS_TTL", "0")

    client = _client(tmp_path)
    client.get("/api/health-status")
    client.get("/api/health-status")

    assert calls["probe"] == 2, "TTL=0 must re-probe every time"


def test_expired_entry_is_served_stale_and_refreshed_behind_the_request(tmp_path, monkeypatch) -> None:
    """An expired entry must never block the request on a live probe."""
    calls: dict[str, int] = {}
    _stub_probe(monkeypatch, calls)
    monkeypatch.setenv("XPST_AUTH_STATUS_TTL", "30")

    client = _client(tmp_path)
    first = client.get("/api/health-status").json()
    assert first["auth_stale"] is False

    # Age the cache entry past the TTL instead of sleeping.
    key = next(iter(api_module._AUTH_STATUS_CACHE))
    stamp, auth, canonical = api_module._AUTH_STATUS_CACHE[key]
    api_module._AUTH_STATUS_CACHE[key] = (stamp - 120, auth, canonical)

    started = time.monotonic()
    aged = client.get("/api/health-status").json()
    elapsed = time.monotonic() - started

    assert aged["auth_cached"] is True, "the stale value should still be served"
    assert aged["auth_stale"] is True, "the response must admit the value is stale"
    assert elapsed < 1.0, f"a stale serve must not wait on the probe (took {elapsed:.2f}s)"

    # The refresh happens in the background; wait briefly for it to land.
    for _ in range(50):
        if calls.get("probe", 0) >= 2:
            break
        time.sleep(0.05)
    assert calls.get("probe", 0) >= 2, "no background refresh was scheduled"


def test_warm_cache_primes_the_answer(tmp_path, monkeypatch) -> None:
    """Startup warming means the first request is already served from cache."""
    calls: dict[str, int] = {}
    _stub_probe(monkeypatch, calls)
    monkeypatch.setenv("XPST_AUTH_STATUS_TTL", "60")
    monkeypatch.delenv("XPST_DISABLE_AUTH_WARM", raising=False)

    from xpst.config import XPSTConfig

    config = XPSTConfig()
    api_module.warm_auth_status_cache(str(tmp_path), config)

    for _ in range(50):
        if calls.get("probe", 0) >= 1:
            break
        time.sleep(0.05)
    assert calls.get("probe", 0) >= 1, "warming did not probe"

    client = _client(tmp_path)
    warmed = client.get("/api/health-status").json()
    assert warmed["auth_cached"] is True
    assert calls["probe"] == 1, "the first request should not probe again"


def test_warm_cache_is_disabled_with_zero_ttl(tmp_path, monkeypatch) -> None:
    calls: dict[str, int] = {}
    _stub_probe(monkeypatch, calls)
    monkeypatch.setenv("XPST_AUTH_STATUS_TTL", "0")
    monkeypatch.delenv("XPST_DISABLE_AUTH_WARM", raising=False)

    from xpst.config import XPSTConfig

    api_module.warm_auth_status_cache(str(tmp_path), XPSTConfig())
    time.sleep(0.1)
    assert not calls.get("probe"), "TTL=0 must not warm the cache"


def test_cached_response_keeps_the_same_readiness_verdict(tmp_path, monkeypatch) -> None:
    calls: dict[str, int] = {}
    _stub_probe(monkeypatch, calls)
    monkeypatch.setenv("XPST_AUTH_STATUS_TTL", "60")

    client = _client(tmp_path)
    first = client.get("/api/health-status").json()
    second = client.get("/api/health-status").json()

    for field in ("status", "providers", "readiness", "next_action", "can_create_post"):
        assert first[field] == second[field], field


def test_cold_probe_answers_immediately_with_a_pending_state(tmp_path, monkeypatch) -> None:
    """A first paint must never wait ~6s on the live probe."""
    import threading

    release = threading.Event()

    def blocking_probe(config):  # noqa: ANN001, ARG001
        release.wait(5)
        return {"youtube": {"state": "ready"}}

    monkeypatch.setattr("xpst.auth_status.collect_live_auth_status", blocking_probe)
    monkeypatch.setenv("XPST_AUTH_STATUS_TTL", "60")
    monkeypatch.delenv("XPST_DISABLE_AUTH_WARM", raising=False)

    from xpst.config import XPSTConfig

    # Simulate the startup warm-up already running.
    api_module.warm_auth_status_cache(str(tmp_path), XPSTConfig())

    client = _client(tmp_path)
    started = time.monotonic()
    payload = client.get("/api/health-status").json()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"cold request blocked for {elapsed:.2f}s"
    assert payload["readiness_pending"] is True
    assert payload["readiness"]["pending"] is True
    assert payload["auth_cached"] is False
    assert payload["next_action"]["kind"] == "checking"
    # Nothing may be claimed either way while the answer is unknown.
    assert payload["can_create_post"] is False
    assert payload["readiness"]["blockers"] == []

    release.set()


def test_resolved_response_clears_the_pending_flag(tmp_path, monkeypatch) -> None:
    calls: dict[str, int] = {}
    _stub_probe(monkeypatch, calls)
    monkeypatch.setenv("XPST_AUTH_STATUS_TTL", "60")

    payload = _client(tmp_path).get("/api/health-status").json()

    assert payload["readiness_pending"] is False
    assert "readiness_pending" in payload
