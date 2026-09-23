"""Tests for the release-asset selection used by the updater manifest job."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "select_updater_artifacts", ROOT / "scripts/select-updater-artifacts.py"
)
assert SPEC and SPEC.loader
selector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(selector)

PLATFORMS = (
    "darwin-aarch64",
    "darwin-x86_64",
    "windows-x86_64",
    "linux-x86_64",
)


def _artifact(directory: Path, name: str, *, signature: str | None = "untrusted comment: fixture\nsig\n") -> Path:
    path = directory / name
    path.write_bytes(b"fixture artifact\n")
    if signature is not None:
        path.with_name(path.name + ".sig").write_text(signature, encoding="utf-8")
    return path


def _statuses(results: list[dict]) -> dict[str, str]:
    return {result["platform"]: result["status"] for result in results}


EXPLICIT_NAMES = {
    "darwin-aarch64": "darwin-aarch64-xPST.app.tar.gz",
    "darwin-x86_64": "darwin-x86_64-xPST.app.tar.gz",
    "windows-x86_64": "windows-x86_64-xPST-setup.exe",
    "linux-x86_64": "linux-x86_64-xPST.AppImage",
}


def test_explicit_target_prefixes_resolve_every_platform(tmp_path: Path) -> None:
    for name in EXPLICIT_NAMES.values():
        _artifact(tmp_path, name)

    results = selector.select_all(tmp_path)

    assert _statuses(results) == {platform: "ok" for platform in PLATFORMS}
    assert all(result["source"] == "explicit-prefix" for result in results)


def test_bundler_default_names_are_mapped_to_targets(tmp_path: Path) -> None:
    _artifact(tmp_path, "xPST_aarch64.app.tar.gz")
    _artifact(tmp_path, "xPST_x64.app.tar.gz")
    _artifact(tmp_path, "xPST_1.1.0_x64-setup.exe")
    _artifact(tmp_path, "xPST_1.1.0_amd64.AppImage")

    assert _statuses(selector.select_all(tmp_path)) == {platform: "ok" for platform in PLATFORMS}


def test_missing_signature_is_reported_not_invented(tmp_path: Path) -> None:
    _artifact(tmp_path, "darwin-aarch64-xPST.app.tar.gz", signature=None)

    result = selector.select("darwin-aarch64", sorted(tmp_path.iterdir()))

    assert result["status"] == "unsigned"
    assert result["missing_signature"].endswith("darwin-aarch64-xPST.app.tar.gz.sig")


def test_empty_signature_is_rejected(tmp_path: Path) -> None:
    _artifact(tmp_path, "linux-x86_64-xPST.AppImage", signature="\n")

    result = selector.select("linux-x86_64", sorted(tmp_path.iterdir()))

    assert result["status"] == "empty-signature"


def test_ambiguous_platform_is_refused_rather_than_guessed(tmp_path: Path) -> None:
    _artifact(tmp_path, "xPST_1.1.0_amd64.AppImage")
    _artifact(tmp_path, "xPST_1.1.0_arm64.AppImage")

    result = selector.select("linux-x86_64", sorted(tmp_path.iterdir()))

    assert result["status"] == "ambiguous"
    assert len(result["candidates"]) == 2


def test_release_bookkeeping_files_are_not_updater_artifacts(tmp_path: Path) -> None:
    _artifact(tmp_path, "linux-SHA512SUMS", signature=None)
    _artifact(tmp_path, "linux-RELEASE_EVIDENCE.json", signature=None)
    _artifact(tmp_path, "windows-SHA256SUMS", signature=None)

    results = selector.select_all(tmp_path)

    assert _statuses(results) == {platform: "absent" for platform in PLATFORMS}
    assert selector.select_all(tmp_path)[0]["candidates"] == []


def test_cli_writes_report_args_and_github_output(tmp_path: Path) -> None:
    _artifact(tmp_path, "darwin-aarch64-xPST.app.tar.gz")
    args_file = tmp_path / "args.txt"
    report_file = tmp_path / "report.json"
    output_file = tmp_path / "github-output"

    rc = selector.main(
        [
            "--dir",
            str(tmp_path),
            "--report",
            str(report_file),
            "--args-file",
            str(args_file),
            "--github-output",
            str(output_file),
        ]
    )

    assert rc == 0
    assert args_file.read_text(encoding="utf-8").startswith("darwin-aarch64=")
    assert "found=1" in output_file.read_text(encoding="utf-8")
    assert "windows-x86_64" in output_file.read_text(encoding="utf-8")
    assert '"found": 1' in report_file.read_text(encoding="utf-8")


def test_windows_and_linux_updater_archives_are_recognized(tmp_path: Path) -> None:
    """The lanes sign `*.nsis.zip` / `*.AppImage.tar.gz`, not the raw installers."""
    _artifact(tmp_path, "xPST_1.1.0_x64-setup.nsis.zip")
    _artifact(tmp_path, "xPST_1.1.0_amd64.AppImage.tar.gz")

    statuses = _statuses(selector.select_all(tmp_path))

    assert statuses["windows-x86_64"] == "ok"
    assert statuses["linux-x86_64"] == "ok"


def test_explicit_nsis_and_appimage_tarball_prefixes_resolve(tmp_path: Path) -> None:
    _artifact(tmp_path, "windows-x86_64-xPST_1.1.0_x64-setup.nsis.zip")
    _artifact(tmp_path, "linux-x86_64-xPST_1.1.0_amd64.AppImage.tar.gz")

    results = {result["platform"]: result for result in selector.select_all(tmp_path)}

    assert results["windows-x86_64"]["status"] == "ok"
    assert results["linux-x86_64"]["status"] == "ok"
    assert results["windows-x86_64"]["source"] == "explicit-prefix"


def test_signed_updater_archive_beats_the_unsigned_raw_installer(tmp_path: Path) -> None:
    """Both files ship in one release; only the archive carries a signature."""
    _artifact(tmp_path, "xPST_1.1.0_x64-setup.exe", signature=None)
    _artifact(tmp_path, "xPST_1.1.0_x64-setup.nsis.zip")
    _artifact(tmp_path, "xPST_1.1.0_amd64.AppImage", signature=None)
    _artifact(tmp_path, "xPST_1.1.0_amd64.AppImage.tar.gz")

    windows = selector.select("windows-x86_64", sorted(tmp_path.iterdir()))
    linux = selector.select("linux-x86_64", sorted(tmp_path.iterdir()))

    assert windows["status"] == "ok"
    assert windows["artifact"].endswith("xPST_1.1.0_x64-setup.nsis.zip")
    assert linux["status"] == "ok"
    assert linux["artifact"].endswith("xPST_1.1.0_amd64.AppImage.tar.gz")
