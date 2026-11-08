"""Clean-profile stranger-install E2E harness tests.

These tests exercise ``scripts/e2e_install_from_artifact.py`` without touching
the network: release resolution is fed a fake asset listing, and the end-to-end
runs install a synthetic published artifact (a zip carrying a fake ``xPST.app``
whose executable serves a real loopback ``/health`` and HTML root).

The synthetic run also proves the harness is *red capable*: the same artifact
with a wrong published checksum must fail with exit status 1.
"""

import hashlib
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

from scripts import e2e_install_from_artifact as e2e_install_module
from scripts.e2e_install_from_artifact import (
    DEFAULT_WORK_BASE,
    E2EError,
    WorkDir,
    architecture_match_score,
    artifact_type,
    asset_preference_score,
    checksum_source_candidates,
    deb_install_record,
    default_platform,
    derive_checksum_source,
    emit_summary,
    engine_processes,
    gatekeeper_report,
    headless_display_prefix,
    host_arch,
    install_artifact,
    install_deb,
    installer_candidates,
    is_nsis_installer,
    load_first_checksum_source,
    local_urls_from_log,
    main,
    observe_engine_sidecar,
    observe_packaged_ui,
    packaged_ui_sample,
    parse_checksums,
    platform_key,
    product_executable,
    release_assets,
    release_assets_from_html,
    safe_extract_zip,
    select_release_asset,
    sniff_artifact_kind,
    stop_work_processes,
    uninstall_artifact,
    verify_checksum,
    walk_app_bundles,
)

SHA_A = "a" * 64
SHA_B = "b" * 64

FAKE_APP_SOURCE = '''#!/usr/bin/env python3
"""Stand-in for the packaged desktop app: serves /health and an HTML root."""
import http.server
import socketserver

HTML = b"<html><head><title>xPST fake</title></head><body>xPST</body></html>"
HEALTH = b'{"status": "ok", "engine": "xPST-fake"}'


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/health"):
            body, content_type = HEALTH, "application/json"
        else:
            body, content_type = HTML, "text/html"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


with socketserver.TCPServer(("127.0.0.1", 0), Handler) as httpd:
    print("engine port: %d" % httpd.server_address[1], flush=True)
    print("http://127.0.0.1:%d/" % httpd.server_address[1], flush=True)
    httpd.serve_forever()
'''


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


ENGINE_SOURCE = '''#!/usr/bin/env python3
"""Stand-in for the packaged xpst-engine sidecar: serves /health and the UI."""
import http.server
import socketserver

HTML = b"<html><head><title>xPST</title></head><body>xPST UI</body></html>"
HEALTH = b\'{"status": "ok", "engine": "xPST-fake"}\'


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/health"):
            body, content_type = HEALTH, "application/json"
        else:
            body, content_type = HTML, "text/html"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


with socketserver.TCPServer(("127.0.0.1", 0), Handler) as httpd:
    print("engine port: %d" % httpd.server_address[1], flush=True)
    httpd.serve_forever()
'''

STALE_FIRST_UI_SOURCE = '''#!/usr/bin/env python3
"""Stand-in for the packaged app during a shell re-exec.

It serves a non-HTML root on the first request (the instant the harness used to
sample), then the packaged HTML. This is the macOS 42508682981 shape: healthy
/health, a root that answers 200, and the packaged index a moment later.
"""
import http.server
import socketserver

HTML = b"<html><head><title>xPST Dashboard</title></head><body>xPST</body></html>"
HEALTH = b'{"status": "ok", "engine": "xPST-fake"}'
ROOT_REQUESTS = [0]


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/health"):
            body, content_type = HEALTH, "application/json"
        else:
            ROOT_REQUESTS[0] += 1
            if ROOT_REQUESTS[0] == 1:
                body = b'{"detail": "UI not mounted yet"}'
            else:
                body = HTML
            content_type = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


with socketserver.TCPServer(("127.0.0.1", 0), Handler) as httpd:
    print("engine port: %d" % httpd.server_address[1], flush=True)
    print("http://127.0.0.1:%d/" % httpd.server_address[1], flush=True)
    httpd.serve_forever()
'''

TAURI_APP_SOURCE = '''#!/usr/bin/env python3
"""Stand-in for the Tauri shell: it only spawns its engine sidecar."""
import pathlib
import subprocess
import time

here = pathlib.Path(__file__).resolve().parent
engine = here.parent / "Resources" / "binaries" / "engine" / "xpst-engine"
subprocess.Popen([str(engine)])
while True:
    time.sleep(1)
'''


