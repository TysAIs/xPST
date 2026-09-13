"""Tests for built desktop artifact verification."""

from __future__ import annotations

import json
import plistlib
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.verify_desktop_package import verify_artifact
from scripts.verify_release_version import release_versions

ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, content: bytes = b"synthetic executable") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755)


def _make_bundle(tmp_path: Path, version: str = "1.1.0") -> Path:
    app = tmp_path / "xPST.app"
    contents = app / "Contents"
    (contents / "Resources" / "ui").mkdir(parents=True)
    (contents / "Resources" / "binaries" / "ffmpeg").mkdir(parents=True)
    (contents / "Resources" / "binaries" / "ytdlp").mkdir(parents=True)
    (contents / "Resources" / "binaries" / "engine").mkdir(parents=True)
    (contents / "Resources" / "ui" / "index.html").write_text(
        "<!doctype html><html><body>xPST</body></html>\n", encoding="utf-8"
    )
    for name in ("ffmpeg", "ffprobe"):
        _write_executable(contents / "Resources" / "binaries" / "ffmpeg" / name)
    _write_executable(contents / "Resources" / "binaries" / "ytdlp" / "yt-dlp")
    _write_executable(contents / "Resources" / "binaries" / "engine" / "xpst-engine")
    (contents / "MacOS").mkdir()
    _write_executable(contents / "MacOS" / "xPST")
    (contents / "Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleExecutable": "xPST",
                "CFBundleIdentifier": "com.tysais.xpst",
                "CFBundleName": "xPST",
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": version,
                "CFBundleVersion": version,
            }
        )
    )
    return app


def _checks(result: dict[str, object], check_id: str) -> dict[str, object]:
    return next(check for check in result["checks"] if check["id"] == check_id)  # type: ignore[index]


def test_release_version_sources_include_cargo_lock() -> None:
    versions = release_versions(ROOT)

    assert versions["cargo-lock"] == "1.1.0"


def test_good_bundle_records_machine_checkable_evidence(tmp_path: Path) -> None:
    app = _make_bundle(tmp_path)
    evidence_path = tmp_path / "RELEASE_EVIDENCE.json"

    result = verify_artifact(app, root=ROOT, evidence_output=evidence_path)

    assert result["ok"] is True
    assert _checks(result, "required_resources")["missing"] == []
    assert _checks(result, "version")["bundle"] == "1.1.0"
    hashes = _checks(result, "hashes")["hashes"]
    assert set(hashes) == {"sha256", "sha512"}
    assert _checks(result, "permissions")["issues"] == []
    assert _checks(result, "privacy")["findings"] == []
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["quality_checks"]["run_by_release_script"] is True
    assert evidence["signed"] is False
    assert evidence["notarized"] in (False, "unknown")
    assert evidence["verified_by"] in ("unknown", None)
    assert evidence["hashes"] == hashes


@pytest.mark.parametrize(
    ("relative_path", "label"),
    [
        ("Contents/Resources/ui/index.html", "ui/index.html"),
        ("Contents/Resources/binaries/engine/xpst-engine", "binaries/engine/xpst-engine"),
        ("Contents/Resources/binaries/ffmpeg/ffmpeg", "binaries/ffmpeg/ffmpeg"),
        ("Contents/Resources/binaries/ffmpeg/ffprobe", "binaries/ffmpeg/ffprobe"),
        ("Contents/Resources/binaries/ytdlp/yt-dlp", "binaries/ytdlp/yt-dlp"),
    ],
)
def test_missing_required_resource_fails_clearly(tmp_path: Path, relative_path: str, label: str) -> None:
    app = _make_bundle(tmp_path)
    missing = app / relative_path
    missing.unlink()

    result = verify_artifact(app, root=ROOT)

    assert result["ok"] is False
    resource_check = _checks(result, "required_resources")
    assert label in resource_check["missing"]
    assert any(label in issue for issue in resource_check["issues"])


def test_bundle_version_mismatch_fails_clearly(tmp_path: Path) -> None:
    app = _make_bundle(tmp_path, version="9.9.9")

    result = verify_artifact(app, root=ROOT)

    assert result["ok"] is False
    version_check = _checks(result, "version")
    assert version_check["bundle"] == "9.9.9"
    assert any("does not match expected 1.1.0" in issue for issue in version_check["issues"])


def test_planted_personal_data_fails_without_echoing_values(tmp_path: Path) -> None:
    app = _make_bundle(tmp_path)
    secret = "sk-" + "abcdefghijklmnopqrstuvwxyz"
    planted = (
        "email=user@example.com phone=+1 (212) 555-0199 "
        "home=/home/example/private api_key=" + secret
    )
    (app / "Contents" / "Resources" / "ui" / "index.html").write_text(planted, encoding="utf-8")

    result = verify_artifact(app, root=ROOT)

    assert result["ok"] is False
    privacy_check = _checks(result, "privacy")
    kinds = {finding["kind"] for finding in privacy_check["findings"]}  # type: ignore[index]
    assert {"email", "phone", "absolute_home_path", "secret"} <= kinds
    serialized = json.dumps(result)
    for value in ("user@example.com", "/home/example/private", secret):
        assert value not in serialized


def test_non_executable_required_binary_fails_clearly(tmp_path: Path) -> None:
    app = _make_bundle(tmp_path)
    engine = app / "Contents" / "Resources" / "binaries" / "engine" / "xpst-engine"
    engine.chmod(0o644)

    result = verify_artifact(app, root=ROOT)

    assert result["ok"] is False
    permission_check = _checks(result, "permissions")
    assert any("xpst-engine" in issue and "executable" in issue for issue in permission_check["issues"])


def test_zip_containing_bundle_is_verified(tmp_path: Path) -> None:
    app = _make_bundle(tmp_path)
    archive_path = tmp_path / "xPST.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for path in sorted(app.rglob("*")):
            if path.is_file():
                info = zipfile.ZipInfo(path.relative_to(app.parent).as_posix())
                info.external_attr = (path.stat().st_mode & 0o7777) << 16
                archive.writestr(info, path.read_bytes())

    result = verify_artifact(archive_path, root=ROOT)

    assert result["ok"] is True
    assert result["artifact"]["type"] == "zip"  # type: ignore[index]


def test_tar_updater_archive_is_verified(tmp_path: Path) -> None:
    app = _make_bundle(tmp_path)
    archive_path = tmp_path / "xPST_1.1.0_aarch64.app.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(app, arcname=app.name)

    result = verify_artifact(archive_path, root=ROOT)

    assert result["ok"] is True
    assert result["artifact"]["type"] == "installer"  # type: ignore[index]


def test_release_workflow_verifies_downloaded_desktop_assets() -> None:
    workflow = (ROOT / ".github" / "workflows" / "verify-release-artifacts.yml").read_text(encoding="utf-8")

    assert "types: [published]" in workflow
    assert "workflow_dispatch:" in workflow
    assert "gh release download" in workflow
    assert "scripts/verify_desktop_package.py" in workflow
    assert "scripts/clean_install_smoke.py" in workflow
    assert "release-evidence/*" in workflow
    assert "tauri-release.yml" in workflow
