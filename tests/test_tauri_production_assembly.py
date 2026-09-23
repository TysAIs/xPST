"""Contracts for the canonical Tauri/Svelte/PyInstaller production assembly."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _json(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def _cargo_package_version() -> str:
    text = (ROOT / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
    match = re.search(r"(?ms)^\[package\].*?^version\s*=\s*\"([^\"]+)\"", text)
    assert match, "Cargo package version is missing"
    return match.group(1)


def test_release_surfaces_are_all_110() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    python_version = re.search(r"(?m)^version\s*=\s*\"([^\"]+)\"", pyproject)
    assert python_version, "Python release version is missing"

    versions = {
        "python": python_version.group(1),
        "cargo": _cargo_package_version(),
        "tauri": _json("src-tauri/tauri.conf.json")["version"],
        "ui": _json("ui/package.json")["version"],
        "ui-lock": _json("ui/package-lock.json")["packages"][""]["version"],
    }
    assert versions == {name: "1.1.0" for name in versions}


def test_release_version_verification_path_passes() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/verify_release_version.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1.1.0" in result.stdout
    assert "python-runtime" in result.stdout


def test_tauri_builds_ui_and_maps_packaged_resources() -> None:
    config = _json("src-tauri/tauri.conf.json")
    build = config["build"]
    resources = config["bundle"]["resources"]

    # Tauri executes beforeBuildCommand with src-tauri as its working directory.
    assert build["beforeBuildCommand"] == "cd ../ui && npm ci && npm run build"
    assert build["frontendDist"] == "../ui/dist"
    assert resources["../ui/dist"] == "ui"
    assert resources["binaries/engine"] == "binaries/engine"


def test_tauri_workflow_is_deterministic_and_fails_closed() -> None:
    workflow = (ROOT / ".github" / "workflows" / "tauri-release.yml").read_text(encoding="utf-8")

    assert "npm ci" in workflow
    assert "npm run build" in workflow
    assert "scripts/build-engine.sh" in workflow
    assert "--onefile" not in workflow
    assert "PyInstaller onefile" not in workflow
    assert "if-no-files-found: error" in workflow
    assert "python scripts/verify_release_version.py" in workflow
    assert "cargo install tauri-cli --version 2.11.4 --locked" in workflow
    assert "cargo tauri build" in workflow
    assert "tauri" in workflow


def test_engine_assembly_is_explicitly_onedir() -> None:
    spec = (ROOT / "build_engine.spec").read_text(encoding="utf-8")
    entry = (ROOT / "scripts" / "engine_entry.py").read_text(encoding="utf-8")

    assert "COLLECT(" in spec
    assert "--onefile" not in spec
    assert "onedir" in entry
    assert "one-file" not in entry


def test_rust_uses_packaged_ui_and_fails_closed_before_spawn() -> None:
    rust = (ROOT / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")

    assert 'let ui_dir = resource_dir.join("ui")' in rust
    assert 'let ui_index = ui_dir.join("index.html")' in rust
    assert "if !engine_exe.is_file() || !ui_index.is_file()" in rust
    assert 'command = command.env("XPST_UI_DIST", ui_dir);' in rust


def test_rust_unix_process_hooks_are_platform_guarded() -> None:
    rust = (ROOT / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")

    assert "#[cfg(unix)]\nfn install_signal_handlers" in rust
    assert "#[cfg(not(unix))]\nfn install_signal_handlers" in rust
    assert "#[cfg(unix)]\nfn acquire_single_instance_lock" in rust
    assert "#[cfg(not(unix))]\nfn acquire_single_instance_lock" in rust
    assert "std::os::unix::io::AsRawFd" not in rust


def test_build_script_writes_the_exact_rust_resource_path() -> None:
    script = (ROOT / "scripts" / "build-engine.sh").read_text(encoding="utf-8")

    assert 'OUT_DIR="$REPO_ROOT/src-tauri/binaries"' in script
    assert '"$OUT_DIR/engine"' in script
    assert 'dist/engine/xpst-engine' in script
    assert "onedir" in script


def test_tauri_workflow_passes_no_empty_apple_identity() -> None:
    """An empty APPLE_SIGNING_IDENTITY is not an absent one.

    The repository has no Apple secrets, so the workflow exported
    APPLE_SIGNING_IDENTITY="" and Tauri ran `codesign --sign ""`, failing every
    bundle with "no identity found" after a successful Rust build.
    """
    workflow = (ROOT / ".github" / "workflows" / "tauri-release.yml").read_text(encoding="utf-8")

    assert 'unset "$var"' in workflow
    for variable in ("APPLE_SIGNING_IDENTITY", "APPLE_ID", "APPLE_PASSWORD", "APPLE_TEAM_ID"):
        assert variable in workflow


def test_tauri_workflow_builds_the_installer_without_an_updater_key() -> None:
    """No updater key must not downgrade the bundle set to `app` only.

    The old else-branch built `--bundles app`, so the macOS lane produced no
    .dmg at all and the subsequent upload had nothing to publish.
    """
    workflow = (ROOT / ".github" / "workflows" / "tauri-release.yml").read_text(encoding="utf-8")

    assert "--bundles app" not in workflow
    assert "no TAURI_SIGNING_PRIVATE_KEY" in workflow
    # The configured bundle set is used on both branches.
    assert workflow.count("--bundles ${{ matrix.bundles }}") == 2


def test_tauri_workflow_uploads_from_the_target_specific_bundle_dir() -> None:
    """`--target <triple>` writes target/<triple>/release/bundle.

    The literal `src-tauri/target/release/bundle/...` upload and release paths
    matched nothing for a targeted build, so a successful build still failed the
    upload step with if-no-files-found: error.
    """
    workflow = (ROOT / ".github" / "workflows" / "tauri-release.yml").read_text(encoding="utf-8")

    assert "src-tauri/target/release/bundle" not in workflow
    assert "src-tauri/target/**/bundle/dmg/*.dmg" in workflow
    assert "src-tauri/target/**/bundle/macos/xPST.app" in workflow


def test_tauri_workflow_asserts_an_installer_exists_and_avoids_gnu_timeout() -> None:
    """The lane must fail loudly when it built no installer, and macOS has no GNU timeout."""
    workflow = (ROOT / ".github" / "workflows" / "tauri-release.yml").read_text(encoding="utf-8")

    assert "no .dmg was produced" in workflow
    assert 'timeout 60 "' not in workflow
    assert "if-no-files-found: error" in workflow


def test_tauri_workflow_gates_the_app_and_installer_size_budget() -> None:
    """The size gate measures both the download and the unpacked app.

    Measuring only the installer let 87MB of ffmpeg ride along unnoticed, so the
    unpacked .app now has a hard budget too — and because a bundle that simply
    lost its engine would also be small, the engine sidecar is asserted present
    in the same step.
    """
    workflow = (ROOT / ".github" / "workflows" / "tauri-release.yml").read_text(encoding="utf-8")

    assert "Verify app/installer size budget" in workflow
    assert "du -sm \"$APP_PATH\"" in workflow
    assert 'INSTALLER=$(find src-tauri/target -path "$PATTERN" -print -quit)' in workflow
    assert "APP_BUDGET_MB=130" in workflow
    assert "over the ${APP_BUDGET_MB}MB budget" in workflow
    assert "must be resolved at runtime" in workflow
    assert "engine sidecar missing from the app bundle" in workflow
    assert "::error::installer ${SIZE_MB}MB exceeds the ${INSTALLER_BUDGET_MB}MB budget" in workflow
