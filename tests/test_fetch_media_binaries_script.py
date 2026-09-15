"""Resilience contract for scripts/fetch-media-binaries.sh.

The macOS Tauri release lane died with `curl: (18) Transferred a partial file`
from a flaky osxexperts.net mirror. These tests pin the fix: retries with
backoff, partial-transfer rejection, alternative sources, a local fallback, and
a loud actionable failure when everything is exhausted.

No network access is used — `curl` is replaced by a stub that emits synthetic
Mach-O payloads / zips / truncated downloads.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "fetch-media-binaries.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="fetch-media-binaries.sh is a bash script",
)

HAVE_SHELL_TOOLS = all(shutil.which(t) for t in ("bash", "unzip", "tar"))

# The execution tests invoke the real script, which needs unzip/tar to unpack
# payloads; skip them on hosts without those tools rather than reporting a
# false failure (the static contract tests below always run).
requires_shell_tools = pytest.mark.skipif(
    not HAVE_SHELL_TOOLS, reason="bash/unzip/tar not available on this host"
)

# Tools the script needs; symlinked into an isolated PATH so a host ffmpeg
# (common on CI images) cannot accidentally satisfy the local-fallback branch.
NEEDED_TOOLS = (
    "bash", "unzip", "tar", "find", "head", "chmod", "wc", "tr", "grep",
    "sleep", "basename", "dirname", "uname", "file", "mkdir", "cp", "mv",
    "rm", "ls", "tail", "cut",
)

STUB_CURL = r'''#!{py}
import io, sys, zipfile
from pathlib import Path

MODE = "{mode}"
MACHO = b"\xcf\xfa\xed\xfe" + b"\x00" * 4096
GARBAGE = b"PK\x03\x04 this is a truncated archive "
ZIPAPP = b"#!/usr/bin/env python3\n" + b"x" * 1_100_000

args = sys.argv[1:]
out = url = None
for i, a in enumerate(args):
    if a == "-o" and i + 1 < len(args):
        out = args[i + 1]
    elif a.startswith("http"):
        url = a

def fail():
    sys.stderr.write("curl: (18) Transferred a partial file (stub)\n")
    sys.exit(18)

if out is None or url is None:
    sys.exit(0)
if MODE == "all-fail":
    fail()
if MODE == "osxexperts-fails" and "osxexperts.net" in url:
    fail()

want = "ffprobe" if "ffprobe" in url else "ffmpeg"
if "yt-dlp" in url:

    Path(out).write_bytes(ZIPAPP)
elif MODE == "corrupt-all":

    Path(out).write_bytes(GARBAGE)
elif url.endswith(".zip") or url.endswith("/zip"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(want, GARBAGE if MODE == "corrupt-zip" else MACHO)

    Path(out).write_bytes(buf.getvalue())
else:

    Path(out).write_bytes(MACHO)
'''


def _isolated_path(tmp_path: Path, mode: str, with_local_ffmpeg: bool = False) -> str:
    """PATH containing symlinked tools + a stub curl (and optionally ffmpeg)."""
    tools = tmp_path / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    for tool in NEEDED_TOOLS:
        found = shutil.which(tool)
        if found:
            link = tools / tool
            if not link.exists():
                link.symlink_to(found)
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / "curl"
    stub.write_text(STUB_CURL.format(py=sys.executable, mode=mode), encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    parts = [str(stub_dir), str(tools)]
    if with_local_ffmpeg:
        local = tmp_path / "localbin"
        local.mkdir(exist_ok=True)
        for name in ("ffmpeg", "ffprobe"):
            binary = local / name
            binary.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 4096)
            binary.chmod(0o755)
        parts.append(str(local))
    return os.pathsep.join(parts)


def _run(tmp_path: Path, mode: str, with_local_ffmpeg: bool = False) -> tuple[subprocess.CompletedProcess, Path]:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, root / "scripts" / "fetch-media-binaries.sh")
    env = {
        "PATH": _isolated_path(tmp_path, mode, with_local_ffmpeg),
        "HOME": str(tmp_path),
        "FETCH_RETRY_DELAY": "0",
        "FETCH_RETRIES": "1",
        "FETCH_ROUNDS": "1",
        "FETCH_CONNECT_TIMEOUT": "5",
        "FETCH_MAX_TIME": "60",
    }
    proc = subprocess.run(
        ["bash", str(root / "scripts" / "fetch-media-binaries.sh"), "macos-arm64"],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    return proc, root


def test_script_uses_retry_and_partial_transfer_flags() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for flag in (
        "--fail",
        "--retry ",
        "--retry-all-errors",
        "--retry-delay",
        "--retry-connrefused",
        "--continue-at -",
    ):
        assert flag in text, f"missing {flag!r} in fetch-media-binaries.sh"


def test_script_has_alternative_sources_and_loud_failure() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "evermeet.cx" in text, "no evermeet.cx fallback source"
    assert "ffmpeg-static/releases/download" in text, "no pinned GitHub fallback asset"
    assert "MEDIA BINARY FETCH FAILED" in text, "no loud failure report"
    assert "fallback_from_path" in text, "no local-binary fallback"


@requires_shell_tools
def test_primary_source_used_when_healthy(tmp_path: Path) -> None:
    proc, root = _run(tmp_path, "success")
    assert proc.returncode == 0, proc.stderr
    ffmpeg = root / "src-tauri" / "binaries" / "ffmpeg" / "ffmpeg"
    ffprobe = root / "src-tauri" / "binaries" / "ffmpeg" / "ffprobe"
    assert ffmpeg.is_file() and os.access(ffmpeg, os.X_OK)
    assert ffprobe.is_file()
    assert "osxexperts.net/ffmpeg6arm.zip" in proc.stderr


@requires_shell_tools
def test_falls_back_when_osxexperts_truncates(tmp_path: Path) -> None:
    proc, root = _run(tmp_path, "osxexperts-fails")
    assert proc.returncode == 0, proc.stderr
    assert "ffmpeg-darwin-arm64" in proc.stderr, "pinned GitHub asset was not used"
    ffmpeg = root / "src-tauri" / "binaries" / "ffmpeg" / "ffmpeg"
    assert ffmpeg.is_file() and os.access(ffmpeg, os.X_OK)


@requires_shell_tools
def test_corrupt_archive_is_rejected_not_installed(tmp_path: Path) -> None:
    proc, root = _run(tmp_path, "corrupt-all")
    assert proc.returncode == 1
    assert "rejected truncated/corrupt zip" in proc.stderr
    assert "MEDIA BINARY FETCH FAILED" in proc.stderr
    assert not (root / "src-tauri" / "binaries" / "ffmpeg" / "ffmpeg").exists()


@requires_shell_tools
def test_all_sources_failed_reports_loudly(tmp_path: Path) -> None:
    proc, root = _run(tmp_path, "all-fail")
    assert proc.returncode == 1
    assert "MEDIA BINARY FETCH FAILED (macos-arm64)" in proc.stderr
    assert "Could not obtain: ffmpeg ffprobe" in proc.stderr
    assert "osxexperts.net/ffmpeg6arm.zip" in proc.stderr  # names what was tried
    assert "evermeet.cx" in proc.stderr
    assert not (root / "src-tauri" / "binaries" / "ffmpeg" / "ffmpeg").exists()


@requires_shell_tools
def test_local_binary_used_as_last_resort(tmp_path: Path) -> None:
    proc, root = _run(tmp_path, "all-fail", with_local_ffmpeg=True)
    assert proc.returncode == 0, proc.stderr
    assert "falling back to" in proc.stderr
    ffmpeg = root / "src-tauri" / "binaries" / "ffmpeg" / "ffmpeg"
    assert ffmpeg.is_file() and os.access(ffmpeg, os.X_OK)