def _write_script(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _zip_dir(payload: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w") as archive:
        for path in sorted(payload.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(payload).as_posix())


def _bundle_info_plist(app: Path) -> None:
    plist = {
        "CFBundleExecutable": "xPST",
        "CFBundleIdentifier": "com.tysais.xpst.fake",
        "CFBundleName": "xPST",
        "CFBundleShortVersionString": "0.0.0-test",
    }
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(plist))


def build_fake_published_artifact(tmp_path: Path, *, name: str = "xPST-macos-arm64.zip") -> tuple[Path, Path]:
    """Create a synthetic published artifact plus its release checksum file.

    The payload is a bare ``.app`` whose executable is the thing under test:
    it serves the loopback ``/health`` and HTML root itself.
    """
    payload = tmp_path / "payload"
    app = payload / "xPST.app"
    _write_script(app / "Contents" / "MacOS" / "xPST", FAKE_APP_SOURCE)
    _bundle_info_plist(app)
    artifact = tmp_path / name
    _zip_dir(payload, artifact)
    checksums = tmp_path / "macos-SHA256SUMS"
    checksums.write_text(f"{_sha256(artifact)}  {artifact.name}\n", encoding="utf-8")
    return artifact, checksums


def build_fake_stale_ui_published_artifact(
    tmp_path: Path, *, name: str = "xPST-macos-arm64.zip"
) -> tuple[Path, Path]:
    """An artifact whose first root response is not the packaged HTML.

    Pins the 42508682981 shape end to end: the harness must retry the root and
    pass, recording the attempt count, instead of failing on the first sample.
    """
    payload = tmp_path / "payload-stale"
    app = payload / "xPST.app"
    _write_script(app / "Contents" / "MacOS" / "xPST", STALE_FIRST_UI_SOURCE)
    _bundle_info_plist(app)
    artifact = tmp_path / name
    _zip_dir(payload, artifact)
    checksums = tmp_path / "macos-SHA256SUMS"
    checksums.write_text(f"{_sha256(artifact)}  {artifact.name}\n", encoding="utf-8")
    return artifact, checksums


def build_fake_tauri_published_artifact(
    tmp_path: Path, *, name: str = "xPST-macos-arm64.zip"
) -> tuple[Path, Path]:
    """Create a Tauri-shaped synthetic artifact: UI assets plus an engine sidecar.

    This is the shape the definition of done requires, so the harness must be
    able to go green on it: a real ``xpst-engine`` process, ``/health`` 200 and
    the packaged HTML served from the healthy loopback root.
    """
    payload = tmp_path / "payload-tauri"
    app = payload / "xPST.app"
    _write_script(app / "Contents" / "MacOS" / "xPST", TAURI_APP_SOURCE)
    _write_script(app / "Contents" / "Resources" / "binaries" / "engine" / "xpst-engine", ENGINE_SOURCE)
    ui = app / "Contents" / "Resources" / "ui" / "index.html"
    ui.parent.mkdir(parents=True, exist_ok=True)
    ui.write_text("<!doctype html><title>xPST</title><body>xPST UI</body>", encoding="utf-8")
    _bundle_info_plist(app)
    artifact = tmp_path / name
    _zip_dir(payload, artifact)
    checksums = tmp_path / "macos-SHA256SUMS"
    checksums.write_text(f"{_sha256(artifact)}  {artifact.name}\n", encoding="utf-8")
    return artifact, checksums


def test_parse_checksums_accepts_gnu_and_bsd_forms():
    text = "\n".join(
        [
            "# comment",
            f"{SHA_A}  xPST.dmg",
            f"{SHA_B} *xpst-1.1.0.tar.gz",
            f"SHA256 (xpst-1.1.0-py3-none-any.whl) = {SHA_A}",
            "",
        ]
    )
    entries = parse_checksums(text)
    assert entries == {
        "xPST.dmg": SHA_A,
        "xpst-1.1.0.tar.gz": SHA_B,
        "xpst-1.1.0-py3-none-any.whl": SHA_A,
    }


def test_parse_checksums_ignores_unparseable_lines():
    assert parse_checksums("not a checksum line\n") == {}


def test_platform_key_infers_known_installers():
    assert platform_key("xPST.dmg", None) == "macos"
    assert platform_key("xPST-macos-arm64.zip", None) == "macos"
    assert platform_key("xPST.exe", None) == "windows"
    assert platform_key("xPST.AppImage", None) == "linux"
    assert platform_key("anything", "linux") == "linux"


def test_platform_key_uses_magic_bytes_for_extensionless_assets():
    assert platform_key("xPST", None, "elf") == "linux"
    assert platform_key("xPST", None, "macho") == "macos"
    assert platform_key("xPST", None, "pe") == "windows"


def test_platform_key_fails_closed_when_unknown():
    with pytest.raises(E2EError):
        platform_key("mystery-asset", None)


def test_default_platform_matches_host():
    expected = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
    assert default_platform() == expected


def test_sniff_artifact_kind_reads_magic_bytes(tmp_path):
    elf = tmp_path / "xPST"
    elf.write_bytes(b"\x7fELF" + b"\x00" * 32)
    macho = tmp_path / "xPST-macho"
    macho.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 32)
    pe = tmp_path / "xPST-pe"
    pe.write_bytes(b"MZ" + b"\x00" * 32)
    zipped = tmp_path / "archive"
    zipped.write_bytes(b"PK\x03\x04" + b"\x00" * 32)
    unknown = tmp_path / "blob"
    unknown.write_bytes(b"\x00\x01\x02\x03")

    assert sniff_artifact_kind(elf) == "elf"
    assert sniff_artifact_kind(macho) == "macho"
    assert sniff_artifact_kind(pe) == "pe"
    assert sniff_artifact_kind(zipped) == "zip"
    assert sniff_artifact_kind(unknown) is None
    assert sniff_artifact_kind(tmp_path / "missing") is None


def test_artifact_type_accepts_published_extensionless_linux_binary(tmp_path):
    binary = tmp_path / "xPST"
    binary.write_bytes(b"\x7fELF" + b"\x00" * 32)

    assert artifact_type("xPST", binary) == "binary"


def test_artifact_type_prefers_the_extension(tmp_path):
    dmg = tmp_path / "xPST.dmg"
    dmg.write_bytes(b"not-a-real-disk-image")

    assert artifact_type("xPST.dmg", dmg) == "dmg"
    assert artifact_type("xPST.exe") == "exe"
    assert artifact_type("xPST.AppImage") == "appimage"


def test_artifact_type_rejects_unknown_payload(tmp_path):
    blob = tmp_path / "xPST.bin"
    blob.write_bytes(b"\x00" * 16)

    with pytest.raises(E2EError):
        artifact_type("xPST.bin", blob)


def test_select_release_asset_picks_the_platform_installer():
    assets = [
        {"name": "xPST.dmg", "browser_download_url": "https://example.invalid/dmg", "size": 1},
        {"name": "xPST.exe", "browser_download_url": "https://example.invalid/exe", "size": 2},
        {"name": "SHA256SUMS", "browser_download_url": "https://example.invalid/sums"},
    ]

    assert select_release_asset(assets, "macos")["name"] == "xPST.dmg"
    assert select_release_asset(assets, "windows")["name"] == "xPST.exe"
    assert select_release_asset(assets, "macos", "xPST.exe")["name"] == "xPST.exe"


def test_select_release_asset_reports_published_names_when_unknown():
    assets = [{"name": "weird.tar.zst", "browser_download_url": "https://example.invalid/x"}]

    with pytest.raises(E2EError) as excinfo:
        select_release_asset(assets, "linux")

    assert "weird.tar.zst" in str(excinfo.value)


def test_derive_checksum_source_from_release_download_url():
    url = "https://github.com/TysAIs/xPST/releases/download/v1.1.0/xPST.dmg"

    assert (
        derive_checksum_source(url, "xPST.dmg", "macos", "TysAIs/xPST", None)
        == "https://github.com/TysAIs/xPST/releases/download/v1.1.0/macos-SHA256SUMS"
    )


def test_derive_checksum_source_requires_release_tag_for_local_files(tmp_path):
    with pytest.raises(E2EError):
        derive_checksum_source(str(tmp_path / "xPST.dmg"), "xPST.dmg", "macos", "TysAIs/xPST", None)


def test_verify_checksum_matches_and_fails_closed(tmp_path):
    artifact = tmp_path / "xPST.dmg"
    artifact.write_bytes(b"payload")
    good = f"{_sha256(artifact)}  xPST.dmg\n"

    result = verify_checksum(artifact, good, "release:macos-SHA256SUMS")
    assert result["ok"] is True
    assert result["bytes"] == len(b"payload")

    with pytest.raises(E2EError) as excinfo:
        verify_checksum(artifact, f"{SHA_B}  xPST.dmg\n", "release:macos-SHA256SUMS")
    assert "checksum mismatch" in str(excinfo.value)

    with pytest.raises(E2EError):
        verify_checksum(artifact, f"{SHA_A}  something-else.dmg\n", "release:macos-SHA256SUMS")


