"""Tests for the clean-profile published-artifact E2E harness.

These tests never touch the network, the keychain, or a real desktop app; they
exercise the classifier, checksum, evidence and health-probe logic that the
release gate depends on.
"""

import json
import plistlib
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.e2e_install_from_artifact import (
    E2EError,
    artifact_type,
    choose_release_asset,
    derive_checksum_source,
    gatekeeper_report,
    gui_session_available,
    health_and_ui,
    parse_args,
    parse_checksums,
    platform_asset_score,
    platform_key,
    resolve_release_artifact,
    safe_extract_zip,
    sniff_artifact_kind,
    verify_checksum,
    write_evidence,
)

# --------------------------------------------------------------------------
# Published-release asset selection
# --------------------------------------------------------------------------

PUBLISHED_ASSETS = [
    {"name": "CHANGELOG.md", "size": 6734, "id": 1, "url": "u", "created_at": "t"},
    {"name": "macos-SHA256SUMS", "size": 253, "id": 2, "url": "u", "created_at": "t"},
    {"name": "xPST", "size": 254540568, "id": 3, "url": "u", "created_at": "t"},
    {"name": "xPST.dmg", "size": 126490269, "id": 4, "url": "u", "created_at": "t"},
    {"name": "xPST.exe", "size": 232834801, "id": 5, "url": "u", "created_at": "t"},
    {"name": "xpst-1.1.0-py3-none-any.whl", "size": 2096622, "id": 6, "url": "u", "created_at": "t"},
]


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("macos", "xPST.dmg"), ("windows", "xPST.exe"), ("linux", "xPST")],
)
def test_choose_release_asset_picks_the_installable_published_asset(platform, expected):
    assert choose_release_asset(PUBLISHED_ASSETS, platform)["name"] == expected


def test_choose_release_asset_ignores_checksums_docs_and_wheels():
    for asset in PUBLISHED_ASSETS:
        if asset["name"] in {"xPST", "xPST.dmg", "xPST.exe"}:
            continue
        assert platform_asset_score(asset["name"], "macos") == 0
        assert platform_asset_score(asset["name"], "linux") == 0


def test_choose_release_asset_fails_closed_when_nothing_matches():
    with pytest.raises(E2EError):
        choose_release_asset([{"name": "CHANGELOG.md", "size": 1, "id": 1, "url": "u", "created_at": "t"}], "macos")


def test_resolve_release_artifact_honours_explicit_asset_and_records_provenance():
    with patch(
        "scripts.e2e_install_from_artifact.release_asset_candidates",
        return_value=[dict(asset, url=f"https://example.test/{asset['name']}") for asset in PUBLISHED_ASSETS],
    ):
        resolved = resolve_release_artifact("TysAIs/xPST", "v1.1.0", "macos", "xPST")
    assert resolved["name"] == "xPST"
    assert resolved["asset_count"] == len(PUBLISHED_ASSETS)
    assert resolved["tag"] == "v1.1.0"
    assert resolved["url"].endswith("/xPST")


def test_resolve_release_artifact_rejects_an_unpublished_name():
    with patch(
        "scripts.e2e_install_from_artifact.release_asset_candidates",
        return_value=[dict(asset, url="u") for asset in PUBLISHED_ASSETS],
    ):
        with pytest.raises(E2EError, match="not a published asset"):
            resolve_release_artifact("TysAIs/xPST", "v1.1.0", "macos", "xPST-v999.dmg")


# --------------------------------------------------------------------------
# Magic-byte classification of published assets
# --------------------------------------------------------------------------

def _write(path: Path, head: bytes, tail: bytes = b"", size: int = 2048) -> Path:
    path.write_bytes(head + b"\x00" * max(0, size - len(head) - len(tail)) + tail)
    return path


def test_sniff_artifact_kind_identifies_extensionless_published_binaries(tmp_path):
    assert sniff_artifact_kind(_write(tmp_path / "xPST", b"\x7fELF\x02\x01\x01\x00")) == "binary"
    assert sniff_artifact_kind(_write(tmp_path / "xPST.exe", b"MZ\x90\x00")) == "exe"
    assert sniff_artifact_kind(_write(tmp_path / "app.zip", b"PK\x03\x04")) == "zip"
    # A UDIF image ends with a 512-byte trailer that begins with 'koly'.
    dmg = tmp_path / "xPST.dmg"
    dmg.write_bytes(b"\x00" * 1536 + b"koly" + b"\x00" * 508)
    assert sniff_artifact_kind(dmg) == "dmg"


