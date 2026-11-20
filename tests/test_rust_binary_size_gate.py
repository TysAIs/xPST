"""Contract for the Rust release-binary size gate and the cold-start harness.

The gate exists because D-4 states "lightweight is measured, not asserted", and a
gate that cannot fail is decoration. These tests therefore drive the real scripts
as subprocesses against synthetic binaries, and assert both directions:

  * a binary over its budget exits 1 and the message names the bytes and the
    budget (so a red CI log says what grew);
  * a binary inside its budget exits 0;
  * an unmeasured binary is *not* a pass — exit 2, because a binary nobody
    budgeted cannot be claimed to be within one;
  * the committed record (``perf/rust-binary-budget.json``) is self-consistent:
    every budget is above the number measured in the same record, and the D-4
    target is the 25 MiB DIRECTION.md states;
  * the cold-start harness fails on a median over its budget and records the
    samples it actually took.

No network, no Rust build: the binaries are sparse files of a chosen size and the
"slow binary" is the Python interpreter sleeping.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "scripts" / "check_rust_binary_size.py"
BOOT = REPO_ROOT / "scripts" / "measure_rust_boot.py"
BUDGET_RECORD = REPO_ROOT / "perf" / "rust-binary-budget.json"

MIB = 1024 * 1024


def _fake_binary(path: Path, size: int) -> Path:
    """A file of exactly ``size`` bytes that needs no linker or toolchain."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    return path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args], capture_output=True, text=True, check=False
    )


# --------------------------------------------------------------------------- #
# The size gate
# --------------------------------------------------------------------------- #


def test_binary_over_budget_fails_and_names_the_numbers(tmp_path: Path) -> None:
    binary = _fake_binary(tmp_path / "xpst", 3 * MIB)
    result = _run(
        str(GATE), "--binary", str(binary), "--max-bytes", str(2 * MIB)
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAIL" in result.stderr
    assert str(3 * MIB) in result.stderr  # the measured size
    assert str(2 * MIB) in result.stderr  # the budget it broke
    assert "1048576 bytes" in result.stderr or "over its" in result.stderr


def test_binary_within_budget_passes(tmp_path: Path) -> None:
    binary = _fake_binary(tmp_path / "xpst", 1024)
    result = _run(
        str(GATE), "--binary", str(binary), "--max-bytes", str(2 * MIB)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "size budget: PASS" in result.stdout
    assert "1024 bytes" in result.stdout


def test_unmeasured_binary_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    binary = _fake_binary(tmp_path / "xpst-not-in-the-record", 1024)
    result = _run(str(GATE), "--binary", str(binary))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "no budget" in result.stderr


def test_missing_binary_is_a_measurement_failure(tmp_path: Path) -> None:
    result = _run(str(GATE), "--binary", str(tmp_path / "absent"))
    assert result.returncode == 2
    assert "no binary at" in result.stderr


def test_missing_budget_file_refuses_to_guess(tmp_path: Path) -> None:
    binary = _fake_binary(tmp_path / "xpst", 1024)
    result = _run(
        str(GATE),
        "--binary",
        str(binary),
        "--budget-file",
        str(tmp_path / "absent.json"),
    )
    assert result.returncode == 2
    assert "budget file not found" in result.stderr


def test_report_only_keeps_the_measurement_but_does_not_fail(tmp_path: Path) -> None:
    binary = _fake_binary(tmp_path / "xpst", 3 * MIB)
    report = tmp_path / "size.json"
    result = _run(
        str(GATE),
        "--binary",
        str(binary),
        "--max-bytes",
        str(2 * MIB),
        "--report-only",
        "--budget-json",
        str(report),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "report-only" in result.stderr
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["binaries"][0]["bytes"] == 3 * MIB
    assert payload["failures"], "a report-only run still records why it would have failed"


def test_budget_record_is_above_every_measurement_it_records() -> None:
    record = json.loads(BUDGET_RECORD.read_text(encoding="utf-8"))
    assert record["d4"]["single_binary_max_bytes"] == 25 * MIB
    assert record["budgets"], "the gate has nothing to assert without budgets"
    for name, entry in record["budgets"].items():
        measured = entry["measured"]["bytes"]
        assert entry["max_bytes"] > measured, (
            f"{name}: budget {entry['max_bytes']} is not above the recorded "
            f"measurement {measured}"
        )
        # Headroom is deliberate but bounded: a budget more than 4x the measured
        # number cannot catch a regression, it can only document one.
        assert entry["max_bytes"] <= 4 * measured, (
            f"{name}: budget {entry['max_bytes']} is more than 4x the measured "
            f"{measured} — that is a record, not a gate"
        )
        assert entry["max_bytes"] <= record["d4"]["single_binary_max_bytes"]


def test_budget_record_cannot_claim_more_than_the_d4_target() -> None:
    record = json.loads(BUDGET_RECORD.read_text(encoding="utf-8"))
    for name, entry in record["budgets"].items():
        assert entry["max_bytes"] < record["d4"]["single_binary_max_bytes"], (
            f"{name}: a per-binary budget at or above the D-4 target would let a "
            "binary pass this gate while missing the target it is named after"
        )


# --------------------------------------------------------------------------- #
# The cold-start harness
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("budget_ms,expected", [(1.0, 1), (60000.0, 0)])
def test_cold_start_harness_gates_on_the_median(
    tmp_path: Path, budget_ms: float, expected: int
) -> None:
    """The interpreter start (tens of ms) stands in for a slow binary."""
    report = tmp_path / "boot.json"
    result = _run(
        str(BOOT),
        "--binary",
        sys.executable,
        "--arg=-c",
        "--arg=pass",
        "--runs",
        "3",
        "--no-cache-purge",
        "--max-median-ms",
        str(budget_ms),
        "--json-out",
        str(report),
    )
    assert result.returncode == expected, result.stdout + result.stderr
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["stats"]["n"] == 3
    assert len(payload["samples"]) == 3
    assert payload["stats"]["median"] > 0
    assert "cache_purge_available" in payload
    if expected == 0:
        assert "cold-start budget: PASS" in result.stdout
    else:
        assert "FAIL" in result.stderr