def test_safe_extract_zip_rejects_path_traversal(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../escaped.txt", "nope")

    with pytest.raises(E2EError):
        safe_extract_zip(archive, tmp_path / "out")


def test_safe_extract_zip_rejects_non_zip(tmp_path):
    bogus = tmp_path / "bogus.zip"
    bogus.write_bytes(b"not a zip")

    with pytest.raises(E2EError):
        safe_extract_zip(bogus, tmp_path / "out")


def test_local_urls_from_log_finds_engine_urls():
    text = "boot ok\nengine port: 43111\nlistening on http://127.0.0.1:43112\n"

    assert set(local_urls_from_log(text)) == {
        "http://127.0.0.1:43111/",
        "http://127.0.0.1:43112/",
    }


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX executable bit")
def test_safe_extract_zip_restores_the_executable_bit(tmp_path):
    payload = tmp_path / "xPST.app" / "Contents" / "MacOS"
    payload.mkdir(parents=True)
    binary = payload / "xPST"
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    archive = tmp_path / "xPST-macos-arm64.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.write(binary, "xPST.app/Contents/MacOS/xPST")
    binary.chmod(0o644)

    out = tmp_path / "out"
    out.mkdir()
    safe_extract_zip(archive, out)

    extracted = out / "xPST.app" / "Contents" / "MacOS" / "xPST"
    assert extracted.stat().st_mode & stat.S_IXUSR


def test_uninstall_artifact_removes_every_throwaway_path(tmp_path):
    installed = tmp_path / "install" / "xPST.app"
    (installed / "Contents").mkdir(parents=True)
    (installed / "Contents" / "Info.plist").write_text("x", encoding="utf-8")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("x", encoding="utf-8")
    home_dir = tmp_path / "home"
    (home_dir / ".xpst").mkdir(parents=True)

    result = uninstall_artifact(installed, config_dir, home_dir)

    assert result["installed_artifact_removed"] is True
    assert result["config_dir_removed"] is True
    assert result["home_profile_removed"] is True
    assert not installed.exists()
    assert not config_dir.exists()
    assert not home_dir.exists()


def test_gatekeeper_report_never_claims_an_unobserved_approval():
    signature = {
        "checked": True,
        "codesign_exit": 0,
        "codesign_stderr": "Signature=adhoc\nTeamIdentifier=not set\n",
        "gatekeeper_accepted": False,
        "ad_hoc": True,
    }

    report = gatekeeper_report(signature, None, required=False)

    if sys.platform == "darwin":
        assert report["ad_hoc_signed"] is True
        assert report["developer_id_signed"] is False
        assert report["spctl_accepted"] is False
        assert report["launchservices_assessment_exercised"] is False
        assert "Open Anyway" in report["remediation"]
    else:
        assert report["checked"] is False


def test_emit_summary_writes_machine_readable_evidence(tmp_path, capsys):
    out = tmp_path / "nested" / "evidence.json"
    emit_summary({"ok": True, "checks": {"checksum": True}}, str(out))

    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["ok"] is True
    assert written["evidence_written_to"] == str(out)
    assert json.loads(capsys.readouterr().out.strip())["ok"] is True


def test_main_requires_an_artifact_or_a_release(tmp_path, capsys):
    code = main(["--evidence-out", str(tmp_path / "evidence.json")])

    assert code == 1
    summary = json.loads(capsys.readouterr().out.strip())
    assert summary["ok"] is False
    assert "pass an artifact path/URL" in summary["failures"][0]
    assert json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))["ok"] is False


def test_main_rejects_release_plus_explicit_artifact(tmp_path, capsys):
    code = main(["--release", "v1.1.0", str(tmp_path / "xPST.dmg"), "--evidence-out", str(tmp_path / "e.json")])

    assert code == 1
    summary = json.loads(capsys.readouterr().out.strip())
    assert "not both" in summary["failures"][0]


@pytest.mark.skipif(os.name == "nt", reason="process-group teardown semantics differ on Windows")
def test_harness_passes_against_a_synthetic_published_artifact(tmp_path, capsys):
    artifact, checksums = build_fake_published_artifact(tmp_path)
    evidence = tmp_path / "evidence.json"

    code = main(
        [
            str(artifact),
            "--checksums",
            str(checksums),
            "--no-require-visible-window",
            "--evidence-out",
            str(evidence),
        ]
    )
    capsys.readouterr()
    summary = json.loads(evidence.read_text(encoding="utf-8"))

    assert code == 0, summary["failures"]
    assert summary["status"] == "passed"
    assert summary["checks"] == {
        "checksum": True,
        "install": True,
        "boot_to_visible": True,
        "engine_health_200": True,
        "packaged_ui_served": True,
        "real_running_process": True,
        "zero_xpst_engine_processes": True,
        "cleanup": True,
        "uninstall": True,
    }
    assert summary["evidence"]["artifact_sha256"] == _sha256(artifact)
    assert summary["evidence"]["http_status"] == 200
    assert summary["evidence"]["health_ok"] is True
    assert summary["evidence"]["uninstall_ok"] is True
    assert summary["evidence"]["window_assertion_required"] is False
    assert summary["evidence"]["running_process"]["app_command_line"]
    assert summary["evidence"]["shutdown_exit_code"] is not None
    assert not Path(summary["install"]["path"]).exists()
    assert not evidence.parent.joinpath("xpst-stranger-install").exists()


@pytest.mark.skipif(os.name == "nt", reason="process-group teardown semantics differ on Windows")
def test_harness_passes_against_a_synthetic_tauri_published_artifact(tmp_path, capsys):
    """The full definition-of-done contract must be reachable: engine + HTML + sidecar."""
    artifact, checksums = build_fake_tauri_published_artifact(tmp_path)
    evidence = tmp_path / "evidence.json"

    code = main(
        [
            str(artifact),
            "--checksums",
            str(checksums),
            "--no-require-visible-window",
            "--evidence-out",
            str(evidence),
        ]
    )
    capsys.readouterr()
    summary = json.loads(evidence.read_text(encoding="utf-8"))

    assert code == 0, summary["failures"]
    assert summary["stack"]["name"] == "tauri"
    assert summary["checks"]["engine_health_200"] is True
    assert summary["checks"]["packaged_ui_served"] is True
    assert summary["checks"]["real_running_process"] is True
    assert summary["checks"]["zero_xpst_engine_processes"] is True
    assert summary["checks"]["uninstall"] is True
    assert summary["evidence"]["http_status"] == 200
    assert summary["evidence"]["ui"]["title"] == "xPST"
    assert summary["evidence"]["ui"]["attempts"] >= 1
    assert summary["evidence"]["running_process"]["engine_sidecar_processes"]
    assert summary["evidence"]["running_process"]["engine_sidecar_attempts"] >= 1
    assert summary["evidence"]["engine_processes_after_shutdown"] == []


@pytest.mark.skipif(os.name == "nt", reason="process-group teardown semantics differ on Windows")
def test_harness_retries_a_stale_first_ui_sample_and_records_the_attempts(tmp_path, capsys):
    """A first root fetch that misses must not fail the run (42508682981 shape)."""
    artifact, checksums = build_fake_stale_ui_published_artifact(tmp_path)
    evidence = tmp_path / "evidence.json"

    code = main(
        [
            str(artifact),
            "--checksums",
            str(checksums),
            "--no-require-visible-window",
            "--evidence-out",
            str(evidence),
        ]
    )
    capsys.readouterr()
    summary = json.loads(evidence.read_text(encoding="utf-8"))

    assert code == 0, summary["failures"]
    assert summary["checks"]["packaged_ui_served"] is True
    assert summary["evidence"]["ui"]["attempts"] == 2
    assert summary["evidence"]["ui"]["title"] == "xPST Dashboard"
    assert summary["evidence"]["ui"]["retry_window_seconds"] > 0


@pytest.mark.skipif(os.name == "nt", reason="process-group teardown semantics differ on Windows")
def test_harness_fails_when_the_published_checksum_does_not_match(tmp_path, capsys):
    artifact, _checksums = build_fake_published_artifact(tmp_path)
    bad = tmp_path / "bad-SHA256SUMS"
    bad.write_text(f"{SHA_B}  {artifact.name}\n", encoding="utf-8")
    evidence = tmp_path / "evidence.json"

    code = main([str(artifact), "--checksums", str(bad), "--evidence-out", str(evidence)])
    capsys.readouterr()
    summary = json.loads(evidence.read_text(encoding="utf-8"))

    assert code == 1
    assert summary["ok"] is False
    assert any("checksum mismatch" in failure for failure in summary["failures"])
    assert summary["checks"] == {}
    assert summary["evidence"]["artifact_name"] == artifact.name


