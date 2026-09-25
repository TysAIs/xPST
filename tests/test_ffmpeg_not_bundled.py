"""The desktop bundle must not ship ffmpeg/ffprobe (87MB of a 192MB app).

The size win only exists while every one of these stays true:

  * ``tauri.conf.json`` does not list a ``binaries/ffmpeg`` resource;
  * the Tauri shell does not point the engine at a bundled ffmpeg/ffprobe;
  * the bundle-input script does not fetch ffmpeg (``scripts/fetch-media-binaries.sh``);
  * the release lane keeps an explicit app-size budget and fails over it, and
    the .app is checked for a reappearing ffmpeg binary.

A regression in any one of them silently puts the app back over its budget, so
each is asserted here (cheap, offline) rather than discovered by a user
downloading a 192MB app.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TAURI_CONF = REPO_ROOT / "src-tauri" / "tauri.conf.json"
LIB_RS = REPO_ROOT / "src-tauri" / "src" / "lib.rs"
FETCH_SCRIPT = REPO_ROOT / "scripts" / "fetch-media-binaries.sh"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tauri-release.yml"
BOOT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "boot-budget.yml"


def test_tauri_config_does_not_bundle_ffmpeg() -> None:
    config = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    resources = config["bundle"]["resources"]

    assert "binaries/ffmpeg" not in resources
    assert not any("ffmpeg" in key or "ffmpeg" in value for key, value in resources.items())
    # The engine and the web UI must still ship — a bundle that lost those
    # would also be small, and would not be an app.
    assert resources.get("binaries/engine") == "binaries/engine"
    assert resources.get("../ui/dist") == "ui"


def test_shell_does_not_point_the_engine_at_a_bundled_ffmpeg() -> None:
    rust = LIB_RS.read_text(encoding="utf-8")

    assert 'resource_dir.join("binaries/ffmpeg")' not in rust
    assert 'env("XPST_FFMPEG_PATH"' not in rust
    assert 'env("XPST_FFPROBE_PATH"' not in rust
    # yt-dlp is the one media helper the bundle still carries.
    assert 'env("XPST_YTDLP_PATH"' in rust


def test_fetch_script_only_fetches_ytdlp() -> None:
    script = FETCH_SCRIPT.read_text(encoding="utf-8")

    assert "binaries/ffmpeg" not in script
    assert "evermeet" not in script and "osxexperts" not in script
    # The script's download urls now live in the pinned lock it reads, and every
    # locked row must be a yt-dlp release asset (no artifact the lane dropped).
    assert "media-binaries.lock" in script, "the script no longer reads the pinned lock"
    lock = (REPO_ROOT / "scripts" / "media-binaries.lock").read_text(encoding="utf-8")
    rows = [line for line in lock.splitlines() if line.strip() and not line.strip().startswith("#")]
    assert rows, "the lock file has no pinned rows"
    assert all("yt-dlp/releases/download/" in row for row in rows), "a non-yt-dlp artifact is pinned again"


def test_release_lane_asserts_the_app_size_budget() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "APP_BUDGET_MB=" in workflow, "the app-size budget disappeared"
    assert "INSTALLER_BUDGET_MB=" in workflow, "the installer-size budget disappeared"

    budgets = dict(re.findall(r"(\w+_BUDGET_MB)=(\d+)", workflow))
    assert int(budgets["APP_BUDGET_MB"]) <= 140, "the app budget must reflect the unbundled size (ffmpeg-scale regressions still fail hard)"
    assert int(budgets["INSTALLER_BUDGET_MB"]) <= 160

    # Over-budget must fail the lane, and the .app must be checked for a
    # reappearing ffmpeg binary (a silent re-bundle would otherwise pass the
    # size gate on a small build).
    assert "over the ${APP_BUDGET_MB}MB budget" in workflow
    assert "must be resolved at runtime" in workflow
    assert "exit 1" in workflow


def test_lanes_no_longer_assert_bundled_ffmpeg() -> None:
    """No build lane may assert a bundled ffmpeg — that design is gone.

    boot-budget.yml shipped with a stale `test -x src-tauri/binaries/ffmpeg/
    ffmpeg` copied from the pre-unbundling release lane, which failed the lane
    on every PR that touched its paths. Both lanes are checked here so a stale
    copy cannot come back through the other one.
    """
    for lane in (RELEASE_WORKFLOW, BOOT_WORKFLOW):
        workflow = lane.read_text(encoding="utf-8")

        assert "test -x src-tauri/binaries/ffmpeg/ffmpeg" not in workflow, lane
        assert "test -f src-tauri/binaries/ffmpeg/ffmpeg.exe" not in workflow, lane
        # ...and each refuses to build if one shows up in the bundle inputs.
        assert "src-tauri/binaries/ffmpeg/ffmpeg" in workflow, lane


def test_engine_entry_auto_fetches_media_binaries() -> None:
    entry = (REPO_ROOT / "scripts" / "engine_entry.py").read_text(encoding="utf-8")

    assert "_start_media_bootstrap()" in entry
    assert "ensure_media_binaries" in entry
    # Opt-out honoured, and the engine must never die because of it.
    assert "XPST_MEDIA_AUTO_FETCH" in entry


def test_runtime_fetch_module_is_not_excluded_from_the_engine_bundle() -> None:
    """The engine bundle must include the module that replaces the download."""
    assert (REPO_ROOT / "src" / "xpst" / "media" / "binaries.py").is_file()
    spec = (REPO_ROOT / "build_engine.spec").read_text(encoding="utf-8")
    excluded = spec.split("EXCLUDED_MODULES")[1].split("]")[0]
    assert "xpst.media" not in excluded
    assert "urllib" not in excluded
