"""Release artifact helper tests."""

import json
import os
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from scripts.build_package import ROOT as BUILD_ROOT
from scripts.build_package import build_package
from scripts.public_release_check import collect_public_release_evidence
from scripts.release_artifacts import (
    copy_project_documents,
    find_dist_files,
    generate_checksum_files,
    generate_pypi_json,
    generate_release_evidence,
    generate_sbom,
    verify_sdist,
    verify_wheel,
)
from scripts.release_preflight import build_release_preflight
from scripts.release_version_guard import expected_version_for_tag, validate_release_version
from scripts.verify_macos_artifact import verify_macos_artifact
from scripts.verify_windows_exe import verify_windows_exe


def _make_wheel(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xpst/__init__.py", "")
        archive.writestr("xpst-0.1.0.dist-info/METADATA", "Name: xpst\nVersion: 0.1.0\n")


def _make_sdist(path: Path, tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[project]\nname = \"xpst\"\n", encoding="utf-8")
    with tarfile.open(path, "w:gz") as archive:
        archive.add(pyproject, arcname="xpst-0.1.0/pyproject.toml")


def _write_versioned_project(root: Path, version: str) -> None:
    (root / "src" / "xpst").mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        f"[project]\nname = \"xpst\"\nversion = \"{version}\"\n",
        encoding="utf-8",
    )
    (root / "src" / "xpst" / "__init__.py").write_text(
        f"__version__ = \"{version}\"\n",
        encoding="utf-8",
    )


def test_release_version_guard_maps_rc_tag_to_pep440_version():
    assert expected_version_for_tag("v0.1.0-rc2") == "0.1.0rc2"


def test_release_version_guard_rejects_rc_tag_with_final_package_version(tmp_path):
    _write_versioned_project(tmp_path, "0.1.0")

    result = validate_release_version("v0.1.0-rc2", root=tmp_path)

    assert result["ok"] is False
    assert result["expected_version"] == "0.1.0rc2"
    assert "pyproject has 0.1.0" in result["error"]


def test_release_version_guard_accepts_matching_final_and_rc_tags(tmp_path):
    _write_versioned_project(tmp_path, "0.1.0")
    assert validate_release_version("v0.1.0", root=tmp_path)["ok"] is True

    _write_versioned_project(tmp_path, "0.1.0rc2")
    assert validate_release_version("v0.1.0-rc2", root=tmp_path)["ok"] is True