def test_cross_platform_hint_explains_a_wrong_os_artifact():
    from scripts.e2e_install_from_artifact import cross_platform_hint, host_binary_kind

    assert cross_platform_hint("elf", "macho").startswith(" — this published artifact is elf")
    assert "macho" in cross_platform_hint("elf", "macho")
    assert cross_platform_hint("macho", "macho") == ""
    assert cross_platform_hint(None, "macho") == ""
    assert cross_platform_hint("elf", None) == ""
    assert host_binary_kind() in {None, "macho", "elf", "pe"}


def test_verify_checksum_record_verdict_source_for_a_normal_lookup(tmp_path):
    artifact = tmp_path / "xPST.dmg"
    artifact.write_bytes(b"payload")

    result = verify_checksum(artifact, f"{_sha256(artifact)}  xPST.dmg\n", "release:macos-SHA256SUMS")

    assert result["verdict_source"] == "checksum-file"


def test_verify_checksum_falls_back_to_the_release_asset_digest(tmp_path):
    """A published asset missing from the lane checksum file is still verifiable."""
    artifact = tmp_path / "xPST-macos-arm64.zip"
    artifact.write_bytes(b"payload")

    result = verify_checksum(
        artifact,
        f"{SHA_A}  xPST.dmg\n",
        "release:macos-SHA256SUMS",
        None,
        _sha256(artifact),
    )

    assert result["ok"] is True
    assert result["verdict_source"] == "release-api-digest"
    assert "release API" in result["checksum_source"]

    with pytest.raises(E2EError) as excinfo:
        verify_checksum(artifact, f"{SHA_A}  xPST.dmg\n", "release:macos-SHA256SUMS", None, SHA_B)
    assert "checksum mismatch" in str(excinfo.value)


def test_verify_checksum_still_fails_closed_without_a_release_digest(tmp_path):
    artifact = tmp_path / "xPST-macos-arm64.zip"
    artifact.write_bytes(b"payload")

    with pytest.raises(E2EError):
        verify_checksum(artifact, f"{SHA_A}  xPST.dmg\n", "release:macos-SHA256SUMS")


@pytest.mark.skipif(os.name == "nt", reason="POSIX ps behaviour; Windows uses tasklist")
def test_engine_processes_asks_ps_not_to_truncate_the_command(monkeypatch):
    """GNU ps truncates its command column, which hides a long sidecar path."""
    calls = []

    def fake_command_result(command, **kwargs):
        calls.append(command)
        if "-ww" not in command:
            return {"returncode": 0, "stdout": "  4242 /tmp/xpst-stranger-install-abcdef/install/xPST\n", "stderr": ""}
        return {
            "returncode": 0,
            "stdout": (
                "  4242 /tmp/xpst-stranger-install-abcdef/install/xPST.app/Contents/"
                "Resources/binaries/engine/xpst-engine\n  1 /sbin/launchd\n"
            ),
            "stderr": "",
        }

    monkeypatch.setattr("scripts.e2e_install_from_artifact.command_result", fake_command_result)

    found = engine_processes()

    assert [item["pid"] for item in found] == ["4242"]
    assert all("-ww" in command for command in calls)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the /proc fallback is Linux-only")
def test_engine_processes_falls_back_to_proc_when_ps_misses_the_sidecar(monkeypatch):
    monkeypatch.setattr(
        "scripts.e2e_install_from_artifact.command_result",
        lambda command, **kwargs: {"returncode": 0, "stdout": "  1 /sbin/init\n", "stderr": ""},
    )
    monkeypatch.setattr(
        "scripts.e2e_install_from_artifact.linux_proc_cmdlines",
        lambda: [
            ("7", "/usr/bin/python3 /tmp/xpst-stranger-install-a/install/xPST.app/Contents/"
                  "Resources/binaries/engine/xpst-engine"),
        ],
    )

    assert [item["pid"] for item in engine_processes()] == ["7"]


def test_linux_proc_cmdlines_is_empty_off_linux():
    from scripts.e2e_install_from_artifact import linux_proc_cmdlines

    if not sys.platform.startswith("linux"):
        assert linux_proc_cmdlines() == []


# ---------------------------------------------------------------------------
# The UI and sidecar assertions are bounded-retry, not single-sample.
#
# Run 42508682981's macOS leg failed with a healthy engine (/health 200 and
# "GET / 200 OK" in the launch log) because one root fetch and one ps sample
# were taken in the instant the shell was re-exec'ing. The identical commit
# passed on re-run. These tests pin both halves of the fix: a missed sample must
# be retried (and the attempt count recorded), the strict HTML test must not be
# weakened, and exhaustion of the documented window must still fail.
# ---------------------------------------------------------------------------

WAITING_ROOT = b'{"detail": "UI not mounted yet"}'
PACKAGED_ROOT = b"<html><head><title>xPST Dashboard</title></head><body>xPST</body></html>"


