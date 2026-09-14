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
import stat
import sys
import zipfile
from pathlib import Path

import pytest

from scripts.e2e_install_from_artifact import (
    E2EError,
    artifact_type,
    default_platform,
    derive_checksum_source,
    emit_summary,
    engine_processes,
    gatekeeper_report,
    local_urls_from_log,
    main,
    parse_checksums,
    platform_key,
    safe_extract_zip,
    select_release_asset,
    sniff_artifact_kind,
    uninstall_artifact,
    verify_checksum,
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
    assert summary["evidence"]["running_process"]["engine_sidecar_processes"]
    assert summary["evidence"]["engine_processes_after_shutdown"] == []


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


def test_require_published_records_that_a_url_run_was_enforced(tmp_path, capsys):
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