def test_release_workflow_guards_version_and_skips_pypi_for_rc_tags():
    workflow = (BUILD_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "python scripts/release_version_guard.py --tag" in workflow
    assert "github.event_name == 'push' && !contains(github.ref_name, '-rc')" in workflow
    assert "python scripts/public_release_check.py --dist release-artifacts --output-dir release-public --json" in workflow
    assert "if: ${{ !contains(github.ref_name, '-rc') }}" in workflow


def test_find_dist_files_accepts_desktop_only_artifact(tmp_path):
    exe = tmp_path / "xPST.exe"
    exe.write_bytes(b"binary")

    files = find_dist_files(tmp_path)

    assert files["all"] == [exe]
    assert files["desktop"] == [exe]
    assert files["wheels"] == []
    assert files["sdists"] == []


def test_generate_checksums_for_all_artifacts(tmp_path):
    wheel = tmp_path / "xpst-0.1.0-py3-none-any.whl"
    exe = tmp_path / "xPST.exe"
    _make_wheel(wheel)
    exe.write_bytes(b"binary")
    sha256_output = tmp_path / "SHA256SUMS"
    sha512_output = tmp_path / "SHA512SUMS"

    generate_checksum_files(tmp_path, sha256_output, sha512_output)
    sha256_text = sha256_output.read_text(encoding="utf-8")
    sha512_text = sha512_output.read_text(encoding="utf-8")

    assert "xpst-0.1.0-py3-none-any.whl" in sha256_text
    assert "xPST.exe" in sha256_text
    assert "xpst-0.1.0-py3-none-any.whl" in sha512_text
    assert "xPST.exe" in sha512_text


def test_generate_pypi_json_skips_desktop_only_dist(tmp_path):
    exe = tmp_path / "xPST.exe"
    exe.write_bytes(b"binary")
    output = tmp_path / "pypi.json"
    output.write_text("stale", encoding="utf-8")

    generate_pypi_json(tmp_path, output, "0.1.0")

    assert not output.exists()


def test_verify_python_distribution_archives(tmp_path):
    wheel = tmp_path / "xpst-0.1.0-py3-none-any.whl"
    sdist = tmp_path / "xpst-0.1.0.tar.gz"
    _make_wheel(wheel)
    _make_sdist(sdist, tmp_path)

    assert verify_wheel(wheel) is True
    assert verify_sdist(sdist) is True


def test_generate_sbom_includes_project_and_release_files(tmp_path):
    wheel = tmp_path / "xpst-0.1.0-py3-none-any.whl"
    exe = tmp_path / "xPST.exe"
    _make_wheel(wheel)
    exe.write_bytes(b"binary")
    output = tmp_path / "xpst-sbom.cdx.json"

    generate_sbom(tmp_path, output, "0.1.0")
    data = json.loads(output.read_text(encoding="utf-8"))

    assert data["bomFormat"] == "CycloneDX"
    assert data["specVersion"] == "1.5"
    assert data["serialNumber"].startswith("urn:uuid:")
    UUID(data["serialNumber"].removeprefix("urn:uuid:"))
    assert data["metadata"]["component"]["name"] == "xpst"
    component_names = {component["name"] for component in data["components"]}
    assert "xpst-0.1.0-py3-none-any.whl" in component_names
    assert "xPST.exe" in component_names


def test_copy_project_documents_includes_open_source_notices(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "LICENSE").write_text("license text", encoding="utf-8")
    (project_root / "NOTICES.md").write_text("notice text", encoding="utf-8")
    (project_root / "NOTICES_QT_LGPL.md").write_text("qt lgpl relink offer", encoding="utf-8")
    (project_root / "LICENSING_REPORT.md").write_text("license report", encoding="utf-8")
    (project_root / "CHANGELOG.md").write_text("changelog", encoding="utf-8")
    output = tmp_path / "release"
    output.mkdir()
    monkeypatch.chdir(project_root)

    copy_project_documents(output)

    assert (output / "LICENSE").read_text(encoding="utf-8") == "license text"
    assert (output / "NOTICES.md").read_text(encoding="utf-8") == "notice text"
    assert (output / "NOTICES_QT_LGPL.md").read_text(encoding="utf-8") == "qt lgpl relink offer"
    assert (output / "LICENSING_REPORT.md").read_text(encoding="utf-8") == "license report"
    assert (output / "CHANGELOG.md").read_text(encoding="utf-8") == "changelog"


def test_copy_project_documents_requires_qt_lgpl_notice(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "LICENSE").write_text("license text", encoding="utf-8")
    output = tmp_path / "release"
    output.mkdir()
    monkeypatch.chdir(project_root)

    try:
        copy_project_documents(output)
    except FileNotFoundError as exc:
        assert "NOTICES_QT_LGPL.md" in str(exc)
    else:  # pragma: no cover - guard against silent regression
        raise AssertionError("copy_project_documents must require the Qt LGPL notice")

    assert not (output / "NOTICES_QT_LGPL.md").exists()


def test_generate_release_evidence_includes_artifacts_and_manual_gates(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    wheel = dist / "xpst-0.1.0-py3-none-any.whl"
    sdist = dist / "xpst-0.1.0.tar.gz"
    _make_wheel(wheel)
    _make_sdist(sdist, scratch)
    output_dir = tmp_path / "release"
    output_dir.mkdir()
    for name in [
        "SHA256SUMS",
        "SHA512SUMS",
        "pypi.json",
        "xpst-sbom.cdx.json",
        "RELEASE_NOTES.md",
        "LICENSE",
        "NOTICES.md",
        "LICENSING_REPORT.md",
        "CHANGELOG.md",
    ]:
        (output_dir / name).write_text(name, encoding="utf-8")
    output = output_dir / "RELEASE_EVIDENCE.json"

    generate_release_evidence(dist, output_dir, output, "0.1.0", checks_run=False)
    data = json.loads(output.read_text(encoding="utf-8"))

    assert data["schema_version"] == 1
    assert data["version"] == "0.1.0"
    assert data["quality_checks"]["run_by_release_script"] is False
    artifact_names = {artifact["filename"] for artifact in data["artifacts"]}
    assert artifact_names == {"xpst-0.1.0-py3-none-any.whl", "xpst-0.1.0.tar.gz"}
    assert all("sha256" in artifact["hashes"] for artifact in data["artifacts"])
    generated = {item["filename"]: item["present"] for item in data["generated_files"]}
    assert generated["xpst-sbom.cdx.json"] is True
    assert any("Windows executable launch" in item for item in data["manual_validation_required"])
    assert "python scripts/release_preflight.py --public --live-evidence release/live-platforms.json --json" in data[
        "quality_checks"
    ]["required_commands"]
    assert "Release owner: python scripts/verify_live_platforms.py --require --json > release/live-platforms.json" in data[
        "quality_checks"
    ]["required_commands"]
    assert data["known_limitations"]


def test_release_preflight_local_mode_warns_for_desktop_gates(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    _make_wheel(dist / "xpst-0.1.0-py3-none-any.whl")
    _make_sdist(dist / "xpst-0.1.0.tar.gz", tmp_path)
    for name in [
        "WINDOWS_CERTIFICATE_BASE64",
        "WINDOWS_CERTIFICATE_PATH",
        "WINDOWS_CERTIFICATE_PASSWORD",
        "MACOS_CODESIGN_IDENTITY",
        "APPLE_ID",
        "APPLE_TEAM_ID",
        "APPLE_APP_PASSWORD",
    ]:
        monkeypatch.delenv(name, raising=False)

    result = build_release_preflight(dist, public=False)

    assert result["ok"] is True
    warning_ids = {item["id"] for item in result["warnings"]}
    assert {
        "windows_desktop_artifact",
        "macos_desktop_artifact",
        "windows_signing",
        "macos_notarization",
        "live_platform_validation",
    } <= warning_ids


def test_release_preflight_build_actions_use_safe_wrapper(tmp_path):
    dist = tmp_path / "missing-dist"

    result = build_release_preflight(dist, public=False)

    actions = {item["id"]: item["action"] for item in result["checks"]}
    expected = "Run python scripts/build_package.py before release preflight."
    assert actions["dist_dir"] == expected
    assert actions["wheel"] == expected
    assert actions["sdist"] == expected


def test_build_package_runs_from_temp_directory(tmp_path):
    calls = []

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, cwd, **_kwargs):
        calls.append((command, Path(cwd)))
        return Completed()

    with patch("scripts.build_package.subprocess.run", side_effect=fake_run):
        result = build_package(tmp_path / "dist")

    assert result == 0
    command, cwd = calls[0]
    assert command[2:] == ["build", str(BUILD_ROOT), "--outdir", str((tmp_path / "dist").resolve())]
    assert cwd != BUILD_ROOT


def test_build_package_falls_back_to_uv_when_build_module_unavailable(tmp_path):
    calls = []

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "missing build"

    class Passed:
        returncode = 0
        stdout = "built"
        stderr = ""

    def fake_run(command, cwd, **_kwargs):
        calls.append((command, Path(cwd)))
        return Failed() if command[:3] != ["uv", "build", "--out-dir"] else Passed()

    with (
        patch("scripts.build_package.subprocess.run", side_effect=fake_run),
        patch("scripts.build_package.shutil.which", return_value="uv"),
    ):
        result = build_package(tmp_path / "dist")

    assert result == 0
    assert calls[1] == (
        ["uv", "build", "--out-dir", str((tmp_path / "dist").resolve())],
        BUILD_ROOT,
    )


def test_release_preflight_public_mode_blocks_missing_desktop_gates(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    _make_wheel(dist / "xpst-0.1.0-py3-none-any.whl")
    _make_sdist(dist / "xpst-0.1.0.tar.gz", tmp_path)
    for name in [
        "WINDOWS_CERTIFICATE_BASE64",
        "WINDOWS_CERTIFICATE_PATH",
        "WINDOWS_CERTIFICATE_PASSWORD",
        "MACOS_CODESIGN_IDENTITY",
        "APPLE_ID",
        "APPLE_TEAM_ID",
        "APPLE_APP_PASSWORD",
    ]:
        monkeypatch.delenv(name, raising=False)

    result = build_release_preflight(dist, public=True)

    assert result["ok"] is False
    blocking_ids = {item["id"] for item in result["blocking"]}
    assert {
        "windows_desktop_artifact",
        "macos_desktop_artifact",
        "macos_notarization",
        "live_platform_validation",
    } <= blocking_ids


def _write_live_evidence(path: Path, ok: bool = True, mode: str = "required") -> Path:
    path.write_text(
        json.dumps({
            "ok": ok,
            "mode": mode,
            "results": [
                {"platform": "youtube", "status": "passed", "ok": True},
                {"platform": "instagram", "status": "passed", "ok": True},
                {"platform": "x", "status": "passed", "ok": True},
            ],
            "blocking": [],
        }),
        encoding="utf-8",
    )
    return path


def test_release_preflight_public_mode_accepts_artifacts_signing_and_live_evidence(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    _make_wheel(dist / "xpst-0.1.0-py3-none-any.whl")
    _make_sdist(dist / "xpst-0.1.0.tar.gz", tmp_path)
    (dist / "xPST.exe").write_bytes(b"windows binary")
    (dist / "xPST.dmg").write_bytes(b"macos disk image")
    live_evidence = _write_live_evidence(tmp_path / "live-platforms.json")

    monkeypatch.setenv("WINDOWS_CERTIFICATE_BASE64", "placeholder")
    monkeypatch.setenv("WINDOWS_CERTIFICATE_PASSWORD", "placeholder")
    monkeypatch.setenv("MACOS_CODESIGN_IDENTITY", "Developer ID Application: Example (TEAMID)")
    monkeypatch.setenv("APPLE_ID", "release@example.com")
    monkeypatch.setenv("APPLE_TEAM_ID", "TEAMID")
    monkeypatch.setenv("APPLE_APP_PASSWORD", "placeholder")

    result = build_release_preflight(dist, public=True, live_evidence=live_evidence)

    assert result["ok"] is True
    assert result["blocking"] == []


def test_release_preflight_rejects_non_required_live_evidence(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    evidence = _write_live_evidence(tmp_path / "live-platforms.json", mode="optional")

    result = build_release_preflight(dist, public=True, live_evidence=evidence)

    blocking = {item["id"]: item["message"] for item in result["blocking"]}
    assert "live_platform_validation" in blocking
    assert "required mode" in blocking["live_platform_validation"]


def test_release_preflight_accepts_live_evidence_with_utf8_bom(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    _make_wheel(dist / "xpst-0.1.0-py3-none-any.whl")
    _make_sdist(dist / "xpst-0.1.0.tar.gz", tmp_path)
    (dist / "xPST.exe").write_bytes(b"windows binary")
    (dist / "xPST.dmg").write_bytes(b"macos disk image")
    evidence = _write_live_evidence(tmp_path / "live-platforms.json")
    evidence.write_bytes(b"\xef\xbb\xbf" + evidence.read_bytes())

    monkeypatch.setenv("WINDOWS_CERTIFICATE_BASE64", "placeholder")
    monkeypatch.setenv("WINDOWS_CERTIFICATE_PASSWORD", "placeholder")
    monkeypatch.setenv("MACOS_CODESIGN_IDENTITY", "Developer ID Application: Example (TEAMID)")
    monkeypatch.setenv("APPLE_ID", "release@example.com")
    monkeypatch.setenv("APPLE_TEAM_ID", "TEAMID")
    monkeypatch.setenv("APPLE_APP_PASSWORD", "placeholder")

    result = build_release_preflight(dist, public=True, live_evidence=evidence)

    assert result["ok"] is True


def test_public_release_check_writes_live_and_preflight_evidence(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    _make_wheel(dist / "xpst-0.1.0-py3-none-any.whl")
    _make_sdist(dist / "xpst-0.1.0.tar.gz", tmp_path)
    (dist / "xPST.exe").write_bytes(b"windows binary")
    (dist / "xPST.dmg").write_bytes(b"macos disk image")
    live_result = {
        "ok": True,
        "mode": "required",
        "results": [
            {"platform": "youtube", "status": "passed", "ok": True},
            {"platform": "instagram", "status": "passed", "ok": True},
            {"platform": "x", "status": "passed", "ok": True},
        ],
        "blocking": [],
    }

    monkeypatch.setenv("WINDOWS_CERTIFICATE_BASE64", "placeholder")
    monkeypatch.setenv("WINDOWS_CERTIFICATE_PASSWORD", "placeholder")
    monkeypatch.setenv("MACOS_CODESIGN_IDENTITY", "Developer ID Application: Example (TEAMID)")
    monkeypatch.setenv("APPLE_ID", "release@example.com")
    monkeypatch.setenv("APPLE_TEAM_ID", "TEAMID")
    monkeypatch.setenv("APPLE_APP_PASSWORD", "placeholder")
    monkeypatch.setattr("scripts.public_release_check.verify_live_platforms", lambda _config, require: live_result)

    result = collect_public_release_evidence(output_dir=tmp_path / "release", dist_dir=dist, config_path="config.yaml")

    assert result["ok"] is True
    live_path = Path(result["live_evidence"])
    preflight_path = Path(result["public_preflight"])
    assert json.loads(live_path.read_text(encoding="utf-8")) == live_result
    assert json.loads(preflight_path.read_text(encoding="utf-8"))["ok"] is True


def test_public_release_check_blocks_when_live_validation_fails(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    _make_wheel(dist / "xpst-0.1.0-py3-none-any.whl")
    _make_sdist(dist / "xpst-0.1.0.tar.gz", tmp_path)
    live_result = {
        "ok": False,
        "mode": "required",
        "results": [{"platform": "youtube", "status": "failed", "ok": False}],
        "blocking": [{"platform": "youtube", "status": "failed", "ok": False}],
    }
    monkeypatch.setattr("scripts.public_release_check.verify_live_platforms", lambda _config, require: live_result)

    result = collect_public_release_evidence(output_dir=tmp_path / "release", dist_dir=dist)

    assert result["ok"] is False
    preflight = json.loads(Path(result["public_preflight"]).read_text(encoding="utf-8"))
    assert any(item["id"] == "live_platform_validation" for item in preflight["blocking"])


def test_windows_exe_smoke_reports_missing_artifact(tmp_path):
    result = verify_windows_exe(tmp_path / "missing.exe", seconds=1)

    assert result["ok"] is False
    assert "Artifact not found" in result["error"]


def test_windows_exe_smoke_accepts_clean_exit(tmp_path):
    if os.name != "nt":
        return

    script = tmp_path / "clean-exit.cmd"
    script.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")

    result = verify_windows_exe(script, seconds=1)

    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert result["signature"]["available"] is True


def test_windows_exe_smoke_can_use_clean_profile(tmp_path):
    if os.name != "nt":
        return

    marker = tmp_path / "profile-marker.txt"
    script = tmp_path / "clean-profile.cmd"
    script.write_text(
        f"@echo off\r\n"
        f"if \"%XPST_CLEAN_PROFILE_SMOKE%\"==\"1\" echo %USERPROFILE% > \"{marker}\"\r\n"
        f"exit /b 0\r\n",
        encoding="utf-8",
    )

    result = verify_windows_exe(script, seconds=1, clean_profile=True)

    assert result["ok"] is True
    assert result["clean_profile"] is True
    assert marker.exists()
    assert "xpst-clean-profile-" in marker.read_text(encoding="utf-8")


def test_windows_exe_smoke_blocks_unsigned_when_required(tmp_path, monkeypatch):
    if os.name != "nt":
        return

    script = tmp_path / "clean-exit.cmd"
    script.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
    monkeypatch.setattr(
        "scripts.verify_windows_exe._authenticode_status",
        lambda path: {"available": True, "status": "NotSigned", "message": "unsigned", "signed": False},
    )

    result = verify_windows_exe(script, seconds=1, require_signed=True)

    assert result["ok"] is False
    assert result["exit_code"] == 0
    assert result["require_signed"] is True
    assert "not signed" in result["error"]


def test_windows_exe_smoke_accepts_valid_signature_when_required(tmp_path, monkeypatch):
    if os.name != "nt":
        return

    script = tmp_path / "clean-exit.cmd"
    script.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
    monkeypatch.setattr(
        "scripts.verify_windows_exe._authenticode_status",
        lambda path: {"available": True, "status": "Valid", "message": "signed", "signed": True},
    )

    result = verify_windows_exe(script, seconds=1, require_signed=True)

    assert result["ok"] is True
    assert result["require_signed"] is True
    assert result["signature"]["signed"] is True


def test_macos_artifact_verifier_accepts_valid_bundle_shape(tmp_path):
    app = tmp_path / "xPST.app"
    macos_dir = app / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_text("<plist></plist>", encoding="utf-8")
    (macos_dir / "xPST").write_bytes(b"launcher")

    result = verify_macos_artifact(app)

    assert result["ok"] is True
    check_ids = {check["id"] for check in result["checks"]}
    assert {"app_bundle", "contents_dir", "macos_dir", "info_plist", "launcher_binary"} <= check_ids


def test_macos_artifact_verifier_records_public_requirements(tmp_path):
    app = tmp_path / "xPST.app"
    macos_dir = app / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_text("<plist></plist>", encoding="utf-8")
    (macos_dir / "xPST").write_bytes(b"launcher")

    result = verify_macos_artifact(app, require_developer_id=True, require_notarized=True)

    assert result["require_developer_id"] is True
    assert result["require_notarized"] is True


def test_macos_artifact_verifier_blocks_missing_bundle(tmp_path):
    result = verify_macos_artifact(tmp_path / "missing.app")

    assert result["ok"] is False
    blocking_ids = {check["id"] for check in result["blocking"]}
    assert "app_bundle" in blocking_ids