def test_artifact_type_sniffs_extensionless_asset_but_rejects_unknown_data(tmp_path):
    elf = _write(tmp_path / "xPST", b"\x7fELF\x02\x01\x01\x00")
    assert artifact_type("xPST", elf) == "binary"
    unknown = tmp_path / "random"
    unknown.write_bytes(b"not an artifact at all")
    with pytest.raises(E2EError):
        artifact_type("random", unknown)


def test_artifact_type_keeps_name_based_detection_without_a_path():
    assert artifact_type("xPST.dmg") == "dmg"
    assert artifact_type("xPST.exe") == "exe"
    assert artifact_type("xPST.zip") == "zip"


# --------------------------------------------------------------------------
# Checksum handling
# --------------------------------------------------------------------------

def test_parse_checksums_reads_sha256sum_and_shasum_forms():
    text = (
        "a03e6bb2a3f8f8e744597c619cfe4ee705885bd46bfac5af049407ff8c805ea2  xPST.dmg\n"
        "da40b393246cc98f5d3fbcf632f5d924f00a0ccef2f721a11490110a4b691c75 *xpst-1.1.0-py3-none-any.whl\n"
        "SHA256(xpst-1.1.0.tar.gz)= 826405794c095ce35b0b4d2f9e2929360dc7ee217a756a8169fe6bcafa7b0242\n"
    )
    entries = parse_checksums(text)
    assert entries["xPST.dmg"].startswith("a03e6bb2")
    assert len(entries) == 3


def test_verify_checksum_accepts_matching_bytes_and_rejects_a_mismatch(tmp_path):
    artifact = tmp_path / "xPST.dmg"
    artifact.write_bytes(b"desktop-image")
    import hashlib

    digest = hashlib.sha256(b"desktop-image").hexdigest()
    result = verify_checksum(artifact, f"{digest}  xPST.dmg\n", "release-SHA256SUMS")
    assert result["ok"] is True
    with pytest.raises(E2EError, match="checksum mismatch"):
        verify_checksum(artifact, f"{'0' * 64}  xPST.dmg\n", "release-SHA256SUMS")


def test_verify_checksum_fails_closed_when_asset_is_not_listed(tmp_path):
    artifact = tmp_path / "xPST.dmg"
    artifact.write_bytes(b"x")
    with pytest.raises(E2EError, match="is not listed"):
        verify_checksum(artifact, f"{'0' * 64}  something.zip\n", "release-SHA256SUMS")
    # Two same-suffix entries are ambiguous, so a renamed file must be declared
    # explicitly rather than guessed.
    ambiguous = f"{'0' * 64}  a.dmg\n{'1' * 64}  b.dmg\n"
    with pytest.raises(E2EError, match="is not listed"):
        verify_checksum(artifact, ambiguous, "release-SHA256SUMS")


def test_verify_checksum_accepts_a_single_same_suffix_renamed_asset(tmp_path):
    import hashlib

    artifact = tmp_path / "downloaded.dmg"
    artifact.write_bytes(b"desktop-image")
    digest = hashlib.sha256(b"desktop-image").hexdigest()
    result = verify_checksum(artifact, f"{digest}  xPST.dmg\n", "release-SHA256SUMS")
    assert result["ok"] is True and result["release_asset"] == "xPST.dmg"


def test_derive_checksum_source_from_release_url_and_local_tag():
    assert (
        derive_checksum_source(
            "https://github.com/TysAIs/xPST/releases/download/v1.1.0/xPST.dmg", "xPST.dmg", "macos", "TysAIs/xPST", None
        )
        == "https://github.com/TysAIs/xPST/releases/download/v1.1.0/macos-SHA256SUMS"
    )
    assert (
        derive_checksum_source("/tmp/xPST.dmg", "xPST.dmg", "macos", "TysAIs/xPST", "v1.1.0")
        == "https://github.com/TysAIs/xPST/releases/download/v1.1.0/macos-SHA256SUMS"
    )
    with pytest.raises(E2EError):
        derive_checksum_source("/tmp/xPST.dmg", "xPST.dmg", "macos", "TysAIs/xPST", None)