def _root_server(bodies: list[bytes]):
    """Serve successive root bodies (the last one repeats), loopback only."""
    import http.server
    import threading

    state = {"index": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler name
            index = min(state["index"], len(bodies) - 1)
            state["index"] += 1
            body = bodies[index]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/"


def test_packaged_ui_observation_retries_a_stale_root_and_records_attempts():
    """A first fetch that misses followed by a packaged-HTML fetch must pass."""
    server, url = _root_server([WAITING_ROOT, PACKAGED_ROOT])
    try:
        observation = observe_packaged_ui(url, window_seconds=5.0, poll_interval=0.01)
    finally:
        server.shutdown()
        server.server_close()

    assert observation["ok"] is True
    assert observation["attempts"] == 2
    assert observation["attempts_exhausted"] is False
    assert observation["title"] == "xPST Dashboard"
    assert observation["retry_window_seconds"] == 5.0


def test_packaged_ui_observation_fails_only_once_its_window_is_exhausted():
    server, url = _root_server([WAITING_ROOT])
    try:
        observation = observe_packaged_ui(url, window_seconds=0.4, poll_interval=0.05)
    finally:
        server.shutdown()
        server.server_close()

    assert observation["ok"] is False
    assert observation["attempts"] >= 2
    assert observation["attempts_exhausted"] is True
    assert observation["status"] == 200
    assert "retried" in observation["reason"]


@pytest.mark.parametrize(
    ("body", "expected_ok"),
    [
        (PACKAGED_ROOT, True),
        (b"<html><body>no product marker here</body></html>", False),
        (b"plain text, not html", False),
        (WAITING_ROOT, False),
    ],
)
def test_packaged_html_assertion_stays_strict(monkeypatch, body, expected_ok):
    """Retrying must not loosen the check: 200 + <html + an xPST marker."""
    monkeypatch.setattr(
        "scripts.e2e_install_from_artifact.http_get",
        lambda url, timeout=2.0: (200, body, None),
    )

    assert packaged_ui_sample("http://127.0.0.1:1/")["ok"] is expected_ok


def test_sidecar_observation_retries_a_ps_that_missed_the_sidecar():
    """A first ps that misses followed by one that sees the sidecar must pass."""
    engine = {
        "pid": "4242",
        "command": "/tmp/xpst-stranger-install-a/install/xPST.app/Contents/"
        "Resources/binaries/engine/xpst-engine",
    }
    calls = {"count": 0}

    def enumerator():
        calls["count"] += 1
        return [] if calls["count"] == 1 else [dict(engine)]

    observation = observe_engine_sidecar(
        window_seconds=5.0, poll_interval=0.01, enumerate_processes=enumerator
    )

    assert observation["observed"] is True
    assert observation["attempts"] == 2
    assert observation["attempts_exhausted"] is False
    assert observation["processes"] == [engine]


def test_sidecar_observation_fails_only_after_its_window_is_exhausted():
    ticks = {"now": 0.0}

    def clock():
        ticks["now"] += 1.0
        return ticks["now"]

    observation = observe_engine_sidecar(
        window_seconds=3.0,
        poll_interval=0.0,
        enumerate_processes=lambda: [],
        clock=clock,
        sleep=lambda _seconds: None,
    )

    assert observation["observed"] is False
    assert observation["attempts"] >= 2
    assert observation["attempts_exhausted"] is True
    assert observation["processes"] == []


def test_sidecar_observation_ignores_a_foreign_engine_that_was_already_running():
    foreign = {"pid": "111", "command": "/opt/other/install/xpst-engine"}
    ours = {"pid": "222", "command": "/tmp/install/xPST.app/Contents/binaries/engine/xpst-engine"}

    observation = observe_engine_sidecar(
        exclude_pids={"111"},
        window_seconds=1.0,
        poll_interval=0.0,
        enumerate_processes=lambda: [dict(foreign), dict(ours)],
        sleep=lambda _seconds: None,
    )

    assert observation["attempts"] == 1
    assert observation["processes"] == [ours]


# ---------------------------------------------------------------------------
# --require-published: the run can only ever test something a stranger can
# download. A local file is a hard failure; a URL run records that it was
# enforced.
# ---------------------------------------------------------------------------

def test_require_published_rejects_a_local_artifact(tmp_path, capsys):
    artifact, checksums = build_fake_published_artifact(tmp_path)

    code = main(
        [
            str(artifact),
            "--checksums",
            str(checksums),
            "--require-published",
            "--no-require-visible-window",
        ]
    )
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert code == 1
    assert summary["status"] == "failed"
    assert any("require-published" in failure for failure in summary["failures"])
    assert summary.get("install") is None


@pytest.mark.skipif(
    os.name == "nt",
    reason="the synthetic bundle is a macOS .app; Windows cannot launch it (WinError 193)",
)
def test_require_published_records_that_a_url_run_was_enforced(tmp_path, capsys):
    """--require-published must allow a URL run and record that it was enforced.

    The end-to-end launch is POSIX-only for the same reason the other synthetic
    tests are: the fake published artifact is a macOS bundle.
    """
    import http.server
    import threading

    artifact, checksums = build_fake_published_artifact(tmp_path)
    evidence = tmp_path / "evidence.json"

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler name
            payload = artifact.read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/{artifact.name}"
        code = main(
            [
                url,
                "--checksums",
                str(checksums),
                "--require-published",
                "--no-require-visible-window",
                "--evidence-out",
                str(evidence),
            ]
        )
    finally:
        server.shutdown()
        thread.join()

    summary = json.loads(evidence.read_text(encoding="utf-8"))
    assert code == 0, summary["failures"]
    assert summary["evidence"]["source_kind"] == "url"
    assert summary["evidence"]["published_required"] is True
    assert summary["evidence"]["artifact_sha256"] == _sha256(artifact)


# ---------------------------------------------------------------------------
# Evidence renderer used by the CI step summary
# ---------------------------------------------------------------------------

def test_evidence_summary_reports_a_missing_evidence_file(tmp_path):
    from scripts.e2e_evidence_summary import render

    assert "No evidence JSON was recorded" in render("macos", tmp_path / "absent.json")


def test_evidence_summary_renders_the_harness_evidence_block(tmp_path):
    from scripts.e2e_evidence_summary import render

    evidence = tmp_path / "macos.json"
    evidence.write_text(
        json.dumps(
            {
                "status": "failed",
                "evidence": {
                    "artifact_name": "xPST.dmg",
                    "artifact_bytes": 126490269,
                    "artifact_sha256": SHA_A,
                    "artifact_type": "dmg",
                    "release": {"tag": "v1.1.0", "id": 562855127, "created_at": "t"},
                    "source_kind": "url",
                    "published_required": True,
                    "http_status": None,
                    "health_ok": False,
                    "health_url": None,
                    "boot_ok": True,
                    "boot_to_visible_seconds": 3.71,
                    "window_assertion_required": True,
                    "running_process": {"app_pid": 42, "app_process_alive_after_boot": True},
                    "shutdown_exit_code": -15,
                    "engine_processes_after_shutdown": [],
                    "cleanup_ok": True,
                    "uninstall_ok": True,
                    "checks": {"engine_health_200": False},
                    "failures": ["engine /health did not return HTTP 200 within 60.0s"],
                },
            }
        ),
        encoding="utf-8",
    )
    markdown = render("macos", evidence)

    assert "verdict: **failed**" in markdown
    assert SHA_A in markdown
    assert "engine health ok: `False`" in markdown
    assert "engine /health did not return HTTP 200" in markdown
    assert "published required: `True`" in markdown


# ---------------------------------------------------------------------------
# CI wiring guard: the automated run must install a published artifact
# ---------------------------------------------------------------------------

def test_ci_workflow_install_tests_a_published_artifact():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "published-artifact-install-e2e.yml"
    ).read_text(encoding="utf-8")

    assert "e2e_install_from_artifact.py" in workflow
    assert "--release" in workflow
    assert "--require-published" in workflow
    assert "--evidence-out" in workflow
    assert "upload-artifact" in workflow


# ---------------------------------------------------------------------------
# Regression coverage for the v1.2.1 stranger-install run: the published Tauri
# release uses `xPST_<version>_<arch>.<ext>` asset names and ships a single
# aggregate SHA256SUMS, and a foreign engine may already be running.
# ---------------------------------------------------------------------------

V121_ASSETS = [
    {"name": "CHANGELOG.md", "browser_download_url": "https://example.invalid/changelog"},
    {"name": "SHA256SUMS", "browser_download_url": "https://example.invalid/sums"},
    {"name": "latest.json", "browser_download_url": "https://example.invalid/latest"},
    {"name": "xPST.app.tar.gz", "browser_download_url": "https://example.invalid/tar"},
    {"name": "xPST.app.tar.gz.sig", "browser_download_url": "https://example.invalid/sig"},
    {"name": "xPST_1.2.1_aarch64.dmg", "browser_download_url": "https://example.invalid/dmg"},
    {"name": "xPST_1.2.1_amd64.AppImage", "browser_download_url": "https://example.invalid/appimage"},
    {"name": "xPST_1.2.1_amd64.deb", "browser_download_url": "https://example.invalid/deb"},
    {"name": "xPST_1.2.1_x64-setup.exe", "browser_download_url": "https://example.invalid/exe"},
    {"name": "xPST_1.2.1_x64_en-US.msi", "browser_download_url": "https://example.invalid/msi"},
    {"name": "xpst-1.2.1-py3-none-any.whl", "browser_download_url": "https://example.invalid/whl"},
]


