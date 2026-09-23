"""Bounded background token refresh (``xpst.token_refresh``).

The refresh job is what keeps a green badge true without bothering a human. It
must be: bounded (a fixed attempt budget + wall-clock deadline), secret-free
(nothing token-shaped in a report, a log line or a badge reason), and honest
(a recorded failure keeps the badge red instead of promising a silent retry).

Refreshers are injected here — no network, no real OAuth client.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from xpst.config import XPSTConfig
from xpst.token_refresh import (
    REFRESH_DRIVERS,
    RefreshRun,
    due_platforms,
    load_refresh_report,
    redact,
    refresh_due_tokens_async,
    report_path,
    save_refresh_report,
)
from xpst.token_state import (
    BADGE_EXPIRING,
    BADGE_NEEDS_REAUTH,
    derive_token_state,
    iso_timestamp,
    token_metadata,
)

NOW = 1_800_000_000.0
HOUR = 3600.0

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX file-permission semantics",
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _expired_youtube_config(tmp_path, *, expires_in: float = -HOUR) -> XPSTConfig:
    """A YouTube account whose token file says 'expired, refreshable'.

    ``expires_in`` is relative to ``NOW`` so the due-ness rules are testable
    without depending on the wall clock.
    """
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    token_file = tmp_path / "youtube_token.json"
    token_file.write_text(
        json.dumps(
            {
                "token": "ya29.SECRET-ACCESS",
                "refresh_token": "1//SECRET-REFRESH",
                "client_id": "cid",
                "client_secret": "SECRET-CLIENT",
                "expiry": iso_timestamp(NOW + expires_in),
            }
        ),
        encoding="utf-8",
    )
    config.youtube.token_file = str(token_file)
    # Everything else off: the job must only ever touch what is due.
    for platform in ("x", "instagram", "tiktok", "threads", "messenger"):
        getattr(config, platform).enabled = False
    return config


class _StubDriver:
    """Records calls, optionally failing N times (the stubbed client)."""

    def __init__(self, failures: int = 0, error: str = "invalid_grant") -> None:
        self.calls = 0
        self.failures = failures
        self.error = error

    async def __call__(self, config) -> None:  # noqa: ANN001, ARG002
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError(self.error)


def _stalled_driver(driver: _StubDriver):
    """A sleep replacement that records the backoff and never really sleeps."""
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    return sleep, slept


# ── due-ness ────────────────────────────────────────────────────────────────


def test_due_platforms_needs_a_refresh_path(tmp_path):
    config = _expired_youtube_config(tmp_path)
    assert due_platforms(config, now=NOW) == ["youtube"]


def test_nothing_is_due_for_a_healthy_account(tmp_path):
    """A live, far-from-expiry token is left alone."""
    config = _expired_youtube_config(tmp_path, expires_in=6 * HOUR)
    live = {
        "youtube": {
            "authenticated": True,
            "session_valid": True,
            "live_checked": True,
            "checked_at": NOW,
            "error": None,
        }
    }
    assert due_platforms(config, live, now=NOW) == []


def test_an_imminent_expiry_is_refreshed_proactively(tmp_path):
    """Renew before the token dies, not after a post fails."""
    config = _expired_youtube_config(tmp_path, expires_in=5 * 60)
    live = {
        "youtube": {
            "authenticated": True,
            "session_valid": True,
            "live_checked": True,
            "checked_at": NOW,
            "error": None,
        }
    }
    assert due_platforms(config, live, now=NOW) == ["youtube"]


def test_force_includes_platforms_with_a_refresh_path(tmp_path):
    config = _expired_youtube_config(tmp_path)
    live = {"youtube": {"authenticated": True, "session_valid": True, "live_checked": True, "checked_at": NOW}}
    assert due_platforms(config, live, now=NOW, force=True) == ["youtube"]


def test_due_platforms_ignores_platforms_without_a_refresh_path(tmp_path):
    """X cookie sessions cannot be refreshed — never attempted."""
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.x.cookies_file = str(tmp_path / "cookies.json")
    (tmp_path / "cookies.json").write_text("{}", encoding="utf-8")
    assert "x" not in due_platforms(config, now=NOW, force=True)
    assert all(name != "x" for name in REFRESH_DRIVERS)


# ── refresh-on-expiry with a stubbed client ─────────────────────────────────


def test_refresh_runs_when_the_token_expired(tmp_path):
    config = _expired_youtube_config(tmp_path)
    driver = _StubDriver()
    sleep, slept = _stalled_driver(driver)

    report = asyncio.run(
        refresh_due_tokens_async(config, drivers={"youtube": driver}, sleep=sleep, now=NOW)
    )

    assert driver.calls == 1
    assert report["youtube"]["ok"] is True
    assert report["youtube"]["attempts"] == 1
    assert report["youtube"]["attempted"] is True
    assert slept == [], "a successful refresh must not back off"


def test_no_work_when_nothing_is_due(tmp_path):
    config = _expired_youtube_config(tmp_path, expires_in=6 * HOUR)
    driver = _StubDriver()
    live = {"youtube": {"authenticated": True, "session_valid": True, "live_checked": True, "checked_at": NOW}}

    report = asyncio.run(
        refresh_due_tokens_async(config, live=live, drivers={"youtube": driver}, now=NOW)
    )

    assert report == {}
    assert driver.calls == 0


# ── bounded retry ───────────────────────────────────────────────────────────


def test_retry_is_bounded_and_backs_off(tmp_path):
    config = _expired_youtube_config(tmp_path)
    driver = _StubDriver(failures=5)
    sleep, slept = _stalled_driver(driver)

    report = asyncio.run(
        refresh_due_tokens_async(
            config,
            drivers={"youtube": driver},
            sleep=sleep,
            max_attempts=3,
            base_delay=1.0,
            deadline=60.0,
            now=NOW,
        )
    )

    outcome = report["youtube"]
    assert driver.calls == 3, "attempt budget exceeded"
    assert outcome["ok"] is False
    assert outcome["attempts"] == 3
    assert outcome["error"]
    assert slept == [1.0, 2.0], "exponential backoff between attempts"


def test_a_failing_first_attempt_recovers_on_retry(tmp_path):
    config = _expired_youtube_config(tmp_path)
    driver = _StubDriver(failures=1)
    sleep, _ = _stalled_driver(driver)

    report = asyncio.run(
        refresh_due_tokens_async(config, drivers={"youtube": driver}, sleep=sleep, now=NOW)
    )

    assert driver.calls == 2
    assert report["youtube"]["ok"] is True
    assert report["youtube"]["attempts"] == 2


def test_deadline_stops_further_retries(tmp_path):
    config = _expired_youtube_config(tmp_path)
    driver = _StubDriver(failures=5)
    sleep, slept = _stalled_driver(driver)

    report = asyncio.run(
        refresh_due_tokens_async(
            config,
            drivers={"youtube": driver},
            sleep=sleep,
            max_attempts=5,
            base_delay=10.0,
            deadline=15.0,
            now=NOW,
        )
    )

    assert driver.calls == 2, "the wall-clock deadline must cut the retries short"
    assert "retry budget exhausted" in report["youtube"]["error"]
    assert slept == [10.0]


def test_single_attempt_budget(tmp_path):
    config = _expired_youtube_config(tmp_path)
    driver = _StubDriver(failures=5)
    sleep, _ = _stalled_driver(driver)

    report = asyncio.run(
        refresh_due_tokens_async(
            config, drivers={"youtube": driver}, sleep=sleep, max_attempts=1, now=NOW
        )
    )

    assert driver.calls == 1
    assert report["youtube"]["attempts"] == 1


def test_one_broken_platform_does_not_stop_the_others(tmp_path):
    config = _expired_youtube_config(tmp_path)
    config.threads.enabled = True
    config.threads.graph_access_token = "THREADS-TOKEN"
    config.threads.threads_user_id = "123"
    broken = _StubDriver(failures=99)
    healthy = _StubDriver()
    sleep, _ = _stalled_driver(healthy)

    report = asyncio.run(
        refresh_due_tokens_async(
            config,
            drivers={"youtube": broken, "threads": healthy},
            platforms=["youtube", "threads"],
            sleep=sleep,
            max_attempts=2,
            now=NOW,
        )
    )

    assert report["youtube"]["ok"] is False
    assert report["threads"]["ok"] is True


# ── no token material, ever ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        "refresh failed for token ya29.SECRET-VALUE-HERE",
        "invalid_client: access_token=SUPERSECRET123&x=1",
        "HTTP 400 refresh_token=1//0gVERYSECRET",
        "EAAABCDEFGHIJKLMNOPQRSTUVWXYZ leaked",
    ],
)
def test_redact_strips_credential_material(message):
    cleaned = redact(message)
    assert "SECRET" not in cleaned
    assert "SUPERSECRET123" not in cleaned
    assert "EAAABCDEFGHIJKLMNOPQRSTUVWXYZ" not in cleaned


def test_report_never_contains_secrets_even_when_the_client_raises_them(tmp_path):
    config = _expired_youtube_config(tmp_path)
    driver = _StubDriver(failures=9, error="boom token=ya29.TOP-SECRET-ACCESS refresh_token=1//TOPSECRET")
    sleep, _ = _stalled_driver(driver)

    report = asyncio.run(
        refresh_due_tokens_async(config, drivers={"youtube": driver}, sleep=sleep, max_attempts=2, now=NOW)
    )

    blob = json.dumps(report)
    assert "TOP-SECRET-ACCESS" not in blob
    assert "1//TOPSECRET" not in blob
    assert "[redacted]" in report["youtube"]["error"]


def test_redact_truncates_long_upstream_errors():
    assert len(redact("x" * 5000)) <= 200


# ── persistence ─────────────────────────────────────────────────────────────


def test_report_round_trips_and_is_owner_only(tmp_path):
    config = _expired_youtube_config(tmp_path)
    save_refresh_report(
        config,
        {"youtube": {"platform": "youtube", "attempted": True, "ok": False, "attempts": 3, "error": "nope"}},
    )

    path = report_path(config)
    assert path.exists()
    assert load_refresh_report(config)["youtube"]["attempts"] == 3


@_POSIX_ONLY
def test_report_file_is_0600(tmp_path):
    config = _expired_youtube_config(tmp_path)
    save_refresh_report(config, {"youtube": {"ok": True, "attempted": True, "attempts": 1}})
    assert _mode(report_path(config)) == 0o600
    assert _mode(report_path(config)) & (stat.S_IRGRP | stat.S_IROTH) == 0


def test_missing_or_corrupt_report_is_not_fatal(tmp_path):
    config = _expired_youtube_config(tmp_path)
    assert load_refresh_report(config) == {}

    report_path(config).write_text("{not json", encoding="utf-8")
    assert load_refresh_report(config) == {}

    report_path(config).write_text(json.dumps(["not", "a", "mapping"]), encoding="utf-8")
    assert load_refresh_report(config) == {}


def test_save_ignores_an_empty_report(tmp_path):
    config = _expired_youtube_config(tmp_path)
    save_refresh_report(config, {})
    assert not report_path(config).exists()


def test_a_recorded_failure_keeps_the_badge_red(tmp_path):
    """The persisted outcome is what stops the badge promising a silent retry."""
    config = _expired_youtube_config(tmp_path)
    save_refresh_report(
        config,
        {
            "youtube": {
                "platform": "youtube",
                "attempted": True,
                "ok": False,
                "attempts": 3,
                "error": "invalid_grant",
                "refreshed_at": NOW - 30,
            }
        },
    )
    report = load_refresh_report(config)
    live = {
        "authenticated": False,
        "session_valid": False,
        "live_checked": True,
        "checked_at": NOW,
        "error": "YouTube credentials expired",
        "auth_mode": "oauth",
    }

    info = derive_token_state(
        "youtube", live, token_metadata(config, "youtube"), refresh=report["youtube"], now=NOW
    )

    assert info["badge"] == BADGE_NEEDS_REAUTH
    assert "3 attempt" in info["badge_reason"]


def test_a_fresh_refresh_keeps_the_badge_amber_not_green(tmp_path):
    """Refreshing is not proof: only a passing live check turns the badge green."""
    config = _expired_youtube_config(tmp_path)
    live = {
        "authenticated": False,
        "session_valid": False,
        "live_checked": True,
        "checked_at": NOW,
        "error": "YouTube credentials expired",
        "auth_mode": "oauth",
    }
    info = derive_token_state(
        "youtube",
        live,
        token_metadata(config, "youtube"),
        refresh={"ok": True, "attempts": 1, "refreshed_at": NOW},
        now=NOW,
    )
    assert info["badge"] == BADGE_EXPIRING
    assert info["badge"] != "connected"


# ── RefreshRun summary ──────────────────────────────────────────────────────


def test_refresh_run_summarises_outcomes():
    run = RefreshRun(
        report={
            "youtube": {"attempted": True, "ok": True, "attempts": 1},
            "threads": {"attempted": True, "ok": False, "attempts": 3},
        }
    )
    assert run.succeeded == ["youtube"]
    assert run.failed == ["threads"]
    assert sorted(run.attempted) == ["threads", "youtube"]
    assert run.to_dict()["failed"] == ["threads"]


# ── CLI surface ─────────────────────────────────────────────────────────────


def test_cli_refresh_tokens_reports_the_outcome(tmp_path, monkeypatch):
    import xpst.token_refresh as token_refresh

    config_file = tmp_path / "config.yaml"
    XPSTConfig().save(str(config_file))

    monkeypatch.setattr(
        token_refresh,
        "refresh_due_tokens",
        lambda config, **kwargs: {"youtube": {"attempted": True, "ok": True, "attempts": 1, "reason": "refreshed on attempt 1"}},
    )

    from click.testing import CliRunner

    from xpst.cli import main

    result = CliRunner().invoke(
        main, ["--config", str(config_file), "refresh-tokens", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["count"] == 1
    assert "youtube" in payload["attempted"]


def test_cli_refresh_tokens_is_a_noop_when_nothing_is_due(tmp_path, monkeypatch):
    import xpst.token_refresh as token_refresh

    config_file = tmp_path / "config.yaml"
    XPSTConfig().save(str(config_file))
    monkeypatch.setattr(token_refresh, "refresh_due_tokens", lambda config, **kwargs: {})

    from click.testing import CliRunner

    from xpst.cli import main

    result = CliRunner().invoke(
        main, ["--config", str(config_file), "refresh-tokens", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["count"] == 0
    assert payload["refreshed"] == {}


def test_cli_auth_status_refresh_reports_what_it_renewed(tmp_path, monkeypatch):
    """`auth status --refresh` renews first, then reports from the new state."""
    import xpst.auth_status as auth_status
    import xpst.token_refresh as token_refresh

    config_file = tmp_path / "config.yaml"
    XPSTConfig().save(str(config_file))
    monkeypatch.setattr(
        token_refresh,
        "refresh_due_tokens",
        lambda config, **kwargs: {"youtube": {"attempted": True, "ok": True, "attempts": 1}},
    )
    monkeypatch.setattr(auth_status, "collect_live_auth_status", lambda config: {})

    from click.testing import CliRunner

    from xpst.cli import main

    result = CliRunner().invoke(
        main, ["--config", str(config_file), "auth", "status", "--refresh", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["refresh"]["youtube"]["ok"] is True


def test_refresh_report_path_is_inside_the_config_dir(tmp_path):
    config = _expired_youtube_config(tmp_path)
    assert report_path(config).parent == Path(tmp_path)
    assert report_path(config).name == "token_refresh.json"


def test_save_report_survives_a_readonly_dir(tmp_path, monkeypatch):
    config = _expired_youtube_config(tmp_path)

    def boom(*args, **kwargs):
        raise OSError("read-only filesystem")

    import xpst.token_refresh as token_refresh

    monkeypatch.setattr(token_refresh, "write_text_0600", boom)
    save_refresh_report(config, {"youtube": {"ok": True}})  # must not raise
    assert not report_path(config).exists()


def test_env_does_not_leak_into_the_report(tmp_path, monkeypatch):
    config = _expired_youtube_config(tmp_path)
    monkeypatch.setenv("XPST_FAKE_SECRET", "ya29.ENV-SECRET-TOKEN")
    driver = _StubDriver(failures=1, error="failed with ya29.ENV-SECRET-TOKEN")
    sleep, _ = _stalled_driver(driver)

    report = asyncio.run(
        refresh_due_tokens_async(config, drivers={"youtube": driver}, sleep=sleep, max_attempts=1, now=NOW)
    )
    assert "ENV-SECRET-TOKEN" not in json.dumps(report)
    assert os.environ.get("XPST_FAKE_SECRET")