def test_platform_key_infers_and_accepts_explicit_platform():
    assert platform_key("xPST.dmg", None) == "macos"
    assert platform_key("xPST.exe", None) == "windows"
    assert platform_key("xpst.AppImage", None) == "linux"
    assert platform_key("xPST", "linux") == "linux"
    with pytest.raises(E2EError):
        platform_key("xpst.bin", None)


# --------------------------------------------------------------------------
# Install safety
# --------------------------------------------------------------------------

def test_safe_extract_zip_rejects_path_traversal(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../escaped.txt", "nope")
    with pytest.raises(E2EError, match="escapes install root"):
        safe_extract_zip(archive, tmp_path / "out")


def test_safe_extract_zip_extracts_a_normal_bundle(tmp_path):
    archive = tmp_path / "app.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("xPST.app/Contents/Info.plist", plistlib.dumps({"CFBundleExecutable": "xPST"}))
    destination = tmp_path / "out"
    safe_extract_zip(archive, destination)
    assert (destination / "xPST.app" / "Contents" / "Info.plist").is_file()


# --------------------------------------------------------------------------
# Health / UI probe against a real loopback server
# --------------------------------------------------------------------------

class _HealthHandler(BaseHTTPRequestHandler):
    health_status = 200
    body = b"<html><head><title>xPST Dashboard</title></head><body>xPST</body></html>"

    def do_GET(self):  # noqa: N802 - stdlib handler name
        if self.path.startswith("/health"):
            self.send_response(self.health_status)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}' if self.health_status == 200 else b"nope")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):  # silence the test server
        return


class _FakeProcess:
    def __init__(self, pid=4242):
        self.pid = pid

    def poll(self):
        return None


def _serve(status=200, body=None):
    handler = type("H", (_HealthHandler,), {"health_status": status})
    if body is not None:
        handler.body = body
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return server, thread, port


def test_health_and_ui_asserts_health_200_and_packaged_html(tmp_path):
    server, thread, port = _serve()
    log = tmp_path / "launch.log"
    log.write_text(f"engine booting at http://127.0.0.1:{port}/\n", encoding="utf-8")
    try:
        boot, health, ui, failures = health_and_ui(log, _FakeProcess(), __import__("time").monotonic(), 2.0, 15.0, None, False)
    finally:
        server.shutdown()
        thread.join()
    assert health["ok"] is True and health["status"] == 200
    assert ui["ok"] is True and ui["title"] == "xPST Dashboard"
    assert boot["ok"] is True and boot["visible_required"] is False
    assert failures == []


def test_health_and_ui_fails_when_health_is_not_200(tmp_path):
    server, thread, port = _serve(status=500)
    log = tmp_path / "launch.log"
    log.write_text(f"engine booting at http://127.0.0.1:{port}/\n", encoding="utf-8")
    try:
        _boot, health, ui, failures = health_and_ui(log, _FakeProcess(), __import__("time").monotonic(), 2.0, 15.0, None, False)
    finally:
        server.shutdown()
        thread.join()
    assert health["ok"] is False and health["status"] == 500
    assert ui["ok"] is False
    assert any("engine /health did not return HTTP 200" in failure for failure in failures)


def test_health_and_ui_reports_no_url_when_the_log_is_silent(tmp_path):
    log = tmp_path / "launch.log"
    log.write_text("nothing useful here\n", encoding="utf-8")
    _boot, health, _ui, failures = health_and_ui(log, _FakeProcess(), __import__("time").monotonic(), 0.3, 15.0, None, False)
    assert health["status"] is None
    assert any("urls=none" in failure for failure in failures)


# --------------------------------------------------------------------------
# Gatekeeper reporting
# --------------------------------------------------------------------------