def test_installer_candidates_ignore_sidecars_and_signatures():
    names = [asset["name"] for asset in installer_candidates(V121_ASSETS, "macos")]

    assert names == ["xPST_1.2.1_aarch64.dmg"]
    assert all(not name.endswith(".sig") for name in names)
    assert all(".tar" not in name for name in names)


def test_select_release_asset_recognizes_the_tauri_installer_naming():
    """v1.2.1 publishes no xPST.dmg; the harness must still find the installer."""

    macos = select_release_asset(V121_ASSETS, "macos")["name"]
    windows = select_release_asset(V121_ASSETS, "windows")["name"]
    linux = select_release_asset(V121_ASSETS, "linux")["name"]

    assert macos.startswith("xPST_1.2.1_") and macos.endswith(".dmg")
    assert windows in {"xPST_1.2.1_x64-setup.exe", "xPST_1.2.1_x64_en-US.msi"}
    assert linux in {"xPST_1.2.1_amd64.AppImage", "xPST_1.2.1_amd64.deb"}


def test_select_release_asset_prefers_the_host_architecture():
    assets = [
        {"name": "xPST_1.2.1_x64_en-US.msi", "browser_download_url": "https://example.invalid/x"},
        {"name": "xPST_1.2.1_arm64_en-US.msi", "browser_download_url": "https://example.invalid/a"},
    ]

    chosen = select_release_asset(assets, "windows")["name"]

    assert architecture_match_score(chosen, host_arch()) > 0


def test_checksum_source_candidates_prefer_platform_then_aggregate():
    release_url = "https://github.com/TysAIs/xPST/releases/download/v1.2.1/xPST_1.2.1_aarch64.dmg"

    candidates = checksum_source_candidates(release_url, "xPST_1.2.1_aarch64.dmg", "macos", "x", None)

    assert candidates == [
        "https://github.com/TysAIs/xPST/releases/download/v1.2.1/macos-SHA256SUMS",
        "https://github.com/TysAIs/xPST/releases/download/v1.2.1/SHA256SUMS",
    ]
    local = checksum_source_candidates(
        "/tmp/xPST_1.2.1_aarch64.dmg", "xPST_1.2.1_aarch64.dmg", "macos", "TysAIs/xPST", "v1.2.1"
    )
    assert local[-1].endswith("/SHA256SUMS")


def test_load_first_checksum_source_falls_back_to_a_later_candidate(tmp_path):
    present = tmp_path / "SHA256SUMS"
    present.write_text(f"{SHA_A}  xPST_1.2.1_aarch64.dmg\n", encoding="utf-8")

    text, provenance = load_first_checksum_source(
        [str(tmp_path / "macos-SHA256SUMS"), str(present)],
        tmp_path / "downloaded-SHA256SUMS",
    )

    assert SHA_A in text
    assert provenance == str(present)


def test_load_first_checksum_source_fails_closed_when_none_exist(tmp_path):
    with pytest.raises(E2EError):
        load_first_checksum_source(
            [str(tmp_path / "a"), str(tmp_path / "b")], tmp_path / "out"
        )


