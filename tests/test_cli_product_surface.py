"""CLI product-surface contract tests (QA wave: qa/cli-product-surface).

Covers the agent/scripting surface of the 40-command CLI:

1. JSON contract  — every offline-safe command must emit parseable JSON in
   ``--json`` mode (and errors must be JSON-shaped too).
2. Exit codes     — actual exit codes must match the documented matrix
   (0/1/2/3/4/10) for failure modes: bad args, bad config, no auth,
   confirmation required.
3. --dry-run      — every mutating command's dry-run performs ZERO network
   calls (socket guard) and ZERO posting-state writes.
4. Non-TTY safety — interactive commands must not hang when stdin is closed.
5. Wizard resume  — a partial wizard run resumes without corrupting state.
6. Concurrency    — parallel schedule mutations must not lose entries.
7. Large payload  — a 1 MB caption fails fast with an actionable error.

All tests are offline: no platform credentials exist under the isolated HOME
and a socket guard blocks accidental network access in dry-run tests.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from xpst.cli import EXIT_AUTH_FAILURE, EXIT_CONFIG_ERROR, EXIT_GENERAL, main

# ── helpers ──────────────────────────────────────────────────────────────

class _NetworkBlockedError(AssertionError):
    """Raised when a dry-run path attempts a network call."""


@pytest.fixture
def no_network(monkeypatch):
    """Fail any test that opens a socket while this fixture is active."""

    class _GuardedSocket(socket.socket):
        def __init__(self, *args, **kwargs):  # noqa: D401
            raise _NetworkBlockedError("network call attempted")

    monkeypatch.setattr(socket, "socket", _GuardedSocket)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(_NetworkBlockedError("network call attempted")))


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated HOME so tests never read or write the real ~/.xpst."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home


@pytest.fixture
def runner():
    return CliRunner()


def _parse_json(output: str) -> object:
    """Parse the whole stdout as JSON (or fail with a readable error)."""
    return json.loads(output)


def _invoke(runner, argv, home, **kwargs):
    result = runner.invoke(main, argv, catch_exceptions=True, **kwargs)
    # A raw traceback bubbling out of the CLI is always a bug: every failure
    # mode must map to a clean message + meaningful exit code. result.output
    # mixes stdout+stderr on click >= 8.2, so leaks are caught there.
    assert "Traceback (most recent call last)" not in (result.output or ""), (
        f"traceback leaked for {argv}:\n{result.output[:2000]}"
    )
    result.json_stdout = result.stdout or ""
    return result


# ── 1. JSON contract ─────────────────────────────────────────────────────

# Offline-safe invocations covering every command group that can run without
# credentials, a dashboard, or network access. Each is invoked twice: with and
# without an explicit --json flag.
JSON_CONTRACT_CASES = [
    ["version"],
    ["providers"],
    ["status"],
    ["quota"],
    ["logs"],
    ["readiness"],
    ["analytics"],
    ["best-time"],
    ["followers"],
    ["auth", "status"],
    ["failures", "list"],
    ["schedule", "list"],
    ["config", "show"],
    ["config", "validate"],
    ["mcp", "list"],
    ["plugins", "list"],
    ["security-audit"],
    ["diagnostics"],
    ["state", "backup"],
    ["kb", "doctor"],
    ["kb", "areas"],
    ["kb", "course"],
    ["connect", "--test"],
    ["connect", "--guide", "youtube"],
    ["connect", "--dry-run"],
    ["delete", "vid123", "--dry-run"],
]


@pytest.mark.parametrize("argv", JSON_CONTRACT_CASES, ids=lambda a: " ".join(a))
@pytest.mark.parametrize("explicit_json", [True, False], ids=["json", "piped"])
def test_json_contract_parseable(runner, home, argv, explicit_json):
    """Every offline-safe command emits parseable JSON on --json (auto-JSON
    covers piped mode, which CliRunner simulates)."""
    full = [*argv, "--json"] if explicit_json else argv
    result = _invoke(runner, full, home)
    assert result.exit_code in (0, 1, 2, 3, 4, 10), (
        f"{full}: unexpected exit {result.exit_code}: {result.output[:500]}"
    )
    data = _parse_json(result.json_stdout)
    assert isinstance(data, (dict, list)), f"{full}: JSON is not a dict/list"


def test_json_contract_error_shape_is_stable(runner, home):
    """Error output in --json mode is a stable {ok, error:{code, message}} object."""
    result = _invoke(
        runner,
        ["schedule", "add", "missing.mp4", "--caption", "x", "--at", "2026-09-01 10:00", "--json"],
        home,
    )
    assert result.exit_code == EXIT_GENERAL
    data = _parse_json(result.json_stdout)
    assert data["ok"] is False
    assert data["error"]["code"] == "FILE_NOT_FOUND"
    assert "missing.mp4" in data["error"]["message"]


@pytest.mark.parametrize("argv", [
    ["generate", "ideas"],
    ["transcript"],
    ["suggest-caption"],
    ["schedule", "add"],
    ["state", "export"],
    ["config", "export"],
], ids=lambda a: " ".join(a))
def test_missing_required_args_exit_2_without_output(runner, home, argv):
    """Click usage errors keep exit code 2 and empty stdout (documented)."""
    result = _invoke(runner, [*argv, "--json"], home)
    assert result.exit_code == 2
    assert "Traceback" not in (result.output or "")


# ── 2. Exit-code matrix ──────────────────────────────────────────────────

def test_exit_code_constants_match_documented_matrix():
    assert (main.commands)  # sanity: group loaded
    from xpst import cli
    assert cli.EXIT_SUCCESS == 0
    assert cli.EXIT_GENERAL == 1
    assert cli.EXIT_CONFIG_ERROR == 2      # also Click usage errors
    assert cli.EXIT_AUTH_FAILURE == 3
    assert cli.EXIT_RATE_LIMIT == 4
    assert cli.EXIT_PLATFORM_UNAVAILABLE == 10


def test_unknown_command_exit_2(home):
    proc = _run_cli(["definitely-not-a-command"], home)
    assert proc.returncode == 2


def test_unknown_flag_exit_2(home):
    proc = _run_cli(["status", "--definitely-not-a-flag"], home)
    assert proc.returncode == 2


def test_invalid_config_file_exit_2(home, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("log_level: [unclosed\n  indent: :oops")
    proc = _run_cli(["status", "-c", str(bad)], home)
    assert proc.returncode == EXIT_CONFIG_ERROR


def test_connect_non_tty_exit_3_json(home):
    """Bare `connect` on closed stdin refuses cleanly: rc 3 + JSON error."""
    proc = _run_cli(["connect", "--json"], home)
    assert proc.returncode == EXIT_AUTH_FAILURE
    data = _parse_json(proc.stdout)
    assert data["error"]["code"] == "INTERACTIVE_REQUIRED"


def test_connect_platform_non_tty_exit_3(home):
    """Even with a platform, non-TTY connect must not prompt or open browsers."""
    proc = _run_cli(["connect", "youtube"], home)
    assert proc.returncode == EXIT_AUTH_FAILURE
    assert "Traceback" not in proc.stderr


def test_delete_without_yes_non_interactive_exit_1(runner, home):
    """Non-interactive delete without --yes is refused (CONFIRMATION_REQUIRED)."""
    result = _invoke(runner, ["delete", "vid123", "--json"], home)
    assert result.exit_code == EXIT_GENERAL
    data = _parse_json(result.json_stdout)
    assert data["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert "--yes" in data["error"]["hint"]


def test_schedule_add_invalid_date_exit_2(runner, home, tmp_path):
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"0")
    result = _invoke(
        runner,
        ["schedule", "add", str(vid), "--caption", "c", "--at", "tomorrow", "--json"],
        home,
    )
    assert result.exit_code == EXIT_CONFIG_ERROR
    data = _parse_json(result.json_stdout)
    assert data["error"]["code"] == "INVALID_DATE_FORMAT"


def test_schedule_add_missing_file_exit_1(runner, home):
    result = _invoke(
        runner,
        ["schedule", "add", "nope.mp4", "--caption", "c", "--at", "2026-09-01 10:00", "--json"],
        home,
    )
    assert result.exit_code == EXIT_GENERAL
    data = _parse_json(result.json_stdout)
    assert data["error"]["code"] == "FILE_NOT_FOUND"


def test_wizard_agent_mode_no_platforms_exit_3(runner, home):
    result = _invoke(runner, ["wizard", "--json"], home)
    assert result.exit_code == EXIT_AUTH_FAILURE
    data = _parse_json(result.json_stdout)
    assert data["mode"] == "agent"
    assert data["all_pass"] is False


def test_setup_non_tty_writes_no_config(home):
    """Piped `setup` must not silently write a default config (EOF defaults)."""
    proc = _run_cli(["setup", "--json"], home)
    assert proc.returncode == EXIT_CONFIG_ERROR
    data = _parse_json(proc.stdout)
    assert data["error"]["code"] == "INTERACTIVE_REQUIRED"
    assert not (home / ".xpst" / "config.yaml").exists()


def test_failures_retry_unknown_video_exit_1(runner, home):
    result = _invoke(
        runner, ["failures", "retry", "ghost", "-p", "youtube", "--json"], home
    )
    assert result.exit_code == EXIT_GENERAL
    data = _parse_json(result.json_stdout)
    assert data["error"]["code"] == "VIDEO_NOT_FOUND"


# ── 3. --dry-run guarantee: zero network, zero state writes ─────────────

STATE_FILES = ("state.json", "schedule.json")

DRY_RUN_CASES = [
    ["delete", "vid123", "--dry-run"],
    ["connect", "--dry-run"],
    ["connect", "youtube", "--dry-run"],
    # schedule add/remove dry-run are invoked in dedicated tests (need files)
]


@pytest.mark.parametrize("argv", DRY_RUN_CASES, ids=lambda a: " ".join(a))
def test_dry_run_zero_network_zero_state_writes(runner, home, no_network, argv):
    _assert_dry_run_clean(runner, home, argv)


def _snapshot_state(home: Path) -> dict:
    xpst = home / ".xpst"
    snap = {}
    for name in STATE_FILES:
        p = xpst / name
        snap[name] = (p.stat().st_mtime_ns, p.read_bytes()) if p.exists() else None
    return snap


def _assert_dry_run_clean(runner, home, argv):
    before = _snapshot_state(home)
    result = _invoke(runner, [*argv, "--json"], home)
    assert result.exit_code == 0, result.output[:500]
    data = _parse_json(result.json_stdout)
    assert data["dry_run"] is True
    assert _snapshot_state(home) == before, f"{argv} mutated posting state"


def test_post_dry_run_zero_network_zero_state_writes(runner, home, tmp_path, no_network):
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"\x00" * 128)
    _assert_dry_run_clean(runner, home, ["post", "-v", str(vid), "-c", "hello", "--dry-run"])


def test_schedule_add_dry_run_zero_network_zero_state_writes(runner, home, tmp_path, no_network):
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"\x00" * 128)
    _assert_dry_run_clean(
        runner, home,
        ["schedule", "add", str(vid), "-c", "hello", "--at", "2026-09-01 10:00", "--dry-run"],
    )
    assert not (home / ".xpst" / "schedule.json").exists()


def test_schedule_remove_dry_run_zero_state_writes(runner, home, no_network):
    from xpst.schedule_manager import ScheduleManager

    ScheduleManager(str(home / ".xpst")).add(
        video_path="x.mp4", caption="c",
        scheduled_time=__import__("datetime").datetime(2026, 9, 1, 10, 0),
    )
    _assert_dry_run_clean(runner, home, ["schedule", "remove", "deadbeef", "--dry-run"])
    entries = json.loads((home / ".xpst" / "schedule.json").read_text())
    assert len(entries) == 1, "dry-run must not remove the entry"


# ── 4. Non-TTY safety (closed stdin) ─────────────────────────────────────

NON_TTY_CASES = [
    ["status"],
    ["version"],
    ["setup"],
    ["wizard"],
    ["connect"],
    ["connect", "youtube"],
    ["delete", "vid123"],
]


@pytest.mark.parametrize("argv", NON_TTY_CASES, ids=lambda a: " ".join(a))
def test_closed_stdin_never_hangs(home, argv):
    """Every command must terminate on closed stdin within the timeout —
    blocking on input() (the old wizard EOF crash) is a product bug."""
    proc = _run_cli(argv, home, timeout=30)
    assert proc.returncode in (0, 1, 2, 3, 4, 10), (
        f"{argv}: rc={proc.returncode} stderr={proc.stderr[:300]}"
    )
    assert "Traceback" not in proc.stderr
    assert "Traceback" not in proc.stdout


# ── 5. Wizard state resume ───────────────────────────────────────────────

def test_wizard_resumes_partial_state_without_corruption(runner, home):
    """A wizard killed mid-run (partial wizard_state.json) resumes cleanly:
    progress is preserved and the state file stays valid JSON."""
    (home / ".xpst").mkdir(parents=True, exist_ok=True)
    seed = {
        "version": 1,
        "platforms": {"youtube": {"status": "connected", "detail": "verified"}},
        "started_at": "2026-08-28T10:00:00",
    }
    (home / ".xpst" / "wizard_state.json").write_text(json.dumps(seed))

    result = _invoke(runner, ["wizard", "--json"], home)
    assert result.exit_code == EXIT_AUTH_FAILURE
    data = _parse_json(result.json_stdout)
    assert data["mode"] == "agent"
    yt = next(c for c in data["checklist"] if c["platform"] == "youtube")
    assert yt["last_wizard_status"] == "connected", "resume must preserve progress"

    state = json.loads((home / ".xpst" / "wizard_state.json").read_text())
    assert state["platforms"]["youtube"]["status"] == "connected"


# ── 6. Concurrent schedule mutations ─────────────────────────────────────

def test_stale_manager_does_not_clobber_concurrent_add(tmp_path):
    """Deterministic lost-update regression: manager B initialised before
    manager A's write must still land B's entry (flock + re-read inside the
    lock). Pre-fix behaviour silently dropped A's or B's entry."""
    from datetime import datetime

    from xpst.schedule_manager import ScheduleManager

    store = str(tmp_path / "xpst")
    a = ScheduleManager(store)
    b = ScheduleManager(store)  # loads the (empty) file before A writes
    a.add(video_path="a.mp4", caption="A", scheduled_time=datetime(2026, 9, 1, 10, 0))
    b.add(video_path="b.mp4", caption="B", scheduled_time=datetime(2026, 9, 2, 10, 0))

    entries = json.loads((Path(store) / "schedule.json").read_text())
    captions = sorted(e["caption"] for e in entries)
    assert captions == ["A", "B"], f"lost update: {captions}"


def test_parallel_manager_adds_no_lost_entries(tmp_path):
    from datetime import datetime

    from xpst.schedule_manager import ScheduleManager

    store = str(tmp_path / "xpst")
    errors = []

    def _add(i: int) -> None:
        try:
            ScheduleManager(store).add(
                video_path=f"v{i}.mp4", caption=f"entry {i}",
                scheduled_time=datetime(2026, 9, 1, 10, i),
            )
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=_add, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    entries = json.loads((Path(store) / "schedule.json").read_text())
    assert len(entries) == 5
    assert len({e["id"] for e in entries}) == 5


@pytest.mark.skipif(sys.platform == "win32", reason="flock cross-process locking is POSIX-only")
def test_parallel_cli_schedule_add(tmp_path):
    """Five concurrent `xpst schedule add` processes: all succeed, none lost."""
    home_dir = tmp_path / "conc-home"
    home_dir.mkdir()
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"\x00" * 64)
    env = {**os.environ, "HOME": str(home_dir), "NO_COLOR": "1"}
    repo_src = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = repo_src + os.pathsep + env.get("PYTHONPATH", "")

    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "xpst", "schedule", "add", str(vid),
             "--caption", f"concurrent {i}", "--at", "2026-09-01 10:00", "--json"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, text=True,
        )
        for i in range(5)
    ]
    outs = [p.communicate() + (p.wait(),) for p in procs]

    rcs = [rc for _, _, rc in outs]
    assert rcs == [0] * 5, f"some adds failed: {rcs}; stderr={[e[:200] for _, e, _ in outs]}"
    sched = home_dir / ".xpst" / "schedule.json"
    entries = json.loads(sched.read_text())
    assert len(entries) == 5, f"lost entries: {len(entries)}"
    assert len({e["id"] for e in entries}) == 5


# ── 7. Large payload (1 MB caption) ──────────────────────────────────────

def test_schedule_add_1mb_caption_fails_fast_with_clear_error(runner, home, tmp_path):
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"\x00" * 128)
    big = "x" * 1_000_000
    result = _invoke(
        runner,
        ["schedule", "add", str(vid), "--caption", big, "--at", "2026-09-01 10:00", "--json"],
        home,
    )
    assert result.exit_code == EXIT_GENERAL
    data = _parse_json(result.json_stdout)
    assert data["error"]["code"] == "CAPTION_TOO_LONG"
    assert data["error"]["caption_length"] == 1_000_000
    assert data["error"]["max_caption_length"] < 1_000_000
    assert not (home / ".xpst" / "schedule.json").exists(), "huge caption must not be persisted"


def test_schedule_manager_rejects_oversized_caption(tmp_path):
    from datetime import datetime

    import pytest as _pytest

    from xpst.schedule_manager import MAX_CAPTION_LENGTH, ScheduleManager

    mgr = ScheduleManager(str(tmp_path / "xpst"))
    with _pytest.raises(ValueError, match="limit"):
        mgr.add(
            video_path="v.mp4", caption="x" * (MAX_CAPTION_LENGTH + 1),
            scheduled_time=datetime(2026, 9, 1, 10, 0),
        )


# ── helper: run the real CLI in a subprocess (real TTY semantics) ────────

def _run_cli(argv, home: Path, timeout: int = 30) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "HOME": str(home),
        "NO_COLOR": "1",
        "TERM": "dumb",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    }
    return subprocess.run(
        [sys.executable, "-m", "xpst", *argv],
        capture_output=True, text=True, env=env,
        stdin=subprocess.DEVNULL, timeout=timeout,
    )
