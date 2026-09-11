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