def test_walk_app_bundles_does_not_follow_symlinks(tmp_path):
    root = tmp_path / "image"
    root.mkdir()
    (root / "xPST.app").mkdir()
    outside = tmp_path / "outside"
    (outside / "Other.app").mkdir(parents=True)
    (root / "Applications").symlink_to(outside, target_is_directory=True)

    found = [path.name for path in walk_app_bundles(root)]

    assert found == ["xPST.app"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group teardown semantics differ")
def test_foreign_engine_already_running_is_not_blamed_as_a_leftover(tmp_path, capsys, monkeypatch):
    """The developer's own running app must not fail the stranger-install run."""
    artifact, checksums = build_fake_published_artifact(tmp_path)
    evidence = tmp_path / "evidence.json"
    foreign = {
        "pid": "53252",
        "command": "/Users/dev/xPST/dist/xPST.app/Contents/Resources/binaries/engine/xpst-engine --port 65498",
    }
    monkeypatch.setattr(
        "scripts.e2e_install_from_artifact.engine_processes", lambda: [dict(foreign)]
    )

    code = main(
        [
            str(artifact),
            "--checksums",
            str(checksums),
            "--no-require-visible-window",
            "--evidence-out",
            str(evidence),
        ]
    )
    capsys.readouterr()
    summary = json.loads(evidence.read_text(encoding="utf-8"))

    assert code == 0, summary["failures"]
    assert summary["checks"]["zero_xpst_engine_processes"] is True
    assert summary["evidence"]["engine_processes_after_shutdown"] == []
    assert summary["evidence"]["preexisting_engine_processes"] == [foreign]
    assert any("already running before this run" in finding for finding in summary["findings"])


# ---------------------------------------------------------------------------
# API-free release resolution: a stranger has no GITHUB_TOKEN and the
# unauthenticated API limit is 60 requests/hour, which the harness must survive.
# ---------------------------------------------------------------------------

EXPANDED_ASSETS_HTML = """
<div>
  <a href="/TysAIs/xPST/releases/download/v1.2.1/SHA256SUMS">SHA256SUMS</a>
  <a href="/TysAIs/xPST/releases/download/v1.2.1/xPST.app.tar.gz">xPST.app.tar.gz</a>
  <a href="/TysAIs/xPST/releases/download/v1.2.1/xPST_1.2.1_aarch64.dmg">xPST_1.2.1_aarch64.dmg</a>
</div>
"""


def test_release_assets_from_html_parses_the_public_asset_page(monkeypatch):
    monkeypatch.setattr(
        "scripts.e2e_install_from_artifact.http_get_text", lambda url, **kwargs: EXPANDED_ASSETS_HTML
    )

    assets = release_assets_from_html("TysAIs/xPST", "v1.2.1")

    assert [asset["name"] for asset in assets] == [
        "SHA256SUMS",
        "xPST.app.tar.gz",
        "xPST_1.2.1_aarch64.dmg",
    ]
    assert assets[-1]["browser_download_url"] == (
        "https://github.com/TysAIs/xPST/releases/download/v1.2.1/xPST_1.2.1_aarch64.dmg"
    )


def test_release_assets_falls_back_to_html_when_the_api_is_rate_limited(monkeypatch):
    def rate_limited(url):
        raise E2EError(f"GitHub API returned HTTP 403 for {url}: API rate limit exceeded")

    monkeypatch.setattr("scripts.e2e_install_from_artifact.github_api_json", rate_limited)
    monkeypatch.setattr(
        "scripts.e2e_install_from_artifact.http_get_text", lambda url, **kwargs: EXPANDED_ASSETS_HTML
    )

    assets = release_assets("TysAIs/xPST", "v1.2.1")

    assert select_release_asset(assets, "macos")["name"] == "xPST_1.2.1_aarch64.dmg"


def test_default_work_dir_is_canonical_and_removable():
    """The app must be launched via a canonical path (no symlinked component)."""

    work = WorkDir(None)
    try:
        assert work.path.exists()
        assert work.path == work.path.resolve()
        if sys.platform == "darwin":
            assert DEFAULT_WORK_BASE == "/private/tmp"
            assert str(work.path).startswith("/private/tmp/")
    finally:
        assert work.cleanup(False) is True
    assert not work.path.exists()


# ---------------------------------------------------------------------------
# Regression coverage for the v1.2.1 published-artifact red run: the Linux
# artifact is a GTK desktop bundle that needs an installer and a display, the
# Windows artifact is an NSIS installer that has to be installed before there
# is an app at all, and an app that dies instantly must report its exit code
# instead of "no /health in 90s".
# ---------------------------------------------------------------------------

CRASH_APP_SOURCE = '''#!/usr/bin/env python3
"""Stand-in for a packaged app that cannot start: fails like a missing .so."""
import sys

print(
    "xPST: error while loading shared libraries: libfontconfig.so.1: "
    "cannot open shared object file: No such file or directory",
    file=sys.stderr,
    flush=True,
)
sys.exit(127)
'''


def _tar_bytes(members):
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, payload, mode in members:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = mode
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _ar_bytes(members) -> bytes:
    body = b"!<arch>\n"
    for name, payload in members:
        header = (
            f"{name.decode():<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(payload):<10}`\n".encode()
        )
        assert len(header) == 60
        body += header + payload + (b"\n" if len(payload) % 2 else b"")
    return body


def build_fake_deb(path: Path, *, executable: str = "usr/bin/xPST") -> Path:
    """Build a minimal but real .deb: an ar archive of a control and data tar."""
    control = (
        b"Package: x-pst\nVersion: 1.2.1\nArchitecture: amd64\nMaintainer: x\n"
        b"Depends: libwebkit2gtk-4.1-0, libgtk-3-0\nDescription: xPST\n"
    )
    control_tar = _tar_bytes([("./control", control, 0o644)])
    data_tar = _tar_bytes([(f"./{executable}", b"#!/bin/sh\ntrue\n", 0o755)])
    path.write_bytes(
        _ar_bytes(
            [
                (b"debian-binary", b"2.0\n"),
                (b"control.tar.gz", control_tar),
                (b"data.tar.gz", data_tar),
            ]
        )
    )
    return path


def test_deb_install_record_reads_the_published_package(tmp_path):
    """The .deb is the Linux artifact a stranger installs, so read it for real."""

    deb = build_fake_deb(tmp_path / "xPST_1.2.1_amd64.deb")

    record = deb_install_record(deb)

    assert record["package"] == "x-pst"
    assert record["version"] == "1.2.1"
    assert record["executable"] == "/usr/bin/xPST"
    assert record["depends"] == ["libwebkit2gtk-4.1-0", "libgtk-3-0"]


def test_deb_install_record_rejects_a_file_that_is_not_an_ar_archive(tmp_path):
    not_a_deb = tmp_path / "xPST.deb"
    not_a_deb.write_bytes(b"MZ not an ar archive")

    with pytest.raises(E2EError, match="not an ar archive"):
        deb_install_record(not_a_deb)


def test_install_deb_extracts_when_apt_cannot_install_it(tmp_path, monkeypatch):
    """Without apt the payload is still unpacked, and the skip is disclosed."""

    deb = build_fake_deb(tmp_path / "xPST_1.2.1_amd64.deb")
    work = tmp_path / "work"
    work.mkdir()

    def fake_which(name):
        return None if name == "apt-get" else f"/usr/bin/{name}"

    def fake_command_result(command, **_kwargs):
        if command[0] == "dpkg-deb":
            root = Path(command[3])
            exe = root / "usr" / "bin" / "xPST"
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_text("#!/bin/sh\ntrue\n", encoding="utf-8")
        return {"returncode": 0, "stdout": "", "stderr": "", "command": command}

    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(e2e_install_module, "command_result", fake_command_result)

    installed, record = install_deb(deb, work)

    assert record["method"] == "dpkg-deb -x extraction"
    assert installed == work / "install" / "deb-root" / "usr" / "bin" / "xPST"
    assert installed.exists()
    assert record["uninstall"] == {"kind": "dir", "path": str(work / "install" / "deb-root")}


def test_install_artifact_routes_a_deb_and_an_msi_to_their_installers(tmp_path, monkeypatch):
    deb = build_fake_deb(tmp_path / "xPST_1.2.1_amd64.deb")
    msi = tmp_path / "xPST_1.2.1_x64_en-US.msi"
    msi.write_bytes(b"\xd0\xcf\x11\xe0")
    calls = []

    def fake_deb(path, work):
        calls.append(("deb", path.name))
        return Path("/usr/bin/xPST"), {"method": "apt-get install"}

    def fake_windows(path, work):
        calls.append(("windows", path.name))
        return tmp_path / "app" / "xPST.exe", {"method": "silent NSIS install (/S)"}

    monkeypatch.setattr(e2e_install_module, "install_deb", fake_deb)
    monkeypatch.setattr(e2e_install_module, "install_windows_installer", fake_windows)

    install_artifact(deb, "deb", tmp_path)
    install_artifact(msi, "msi", tmp_path)

    assert calls == [("deb", "xPST_1.2.1_amd64.deb"), ("windows", "xPST_1.2.1_x64_en-US.msi")]


def test_artifact_type_recognizes_the_msi_and_deb_installers(tmp_path):
    msi = tmp_path / "xPST_1.2.1_x64_en-US.msi"
    msi.write_bytes(b"\xd0\xcf\x11\xe0")
    deb = tmp_path / "xPST_1.2.1_amd64.deb"
    deb.write_bytes(b"!<arch>\n")

    assert artifact_type(msi.name, msi) == "msi"
    assert artifact_type(deb.name, deb) == "deb"


def test_select_release_asset_prefers_the_installable_package():
    """A .deb installs; the AppImage is a portable copy that needs FUSE + GTK."""

    assert select_release_asset(V121_ASSETS, "linux")["name"] == "xPST_1.2.1_amd64.deb"
    assert select_release_asset(V121_ASSETS, "windows")["name"] == "xPST_1.2.1_x64-setup.exe"
    assert asset_preference_score("xPST_1.2.1_amd64.deb", "linux") > asset_preference_score(
        "xPST_1.2.1_amd64.AppImage", "linux"
    )


def test_is_nsis_installer_recognizes_the_nullsoft_header(tmp_path):
    setup = tmp_path / "xPST_1.2.1_x64-setup.exe"
    setup.write_bytes(b"MZ" + b"\x00" * 512 + b"NullsoftInst" + b"\x00" * 64)
    portable = tmp_path / "portable.exe"
    portable.write_bytes(b"MZ" + b"\x00" * 512)

    assert is_nsis_installer(setup) is True
    assert is_nsis_installer(portable) is False


def test_product_executable_prefers_the_app_over_the_uninstaller(tmp_path):
    """After a silent install the product .exe is the app, not uninstall.exe."""

    (tmp_path / "xPST.exe").write_bytes(b"MZ" + b"\x00" * 16)
    (tmp_path / "uninstall.exe").write_bytes(b"MZ" + b"\x00" * 4096)

    chosen = product_executable(tmp_path)

    assert chosen is not None
    assert chosen.name == "xPST.exe"


def test_headless_display_prefix_only_fires_on_linux_without_a_display(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/xvfb-run" if name == "xvfb-run" else None
    )

    assert headless_display_prefix() == ["/usr/bin/xvfb-run", "-a", "--"]

    monkeypatch.setenv("DISPLAY", ":99")

    assert headless_display_prefix() == []


def test_headless_display_prefix_is_a_no_op_when_xvfb_is_absent(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    assert headless_display_prefix() == []


@pytest.mark.skipif(
    os.name == "nt",
    reason="the synthetic app is a shebang script; Windows cannot exec it (WinError 193)",
)
def test_a_crashing_artifact_reports_its_exit_code_instead_of_a_timeout(tmp_path, capsys):
    """The v1.2.1 linux/windows red run: the app died, not the health poll."""

    payload = tmp_path / "payload-crash"
    app = payload / "xPST.app"
    _write_script(app / "Contents" / "MacOS" / "xPST", CRASH_APP_SOURCE)
    _bundle_info_plist(app)
    artifact = tmp_path / "xPST_1.2.1_amd64.zip"
    _zip_dir(payload, artifact)
    checksums = tmp_path / "SHA256SUMS"
    checksums.write_text(f"{_sha256(artifact)}  {artifact.name}\n", encoding="utf-8")
    evidence = tmp_path / "evidence.json"

    code = main(
        [
            str(artifact),
            "--platform",
            "linux",
            "--checksums",
            str(checksums),
            "--no-require-visible-window",
            "--health-timeout-seconds",
            "5",
            "--evidence-out",
            str(evidence),
        ]
    )
    capsys.readouterr()
    summary = json.loads(evidence.read_text(encoding="utf-8"))
    joined = " ".join(summary["failures"])

    assert code == 1
    assert summary["status"] == "failed"
    assert "exited with code 127" in joined
    assert "libfontconfig" in joined
    assert "did not return HTTP 200 within" not in joined
    assert summary["launch"]["exit_code_before_boot"] == 127
    assert summary["launch"]["command"]


def test_ci_workflow_gives_the_linux_gui_bundle_a_display():
    """A headless runner cannot start a GTK app; the workflow must say so."""

    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "published-artifact-install-e2e.yml"
    ).read_text(encoding="utf-8")

    assert "apt-get install -y -qq xvfb" in workflow
    assert "matrix.platform == 'linux'" in workflow


# ---------------------------------------------------------------------------
# CI matrix guard: the portable Linux download must be booted, not just the .deb
# ---------------------------------------------------------------------------

def _e2e_workflow() -> dict:
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "published-artifact-install-e2e.yml"
    ).read_text(encoding="utf-8")
    return yaml.safe_load(workflow)


