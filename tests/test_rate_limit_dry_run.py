"""The counted dry run that gates un-halting: it must be repeatable and honest.

The un-halt rule is "the rate-limit fixes are verified by a counted dry run".
These tests pin the properties that make that sentence mean something:

* the counts are identical on a second run, including a second run that reuses
  the same scratch root (the persisted cooldown file is the trap: it survives a
  process exit, so a naive second run measures the first run's leftovers);
* no real post can happen — the matrix runs under a socket guard;
* the gate is not vacuous: an observation that drops the provider's
  ``Retry-After`` fails it.
"""

from __future__ import annotations

import json

import pytest

from xpst.rate_limit_dry_run import (
    MAX_ATTEMPTS_PER_POST,
    Observation,
    check_invariants,
    main,
    run_dry_run,
)


@pytest.fixture(scope="module")
def summary(tmp_path_factory):
    """One two-run dry run, shared by the assertions below (it is the artifact)."""
    root = tmp_path_factory.mktemp("dry-run")
    return run_dry_run(runs=2, work_root=root, keep=True)


def test_two_runs_produce_the_same_counts(summary):
    repro = summary["reproducibility"]
    assert repro["runs"] == 2
    assert repro["identical"] is True, repro["counts_sha256"]
    assert len(set(repro["counts_sha256"])) == 1
    assert summary["invariants"]["violations"] == []


def test_the_matrix_covers_every_destination_and_throttle_shape(summary):
    totals = summary["totals"]
    assert totals["scenarios"] >= 40
    assert set(totals["platforms_attempted"]) == {
        "facebook",
        "instagram",
        "tiktok",
        "x",
        "youtube",
    }
    scenarios = {item["scenario"] for item in summary["scenarios"]}
    for transport_id in ("x", "instagram", "instagram_session", "facebook", "youtube", "tiktok"):
        for fault in ("clean", "meta-code-4", "meta-code-4-retry-after", "403-rate-limit", "500"):
            assert f"{transport_id}/{fault}" in scenarios


def test_a_throttle_never_counts_as_a_post_and_records_the_retry_after(summary):
    throttled = [
        item
        for item in summary["scenarios"]
        if item["fault"].startswith(("meta-code-4", "403-rate-limit"))
        and not item["scenario"].startswith(("restart/", "hard-stop/"))
    ]
    assert throttled
    for item in throttled:
        assert item["published"] is False, item["scenario"]
        assert item["recorded_posted"] is False, item["scenario"]
        assert item["cooldown_pause_s"] > 0, item["scenario"]
        assert item["attempts"] <= MAX_ATTEMPTS_PER_POST, item["scenario"]
    declared = [
        item
        for item in throttled
        if item["fault"] == "meta-code-4-retry-after"
    ]
    for item in declared:
        assert item["declared_retry_after_s"] == 900
        assert item["retry_after_honoured"] is True, item["scenario"]
        assert item["cooldown_pause_s"] == pytest.approx(900, abs=1)


def test_nothing_is_lost_and_nothing_reaches_the_network(summary):
    totals = summary["totals"]
    assert totals["posts_lost"] == 0
    assert totals["posts_that_would_have_gone_out"] > 0
    assert summary["network"]["real_connections_attempted"] == 0


def test_every_scenario_started_from_a_reset_cooldown_state(summary):
    reset = summary["reset"]
    assert reset["scenarios_with_leftover_cooldown_file"] == 0
    # ...while the restart proofs deliberately kept the persisted state.
    assert reset["restart_proofs_with_persisted_state"] >= 5


def test_a_second_invocation_in_the_same_scratch_root_is_still_clean(tmp_path):
    """The task's known trap: persisted cooldown state must not leak into run 2."""
    first = run_dry_run(runs=1, work_root=tmp_path / "scratch")
    second = run_dry_run(runs=1, work_root=tmp_path / "scratch")

    assert first["reset"]["scenarios_with_leftover_cooldown_file"] == 0
    assert second["reset"]["scenarios_with_leftover_cooldown_file"] == 0
    assert (
        first["reproducibility"]["counts_sha256"]
        == second["reproducibility"]["counts_sha256"]
    )


def test_the_gate_fails_when_a_provider_window_is_dropped():
    """Not a vacuous check: 60s instead of the declared 900s is a violation."""
    dropped = Observation(
        scenario="x/meta-code-4-retry-after",
        platform="x",
        fault="meta-code-4-retry-after",
        attempts=1,
        rate_limit_responses=1,
        cooldown_pause_s=60.0,
        cooldown_strikes=1,
        declared_retry_after_s=900.0,
        retry_after_honoured=False,
    )
    violations = check_invariants([dropped])
    assert any("Retry-After" in v for v in violations), violations


def test_the_command_writes_the_machine_readable_summary(tmp_path, capsys):
    out = tmp_path / "XPST-DRYRUN.json"
    code = main(["--json", str(out), "--runs", "1", "--quiet"])

    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema"] == "xpst.rate-limit-dry-run/1"
    assert payload["reproducibility"]["identical"] is True
    assert payload["invariants"]["violations"] == []
    for key in (
        "platforms_attempted",
        "posts_that_would_have_gone_out",
        "rate_limit_responses",
        "dropped_requests",
    ):
        assert key in payload["totals"], key
    assert payload["cooldown_decisions"]
    # stdout carries the same document, so a pipeline can pipe it straight to jq.
    assert json.loads(capsys.readouterr().out)["totals"] == payload["totals"]
