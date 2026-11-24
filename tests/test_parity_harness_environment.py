"""The local-surface harness must not inherit the caller's environment.

``xtask/scripts/capture_local_surface.py`` runs the shipped Python engine and the
Rust binary over the same scenarios and diffs them. Anything it inherits from
the caller is a difference between a green local run and a red CI run, so the
variables that can change a rendering are pinned in ``env_for`` and recorded in
the fixture. These tests hold that contract:

* ``TZ`` is pinned because an entry with no ``timezone`` field renders in the
  machine zone (``ScheduleManager._tz`` is ``local_zone()``, which reads ``TZ``
  first). ``schedule_list_pinned_zone`` / ``schedule_list_machine_zone`` in the
  fixture are the rows that exercise it.
* ``LC_ALL``/``LANG`` are pinned because rich's log stamp is ``[%x %X]``, which
  a locale that Python adopted would change.
* ``PYTHONHASHSEED`` is pinned because a payload that serialises a set would
  otherwise vary between runs.

The last two are belt-and-braces rather than load-bearing today: Python only
adopts the environment locale for ``strftime`` after an explicit
``locale.setlocale``, which the engine never calls, and no scenario serialises a
set. The tests below prove both properties, so the pins cannot quietly become
the only thing holding the fixture together.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "crates/xpst-cli/tests/fixtures/local_surface.json"


def _load_harness():
    spec = importlib.util.spec_from_file_location(
        "capture_local_surface", REPO / "xtask/scripts/capture_local_surface.py"
    )
    assert spec is not None and spec.loader is not None, "the harness script is loadable"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HARNESS = _load_harness()


def _locale_available(name: str) -> bool:
    try:
        listing = subprocess.run(
            ["locale", "-a"], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    wanted = name.lower().replace("-", "")
    return any(
        line.strip().lower().replace("-", "") == wanted for line in listing.splitlines()
    )


def _run_cli(argv: list[str], profile: Path, seed: str) -> str:
    env = {
        **os.environ,
        "XPST_CONFIG_DIR": str(profile),
        "TZ": "UTC",
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONHASHSEED": seed,
        "NO_COLOR": "1",
        "TERM": "dumb",
        "PYTHONPATH": str(REPO / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "xpst", *argv],
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    assert proc.returncode == 0, f"{argv} exited {proc.returncode}: {proc.stderr}"
    return proc.stdout


# ── the pins ─────────────────────────────────────────────────────────────


def test_env_for_pins_the_rendering_variables(tmp_path):
    env = HARNESS.env_for({}, tmp_path)
    assert env["TZ"] == "UTC"
    assert env["LC_ALL"] == "C"
    assert env["LANG"] == "C"
    assert env["PYTHONHASHSEED"] == "0"


def test_a_scenario_env_overrides_a_pin(tmp_path):
    # The override is what makes `schedule_list_machine_zone` a proof that both
    # engines read TZ rather than hardcoding the pinned value.
    env = HARNESS.env_for({"env": {"TZ": "Pacific/Kiritimati"}}, tmp_path)
    assert env["TZ"] == "Pacific/Kiritimati"
    assert env["LC_ALL"] == "C", "an unrelated pin must survive the override"


def test_the_fixture_records_the_pins():
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert document["environment"] == HARNESS.PINNED_ENV, (
        "the fixture was captured under a different environment than the harness "
        "pins now; re-capture it"
    )


# ── locale: proven, not assumed ──────────────────────────────────────────


def test_the_engine_never_adopts_the_environment_locale():
    """`strftime` stays C-rendered only while nothing calls `setlocale`."""
    offenders = [
        str(path.relative_to(REPO))
        for path in (REPO / "src/xpst").rglob("*.py")
        if "setlocale" in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert not offenders, (
        "the engine now calls setlocale, so rich's `[%x %X]` log stamp follows "
        f"the environment locale: {offenders}"
    )


def test_the_log_stamp_is_c_rendered_under_a_non_c_locale():
    """The mechanism behind the previous test, measured rather than assumed."""
    if not _locale_available("de_DE.UTF-8"):
        pytest.skip("de_DE.UTF-8 is not installed on this machine")
    env = {**os.environ, "LC_ALL": "de_DE.UTF-8", "LANG": "de_DE.UTF-8"}
    out = subprocess.run(
        [sys.executable, "-c", "import time; print(time.strftime('%x %X'))"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=True,
    ).stdout.strip()
    assert re.fullmatch(r"\d{2}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}", out), (
        f"strftime rendered {out!r} under de_DE.UTF-8; the C-locale log stamp is "
        "no longer guaranteed"
    )


# ── hash seed: proven, not assumed ───────────────────────────────────────


@pytest.mark.parametrize(
    "argv",
    [
        ["status", "--json"],
        ["failures", "list", "--json"],
        ["schedule", "list", "--json"],
        ["capabilities", "--json"],
    ],
)
def test_payloads_do_not_depend_on_the_hash_seed(tmp_path, argv):
    """No scenario serialises a set, so the seed cannot change a payload."""
    outputs = {}
    for seed in ("1", "2"):
        profile = tmp_path / f"seed-{seed}"
        profile.mkdir()
        (profile / "state.json").write_text(
            json.dumps(HARNESS.RICH_STATE), encoding="utf-8"
        )
        (profile / "schedule.json").write_text(
            json.dumps(HARNESS.SCHEDULE_ENTRIES), encoding="utf-8"
        )
        outputs[seed] = _run_cli(argv, profile, seed)
    assert outputs["1"] == outputs["2"], (
        f"`{' '.join(argv)}` depends on PYTHONHASHSEED: a set is being serialised "
        "in hash order"
    )