def _e2e_workflow_job() -> dict:
    return _e2e_workflow()["jobs"]["install-published-artifact"]


def _e2e_workflow_steps() -> list[dict]:
    return _e2e_workflow_job()["steps"]


def _step(needle: str) -> dict:
    for step in _e2e_workflow_steps():
        if needle in str(step.get("name", "")):
            return step
    raise AssertionError(f"no workflow step named like {needle!r}")


def test_ci_workflow_covers_every_published_installer_leg():
    """Four legs: the .deb install path AND the portable AppImage a user may take."""

    legs = {
        str(entry["leg"]): entry for entry in _e2e_workflow_job()["strategy"]["matrix"]["include"]
    }

    assert set(legs) == {"macos", "linux-deb", "linux-appimage", "windows"}
    assert legs["linux-deb"]["platform"] == "linux"
    assert legs["linux-appimage"]["platform"] == "linux"
    assert legs["linux-appimage"]["asset_suffix"] == "_amd64.AppImage"
    # The deb leg deliberately has no asset name: the ranker prefers the
    # installable package, and that preference is what a stranger gets.
    assert "asset_suffix" not in legs["linux-deb"]
    assert legs["linux-deb"]["os"] == legs["linux-appimage"]["os"]


def test_ci_workflow_appimage_leg_installs_the_portable_runtime():
    """The AppImage links the distro GTK/WebKit stack and mounts via FUSE."""

    step = _step("AppImage runtime")
    script = str(step["run"])

    assert step["if"] == "matrix.leg == 'linux-appimage'"
    for package in (
        "libfuse2t64",
        "libfuse2",
        "libgtk-3-0t64",
        "libwebkit2gtk-4.1-0",
        "libfontconfig1",
        "libxcb1",
        "libxkbcommon-x11-0",
    ):
        assert package in script, package
    # A runner without /dev/fuse still runs the artifact, by self-extraction.
    assert "APPIMAGE_EXTRACT_AND_RUN=1" in "\n".join(
        str(step.get("run", "")) for step in _e2e_workflow_steps()
    )
    assert "/dev/fuse" in "\n".join(str(step.get("run", "")) for step in _e2e_workflow_steps())


def test_ci_workflow_appimage_leg_names_its_own_asset():
    """The ranker prefers the .deb, so the portable leg must name the AppImage."""

    run = str(_step("Install a published artifact")["run"])

    assert "--asset-name" in run
    assert "xPST_${{ steps.tag.outputs.version }}${{ matrix.asset_suffix }}" in run
    # v1.2.1's real asset name is what that expression produces.
    version = "v1.2.1"[1:]
    assert f"xPST_{version}_amd64.AppImage" == "xPST_1.2.1_amd64.AppImage"
    assert (
        select_release_asset(V121_ASSETS, "linux", f"xPST_{version}_amd64.AppImage")["name"]
        == "xPST_1.2.1_amd64.AppImage"
    )


def test_ci_workflow_keeps_one_evidence_file_per_leg():
    """Two Linux legs on one image must not overwrite each other's evidence."""

    run = str(_step("Install a published artifact")["run"])
    upload = _step("Attach the machine-readable evidence")["with"]

    assert '--evidence-out "evidence/${{ matrix.leg }}.json"' in run
    assert upload["name"] == "published-artifact-install-evidence-${{ matrix.leg }}"
    assert _step("Summarise the evidence")["run"].strip().endswith(
        'e2e_evidence_summary.py "${{ matrix.leg }}" >> "$GITHUB_STEP_SUMMARY"'
    )


# ---------------------------------------------------------------------------
# Teardown: a portable AppImage leaves processes that hold the work directory
# ---------------------------------------------------------------------------

def test_stop_work_processes_kills_only_what_this_run_started(tmp_path):
    """The AppImage shell and its WebKit helpers escape the process-group kill."""

    if os.name == "nt":
        pytest.skip("command lines are read from ps/proc on posix only")

    work = tmp_path / "xpst-stranger-install-under-test"
    work.mkdir()
    # A stand-in for the self-extracted AppImage shell: its command line carries
    # the throwaway path, exactly like the real one.
    mine = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", "import time; time.sleep(30)", str(work)]
    )
    other = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    try:
        stopped = stop_work_processes(work, settle_seconds=5.0)

        assert str(mine.pid) in {entry["pid"] for entry in stopped}
        assert str(other.pid) not in {entry["pid"] for entry in stopped}
        mine.wait(timeout=10)
        assert mine.poll() is not None
        assert other.poll() is None
    finally:
        for process in (mine, other):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


def test_work_dir_cleanup_retries_a_transient_delete_failure(tmp_path, monkeypatch):
    """A WebKit helper writing into the tree must not turn teardown red."""

    work = WorkDir(str(tmp_path / "xpst-stranger-install-retry"))
    real_rmtree = shutil.rmtree
    calls: list[Path] = []

    def flaky_rmtree(path, ignore_errors=False):
        calls.append(Path(path))
        if len(calls) < 3:  # the first two deletes lost the race and removed nothing
            return
        real_rmtree(path, ignore_errors=ignore_errors)

    monkeypatch.setattr(shutil, "rmtree", flaky_rmtree)

    assert work.cleanup(False) is True
    assert len(calls) == 3
    assert not work.path.exists()
