"""Contracts for the canonical Tauri/Svelte/PyInstaller production assembly."""

from __future__ import annotations

import json
import re
import shutil
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


def _workflow_jobs() -> dict:
    import yaml

    return yaml.safe_load((ROOT / ".github" / "workflows" / "tauri-release.yml").read_text(encoding="utf-8"))["jobs"]


def test_tauri_workflow_publishes_one_asserted_release_asset_set() -> None:
    """One job owns the release, and the published asset set is asserted first.

    Three lanes racing to create the same GitHub release killed a tag build with
    `Creating new GitHub release ... 500 / Too many retries` (dryrun-tauri-1), so
    the lanes upload artifacts and a single publish job collects, asserts,
    checksums and publishes them.
    """
    workflow = (ROOT / ".github" / "workflows" / "tauri-release.yml").read_text(encoding="utf-8")
    jobs = _workflow_jobs()

    assert "publish-release" in jobs
    publish = jobs["publish-release"]
    assert publish["needs"] == ["build-tauri"]
    assert publish.get("if") == "startsWith(github.ref, 'refs/tags/')"

    build_lane = workflow.split("publish-release:", 1)[0]
    assert "action-gh-release" not in build_lane, "a build lane still creates the release itself"
    assert "fail_on_unmatched_files: true" in workflow
    assert "would publish no" in workflow, "no platform-coverage assertion before publishing"
    assert "upload-artifact@v4" in build_lane and "download-artifact@v4" in workflow
    assert "SHA256SUMS" in workflow


def test_tauri_workflow_marks_a_documented_dry_run_tag_as_a_draft() -> None:
    workflow = (ROOT / ".github" / "workflows" / "tauri-release.yml").read_text(encoding="utf-8")
    assert "dryrun-tauri-*" in workflow
    assert "draft: ${{ !startsWith(github.ref_name, 'v') }}" in workflow
    # A path filter on the tag trigger would be dead config: GitHub does not
    # evaluate path filters for tag pushes, so the lane must not carry one.
    import yaml

    triggers = yaml.safe_load((ROOT / ".github" / "workflows" / "tauri-release.yml").read_text(encoding="utf-8"))[True]
    assert "paths" not in triggers["push"]
    assert triggers["push"]["tags"] == ["v*.*.*", "dryrun-tauri-*"]


def test_release_asset_assertion_runs_and_gates(tmp_path: Path) -> None:
    """The publish job's assertion step is executed, not just grepped for.

    A tag that resolves no installer for a claimed platform must fail before
    anything is published.
    """
    if sys.platform == "win32":
        import pytest

        pytest.skip("the publish job runs on Linux; the step is a bash script")

    step = None
    for candidate in _workflow_jobs()["publish-release"]["steps"]:
        if candidate.get("id") == "assets":
            step = candidate["run"]
    assert step, "publish-release has no asset-resolution step"

    def run(artifacts: dict[str, str]) -> subprocess.CompletedProcess:
        (tmp_path / "release-artifacts").mkdir(exist_ok=True)
        for name, body in artifacts.items():
            path = tmp_path / "release-artifacts" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        out = tmp_path / "github_output"
        out.write_text("", encoding="utf-8")
        script = tmp_path / "step.sh"
        script.write_text(step, encoding="utf-8")
        return subprocess.run(
            ["bash", str(script)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin", "GITHUB_REF_NAME": "dryrun-tauri-1", "GITHUB_OUTPUT": str(out)},
            timeout=120,
        )

    complete = {
        "mac/dmg/xPST_1.1.0_aarch64.dmg": "dmg",
        "mac/dmg/media-binaries-PROVENANCE-aarch64-apple-darwin.txt": "prov",
        "win/nsis/xPST_1.1.0_x64-setup.exe": "exe",
        "linux/appimage/xPST_1.1.0_amd64.AppImage": "appimage",
    }
    proc = run(complete)
    assert proc.returncode == 0, proc.stderr
    assert "SHA256SUMS" in (tmp_path / "github_output").read_text(encoding="utf-8")
    assert (tmp_path / "release-artifacts" / "SHA256SUMS").is_file()

    shutil.rmtree(tmp_path / "release-artifacts")
    proc = run({k: v for k, v in complete.items() if not k.startswith("linux")})
    assert proc.returncode == 1
    assert "would publish no Linux installer" in proc.stdout


def test_release_asset_assertion_requires_the_provenance_record(tmp_path: Path) -> None:
    if sys.platform == "win32":
        import pytest

        pytest.skip("the publish job runs on Linux; the step is a bash script")

    jobs = _workflow_jobs()
    step = [s for s in jobs["publish-release"]["steps"] if s.get("id") == "assets"][0]["run"]
    (tmp_path / "release-artifacts").mkdir()
    for name in ("xPST_1.1.0_aarch64.dmg", "xPST_1.1.0_x64-setup.exe", "xPST_1.1.0_amd64.AppImage"):
        (tmp_path / "release-artifacts" / name).write_text("x", encoding="utf-8")
    script = tmp_path / "step.sh"
    script.write_text(step, encoding="utf-8")
    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "GITHUB_REF_NAME": "v1.1.1", "GITHUB_OUTPUT": str(out)},
        timeout=120,
    )
    assert proc.returncode == 1
    assert "would publish no media binary provenance record" in proc.stdout