def test_gatekeeper_report_flags_ad_hoc_unsigned_builds():
    signature = {"checked": True, "gatekeeper_accepted": False, "ad_hoc": True}
    report = gatekeeper_report(signature, "0081;abc", True)
    assert report["applicable"] is True
    assert report["ad_hoc_signature_only"] is True
    assert report["developer_id_signed"] is False
    assert report["notarization_proven"] is False
    assert report["blocked_launch_detected"] is False  # the process did launch
    assert "Open Anyway" in report["remediation"]


def test_gatekeeper_report_is_not_applicable_off_macos():
    assert gatekeeper_report({"checked": False}, None, None)["applicable"] is False


# --------------------------------------------------------------------------
# Evidence + CLI surface
# --------------------------------------------------------------------------

def test_write_evidence_produces_machine_readable_json(tmp_path):
    path = tmp_path / "nested" / "evidence.json"
    write_evidence(path, {"ok": False, "http_status": None, "artifact": {"sha256": "abc"}})
    assert json.loads(path.read_text(encoding="utf-8"))["artifact"]["sha256"] == "abc"


def test_gui_session_available_returns_a_bool():
    assert isinstance(gui_session_available(), bool)


def test_parse_args_requires_an_artifact_or_a_release():
    with pytest.raises(SystemExit):
        parse_args([])
    args = parse_args(["--release", "v1.1.0", "--require-published"])
    assert args.release == "v1.1.0" and args.require_published is True
    assert args.require_visible == "auto"


def test_local_artifact_with_require_published_fails_fast(tmp_path):
    from scripts.e2e_install_from_artifact import main

    local = tmp_path / "xPST.dmg"
    local.write_bytes(b"koly" + b"\x00" * 600)
    exit_code = main([str(local), "--require-published"])
    assert exit_code == 1


def test_loopback_regex_ignores_non_loopback_urls(tmp_path):
    from scripts.e2e_install_from_artifact import local_urls_from_log

    assert local_urls_from_log("see https://example.com/x and http://127.0.0.1:1234/health") == [
        "http://127.0.0.1:1234/"
    ]
    assert local_urls_from_log("engine port: 5050") == ["http://127.0.0.1:5050/"]


# --------------------------------------------------------------------------
# Evidence summary renderer
# --------------------------------------------------------------------------

def test_evidence_summary_reports_a_missing_evidence_file(tmp_path):
    from scripts.e2e_evidence_summary import render

    markdown = render("macos", tmp_path / "absent.json")
    assert "No evidence JSON was recorded" in markdown


def test_evidence_summary_renders_a_truthful_failure(tmp_path):
    from scripts.e2e_evidence_summary import render

    evidence = tmp_path / "macos.json"
    evidence.write_text(
        json.dumps(
            {
                "status": "failed",
                "artifact": {"name": "xPST.dmg", "bytes": 126490269, "type": "dmg", "sha256": "a03e6bb2"},
                "published": {"tag": "v1.1.0", "asset_id": 562855127, "asset_created_at": "t"},
                "http_status": None,
                "health": {"ok": False, "status": None},
                "process": {"pid": 1, "alive_at_health_probe": True},
                "checks": {"engine_health_200": False},
                "failures": ["engine /health did not return HTTP 200 within 60.0s"],
            }
        ),
        encoding="utf-8",
    )
    markdown = render("macos", evidence)
    assert "verdict: **failed**" in markdown
    assert "a03e6bb2" in markdown
    assert "engine /health did not return HTTP 200" in markdown


# --------------------------------------------------------------------------
# Regression guards for the release lane + CI wiring
# --------------------------------------------------------------------------

def test_media_binary_fetch_retries_partial_downloads():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "fetch-media-binaries.sh").read_text(encoding="utf-8")
    # curl exit 18 ("Transferred a partial file") fails the Tauri lane unless
    # --retry-all-errors is set; --retry alone does not cover it.
    assert "--retry-all-errors" in script


def test_post_release_workflow_installs_a_published_artifact():
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "published-artifact-install-e2e.yml"
    ).read_text(encoding="utf-8")
    assert "--release" in workflow
    assert "--require-published" in workflow
    assert "e2e_install_from_artifact.py" in workflow
    assert "upload-artifact" in workflow
