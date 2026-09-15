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


def test_resolve_binary_refuses_a_bundle_without_an_executable(tmp_path: Path):
    (tmp_path / "xPST.app" / "Contents" / "MacOS").mkdir(parents=True)
    with pytest.raises(mb.HarnessError):
        mb.resolve_binary(tmp_path / "xPST.app")


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
