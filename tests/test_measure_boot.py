"""Contract tests for the cold-boot measurement harness and its CI wiring.

The boot budget only has value if three things stay true: the marker parsing
still matches what the shell prints, the budget comparison really fails when a
limit is exceeded, and both CI lanes actually invoke the harness. Each of those
is asserted here, because all three fail silently (a harness that parses nothing
or a lane that stopped calling it looks identical to a passing gate).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "measure_boot.py"
BUDGET = REPO_ROOT / "perf" / "boot-budget.json"
RELEASE_LANE = REPO_ROOT / ".github" / "workflows" / "tauri-release.yml"
BOOT_LANE = REPO_ROOT / ".github" / "workflows" / "boot-budget.yml"
METRIC = "BOOT_TO_READY_SECS"


def _load_module():
    spec = importlib.util.spec_from_file_location("measure_boot", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["measure_boot"] = module
    spec.loader.exec_module(module)
    return module


mb = _load_module()


# --------------------------------------------------------------------------
# marker parsing
# --------------------------------------------------------------------------
SHELL_LOG = """\
[xpst-shell] SHELL_STARTED pid=1234
[xpst-shell] BOOT_TO_VISIBLE_SECS=1.671
[xpst-shell] engine port: 53117
[xpst-shell] ENGINE_HEALTH_WAIT_SECS=1.841
[xpst-shell] BOOT_TO_READY_SECS=3.536
[xpst-shell] WEBVIEW_URL=http://127.0.0.1:53117/
"""


def test_parse_markers_reads_prefixed_shell_output():
    markers = mb.parse_markers(SHELL_LOG)
    assert markers == {
        "BOOT_TO_VISIBLE_SECS": 1.671,
        "ENGINE_HEALTH_WAIT_SECS": 1.841,
        "BOOT_TO_READY_SECS": 3.536,
    }


def test_parse_markers_ignores_absent_markers():
    assert mb.parse_markers("[xpst-shell] nothing to see") == {}


def test_parse_markers_takes_the_first_occurrence():
    text = "BOOT_TO_READY_SECS=2.000\nBOOT_TO_READY_SECS=9.999\n"
    assert mb.parse_markers(text)["BOOT_TO_READY_SECS"] == 2.0


def test_early_exit_hint_names_the_single_instance_case():
    hint = mb.early_exit_hint("[xpst-shell] SINGLE_INSTANCE_RACE_LOSER exiting\n")
    assert "another xPST instance" in hint


def test_early_exit_hint_names_an_unhealthy_engine():
    hint = mb.early_exit_hint("FATAL: engine did not become healthy within timeout\n")
    assert "never answered /health" in hint


def test_early_exit_hint_is_empty_for_unknown_exits():
    assert mb.early_exit_hint("") == ""


# --------------------------------------------------------------------------
# statistics + budget verdict
# --------------------------------------------------------------------------
def _samples(values: list[float]) -> list[dict]:
    return [{"markers": {"BOOT_TO_READY_SECS": v}} for v in values]


def test_summarize_reports_the_numbers_the_budget_gates_on():
    stats = mb.summarize(_samples([2.0, 3.0, 4.0, 10.0]), "BOOT_TO_READY_SECS")
    assert stats["n"] == 4
    assert stats["min"] == 2.0
    assert stats["median"] == 3.5
    assert stats["p95"] == 10.0
    assert stats["max"] == 10.0
    assert stats["first_run"] == 2.0


def test_budget_passes_when_every_limit_is_respected():
    stats = mb.summarize(_samples([2.0, 2.4, 2.6]), "BOOT_TO_READY_SECS")
    budget = {"median_max": 3.5, "p95_max": 4.5, "max_max": 6.0, "first_run_max": 5.0}
    assert mb.check_budget(stats, budget, "host", "BOOT_TO_READY_SECS") == []


def test_budget_fails_and_names_the_measured_number():
    stats = mb.summarize(_samples([2.0, 2.4, 7.5]), "BOOT_TO_READY_SECS")
    budget = {"median_max": 3.5, "p95_max": 4.5, "max_max": 6.0, "first_run_max": 5.0}
    violations = mb.check_budget(stats, budget, "host", "BOOT_TO_READY_SECS")
    assert any("max 7.500s > 6.000s" in v for v in violations)
    assert any("p95 7.500s > 4.500s" in v for v in violations)
    # A healthy median must not mask a bad tail, and vice versa.
    assert not any(v.startswith("BOOT_TO_READY_SECS median") for v in violations)


def test_budget_flags_a_healthy_tail_with_a_bad_median():
    stats = mb.summarize(_samples([4.0, 4.1, 4.2]), "BOOT_TO_READY_SECS")
    budget = {"median_max": 3.5, "p95_max": 4.5, "max_max": 6.0, "first_run_max": 5.0}
    violations = mb.check_budget(stats, budget, "host", "BOOT_TO_READY_SECS")
    assert any("median 4.100s > 3.500s" in v for v in violations)


def test_budget_ignores_limits_that_are_not_committed():
    stats = mb.summarize(_samples([1.0, 2.0]), "BOOT_TO_READY_SECS")
    assert mb.check_budget(stats, {"median_max": 3.5}, "host", "BOOT_TO_READY_SECS") == []


# --------------------------------------------------------------------------
# first-boot outlier: the one breach a re-measurement may absolve
# --------------------------------------------------------------------------
def _host_budget() -> dict:
    return json.loads(BUDGET.read_text())["budgets"]["github-macos-arm64"]


def test_first_run_outlier_is_the_measured_release_lane_failure():
    # The exact set that failed run 43765015694: median inside budget, tail
    # inside budget, first boot outside both p95_max and first_run_max.
    samples = _samples([19.192, 9.313, 9.122])
    assert mb.check_budget(mb.summarize(samples, METRIC), _host_budget(), "h", METRIC)
    assert mb.first_run_outlier_only(samples, _host_budget(), METRIC) is True


def test_first_run_outlier_rejects_a_slow_tail_boot():
    # Run 2 over budget: a warmish boot that slow is not a cold-start artefact.
    samples = _samples([9.0, 17.0, 9.2])
    assert mb.first_run_outlier_only(samples, _host_budget(), METRIC) is False


def test_first_run_outlier_rejects_a_slow_median():
    samples = _samples([13.0, 13.5, 14.0])
    assert mb.first_run_outlier_only(samples, _host_budget(), METRIC) is False


def test_first_run_outlier_requires_a_gated_first_boot():
    samples = _samples([19.192, 9.313, 9.122])
    budget = {k: v for k, v in _host_budget().items() if k != "first_run_max"}
    assert mb.first_run_outlier_only(samples, budget, METRIC) is False


def test_first_run_outlier_needs_a_second_sample_to_compare_against():
    assert mb.first_run_outlier_only(_samples([19.192]), _host_budget(), METRIC) is False


def test_first_run_outlier_is_false_when_the_first_boot_is_fine():
    assert mb.first_run_outlier_only(_samples([3.0, 3.1, 3.2]), _host_budget(), METRIC) is False


@pytest.mark.parametrize("first_boot", [16.001, 16.5, 17.0, 18.0, 18.01, 19.192])
def test_first_run_outlier_covers_the_p95_band_under_first_run_allowance(first_boot: float):
    # The arm64 budget allows p95 16 s, max 20 s and a separate 18 s first-run
    # allowance. At n=3 the p95 IS the worst boot, so a cold first boot between
    # 16 s and 18 s breaches p95_max while sitting inside its own allowance.
    # That band is the same flake as the 19.192 s incident and must be treated
    # identically: every later boot is at 9 s, so the breach is one sample.
    samples = _samples([first_boot, 9.0, 9.0])
    violations = mb.check_budget(mb.summarize(samples, METRIC), _host_budget(), "h", METRIC)
    assert violations, "the band must actually breach a committed limit"
    assert mb.first_run_outlier_only(samples, _host_budget(), METRIC) is True


def test_first_run_outlier_is_false_at_the_p95_limit_itself():
    # 16.0 s is inside p95_max, so nothing is breached and there is nothing to
    # absolve: the predicate must stay a statement about breaches.
    samples = _samples([16.0, 9.0, 9.0])
    assert mb.check_budget(mb.summarize(samples, METRIC), _host_budget(), "h", METRIC) == []
    assert mb.first_run_outlier_only(samples, _host_budget(), METRIC) is False


def test_first_run_outlier_rejects_a_slow_tail_boot_even_in_the_band():
    # A first boot in the p95 band does not launder a later 17 s boot.
    samples = _samples([16.5, 17.0, 9.0])
    assert mb.first_run_outlier_only(samples, _host_budget(), METRIC) is False


def _run_main_with(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sequences: list[list[float]],
    *extra: str,
    github_actions: bool = False,
) -> tuple[int, dict, str]:
    """Drive main() with synthetic cold boots. Returns (rc, json record, stdout)."""
    exe = tmp_path / "xPST"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    pending = list(sequences)

    def fake_measure_once(binary, *, timeout, purge, index, scope):
        return {
            "markers": {
                "BOOT_TO_READY_SECS": pending[0][index - 1],
                "BOOT_TO_VISIBLE_SECS": 1.0,
                "ENGINE_HEALTH_WAIT_SECS": 1.0,
            },
            "cache_purge": True,
        }

    def fake_collect(*args, **kwargs):
        values = pending.pop(0)
        return [
            {
                "markers": {
                    "BOOT_TO_READY_SECS": v,
                    "BOOT_TO_VISIBLE_SECS": 1.0,
                    "ENGINE_HEALTH_WAIT_SECS": 1.0,
                },
                "cache_purge": True,
            }
            for v in values
        ], None

    monkeypatch.setattr(mb, "collect_samples", fake_collect)
    monkeypatch.setattr(mb, "measure_once", fake_measure_once)
    monkeypatch.setattr(mb.time, "sleep", lambda _seconds: None)
    # The harness prints a GitHub annotation when GITHUB_ACTIONS is set and plain
    # text otherwise; pin the mode explicitly so an assertion does not change
    # meaning depending on where the suite runs (this suite runs on Actions too).
    if github_actions:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
    else:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    out = tmp_path / "boot-samples.json"
    rc = mb.main(
        [
            "--app",
            str(exe),
            "--host-class",
            "github-macos-arm64",
            "--runs",
            "3",
            "--json-out",
            str(out),
            *extra,
        ]
    )
    return rc, json.loads(out.read_text()), ""


def test_retry_absolves_a_first_boot_outlier_and_keeps_both_sets(monkeypatch, tmp_path, capsys):
    rc, record, _ = _run_main_with(
        monkeypatch,
        tmp_path,
        [[19.192, 9.313, 9.122], [9.8, 9.4, 9.2]],
        "--retry-first-run-outlier",
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert len(record["attempts"]) == 2
    assert record["attempts"][0]["discarded_as_first_run_outlier"] is True
    assert record["attempts"][1]["discarded_as_first_run_outlier"] is False
    # The discarded set is evidence, not a deletion.
    assert [s["markers"]["BOOT_TO_READY_SECS"] for s in record["attempts"][0]["samples"]] == [
        19.192,
        9.313,
        9.122,
    ]
    assert record["stats"]["samples"] == [9.8, 9.4, 9.2]
    assert record["decided_by_attempt"] == 2
    assert "only the first boot" in out and "re-measuring once" in out
    assert "attempt 2/2" in out
    assert "PASS" in out


def test_retry_absolves_a_first_boot_inside_the_p95_band(monkeypatch, tmp_path, capsys):
    # 16.5 s breaches p95_max but sits inside the 18 s first-run allowance —
    # the band the narrow predicate used to miss. Everything after the first
    # boot is at ~9 s, so this is one cold sample deciding a release.
    rc, record, _ = _run_main_with(
        monkeypatch,
        tmp_path,
        [[16.5, 9.0, 9.1], [9.3, 9.0, 9.2]],
        "--retry-first-run-outlier",
    )
    out = capsys.readouterr().out
    violations = " ".join(record["attempts"][0]["violations"])
    assert rc == 0
    assert "p95" in violations and "first_run" not in violations
    assert len(record["attempts"]) == 2
    assert record["attempts"][0]["discarded_as_first_run_outlier"] is True
    assert record["decided_by_attempt"] == 2
    assert "p95" in out and "re-measuring once" in out


def test_retry_fails_when_a_first_boot_in_the_p95_band_reproduces(monkeypatch, tmp_path, capsys):
    rc, record, _ = _run_main_with(
        monkeypatch,
        tmp_path,
        [[16.5, 9.0, 9.1], [17.2, 9.0, 9.0]],
        "--retry-first-run-outlier",
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert len(record["attempts"]) == 2
    assert "reproduced on attempt 2" in out


def test_retry_fails_when_the_first_boot_outlier_reproduces(monkeypatch, tmp_path, capsys):
    rc, record, _ = _run_main_with(
        monkeypatch,
        tmp_path,
        [[19.192, 9.313, 9.122], [19.5, 9.3, 9.1]],
        "--retry-first-run-outlier",
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert len(record["attempts"]) == 2
    assert "reproduced on attempt 2" in out


def test_retry_does_not_excuse_a_slow_median(monkeypatch, tmp_path, capsys):
    rc, record, _ = _run_main_with(
        monkeypatch,
        tmp_path,
        [[13.0, 13.5, 14.0], [9.0, 9.1, 9.2]],
        "--retry-first-run-outlier",
    )
    assert rc == 1
    # No second attempt was spent: a median breach is a regression signal.
    assert len(record["attempts"]) == 1
    assert "median" in " ".join(record["attempts"][0]["violations"])


def test_retry_warning_is_a_github_annotation_when_running_on_actions(
    monkeypatch, tmp_path, capsys
):
    rc, record, _ = _run_main_with(
        monkeypatch,
        tmp_path,
        [[19.192, 9.313, 9.122], [9.8, 9.4, 9.2]],
        "--retry-first-run-outlier",
        github_actions=True,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "::warning::" in out
    assert len(record["attempts"]) == 2


def test_without_the_flag_a_first_boot_outlier_still_fails(monkeypatch, tmp_path):
    rc, record, _ = _run_main_with(monkeypatch, tmp_path, [[19.192, 9.313, 9.122]])
    assert rc == 1
    assert len(record["attempts"]) == 1


# --------------------------------------------------------------------------
# bundle resolution
# --------------------------------------------------------------------------
def test_resolve_binary_rejects_a_missing_bundle(tmp_path: Path):
    with pytest.raises(mb.HarnessError):
        mb.resolve_binary(tmp_path / "nope.app")


def test_resolve_binary_accepts_a_bare_executable(tmp_path: Path):
    exe = tmp_path / "xPST"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert mb.resolve_binary(exe) == exe


def test_resolve_binary_finds_the_executable_inside_a_bundle(tmp_path: Path):
    macos = tmp_path / "xPST.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    exe = macos / "xPST"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert mb.resolve_binary(tmp_path / "xPST.app") == exe


def test_resolve_binary_returns_an_absolute_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # The harness launches the app with a temporary cwd, so a relative --app
    # path (what the CI step passes, via `find`) must be resolved up front.
    macos = tmp_path / "xPST.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    exe = macos / "xPST"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    resolved = mb.resolve_binary(Path("xPST.app"))
    assert resolved.is_absolute()
    assert resolved == exe


def test_resolve_binary_refuses_a_bundle_without_an_executable(tmp_path: Path):
    (tmp_path / "xPST.app" / "Contents" / "MacOS").mkdir(parents=True)
    with pytest.raises(mb.HarnessError):
        mb.resolve_binary(tmp_path / "xPST.app")


def test_measure_once_reports_an_unlaunchable_binary_as_a_harness_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Not a budget verdict, and not a traceback: a bad bundle is a harness error.
    # Stub the residency probe so the test does not depend on whether the
    # developer happens to have xPST open.
    monkeypatch.setattr(mb, "engine_pids", lambda scope=None: [])
    with pytest.raises(mb.HarnessError, match="could not launch"):
        mb.measure_once(tmp_path, timeout=5, purge=False, index=1, scope=str(tmp_path))


def test_host_class_is_derived_from_the_ci_runner_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_OS", "macOS")
    monkeypatch.setenv("RUNNER_ARCH", "ARM64")
    assert mb.detect_host_class() == "github-macos-arm64"


def test_host_class_is_never_empty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert mb.detect_host_class()


# --------------------------------------------------------------------------
# budget file contract
# --------------------------------------------------------------------------
def test_budget_file_is_loadable_and_gates_the_shell_marker():
    doc = json.loads(BUDGET.read_text())
    assert doc["schema"] == 1
    assert doc["metric"] == "BOOT_TO_READY_SECS"
    assert doc["metric"] in mb.MARKER_NAMES
    assert doc["budgets"], "a budget file with no host class enforces nothing"


def test_every_committed_budget_carries_both_numbers_and_its_evidence():
    doc = json.loads(BUDGET.read_text())
    for host_class, entry in doc["budgets"].items():
        assert {"median_max", "p95_max", "max_max", "first_run_max"} <= set(entry), host_class
        # A budget with no measured baseline is a guess; refuse to ship one.
        baseline = entry["baseline"]
        assert baseline["median"] > 0, host_class
        assert entry["median_max"] > baseline["max"], host_class
        assert entry["evidence"], host_class


def test_local_budget_matches_the_recorded_baseline_run():
    doc = json.loads(BUDGET.read_text())
    entry = doc["budgets"]["mac-mini-m4-16gb-local"]
    assert entry["baseline"]["n"] == entry["baseline_runs"]
    # The budget must sit above the worst measured sample, otherwise the gate
    # fails on its own recorded baseline.
    assert entry["median_max"] > entry["baseline"]["max"]
    assert entry["p95_max"] > entry["baseline"]["p95"]
    # A budget without an evidence pointer is an opinion, not a measurement.
    assert "boot-baseline-mac-mini-m4-2026-09-15.json" in entry["evidence"]


def test_release_lane_measures_cold_boot_and_no_longer_sleeps_15s():
    text = RELEASE_LANE.read_text()
    assert "scripts/measure_boot.py" in text
    # The old step backgrounded the app and slept; both are gone.
    assert "APP_PID=$!" not in text
    assert "\n          sleep 15\n" not in text


def test_boot_budget_lane_gates_pull_requests_that_move_startup():
    text = BOOT_LANE.read_text()
    assert "scripts/measure_boot.py" in text
    assert "pull_request" in text
    for path in ("src-tauri/**", "ui/**", "perf/boot-budget.json"):
        assert path in text, path


def test_boot_budget_lane_uses_the_same_app_binary_the_release_lane_builds():
    # A gate that measured a different artifact than the one shipped would be
    # worse than no gate.
    for lane in (RELEASE_LANE, BOOT_LANE):
        text = lane.read_text()
        assert "xPST.app" in text
        assert "xpst-engine" in text


def test_both_boot_lanes_retry_a_lone_first_boot_outlier():
    # The observed release-lane failure was a single cold first boot
    # ([19.192, 9.313, 9.122]); both lanes must opt into the re-measurement
    # rule, or the flake just moves to whichever lane forgot it.
    for lane in (RELEASE_LANE, BOOT_LANE):
        assert "--retry-first-run-outlier" in lane.read_text(), lane.name


def test_release_lane_retries_packaging_but_not_compilation():
    text = RELEASE_LANE.read_text()
    assert "clear_packaging_state" in text
    assert "failed to run custom build command" in text
    # The retry loop must refuse to spend attempts on a compiler error.
    assert "a compile error is not retried" in text
    # ...and it must still be the packaging step of the real build.
    assert "cargo tauri build" in text
