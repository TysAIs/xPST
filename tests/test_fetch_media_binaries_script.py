"""Provenance contract for scripts/fetch-media-binaries.sh.

The macOS Tauri release lane died with `curl: (18) Transferred a partial file`
from a flaky osxexperts.net mirror, and the lane had no way to say *which*
bytes it bundled. These tests pin the two halves of the fix:

  * `scripts/media-binaries.lock` names every input by an immutable url plus the
    SHA-256 of the exact file that url serves, and the script refuses to
    install anything whose bytes do not match (so upstream drift is a named
    failure, never a silently different release);
  * transport stays resilient (retries, partial-transfer rejection, resume) and
    a failed fetch reports every pinned candidate it tried;
  * an unverified local binary can never slip into a build: the PATH fallback
    only exists behind XPST_ALLOW_UNPINNED_MEDIA=1 and is recorded as UNPINNED
    in the provenance file the release lane asserts against.

Network access is not used - `curl` is replaced by a stub that serves synthetic
Mach-O payloads whose SHA-256 the test computes into a temporary lock file.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "fetch-media-binaries.sh"
LOCK = REPO_ROOT / "scripts" / "media-binaries.lock"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="fetch-media-binaries.sh is a bash script",
)

HAVE_SHELL_TOOLS = all(shutil.which(t) for t in ("bash", "unzip", "tar"))
HAVE_SHA_TOOL = any(shutil.which(t) for t in ("shasum", "sha256sum", "openssl", "python3", "python"))

# The execution tests invoke the real script, which needs unzip/tar to unpack
# payloads and a sha256 tool; skip them on hosts without those rather than
# reporting a false failure (the static contract tests below always run).
requires_shell_tools = pytest.mark.skipif(
    not (HAVE_SHELL_TOOLS and HAVE_SHA_TOOL),
    reason="bash/unzip/tar/sha256 tool not available on this host",
)

NEEDED_TOOLS = (
    "bash", "unzip", "tar", "find", "head", "tail", "chmod", "wc", "tr", "grep",
    "sleep", "basename", "dirname", "uname", "file", "mkdir", "cp", "mv", "rm",
    "ls", "cut", "awk", "sort", "cat", "shasum", "sha256sum", "openssl",
    "python3", "python",
)

MACHO = b"\xcf\xfa\xed\xfe" + b"\x00" * 4096
MACHO_ALT = b"\xcf\xfa\xed\xfe" + b"\x01" * 4096
ZIPAPP = b"#!/usr/bin/env python3\n" + b"x" * 1_100_000
PARTIAL = b"\xcf\xfa\xed\xfe truncated"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


STUB_CURL = r'''#!{py}
import sys
from pathlib import Path

MODE = "{mode}"
PAYLOADS = {{
    "ffmpeg-a": {macho!r},
    "ffmpeg-b": {macho_alt!r},
    "ffprobe": {macho!r},
    "yt-dlp": {zipapp!r},
    "bad": {partial!r},
}}

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
if MODE == "primary-fails" and url.endswith("ffmpeg-a"):
    fail()

key = "yt-dlp" if "yt-dlp" in url else ("ffprobe" if "ffprobe" in url else "ffmpeg-a")
key = {{
    "https://example.invalid/ffmpeg-a": "ffmpeg-a",
    "https://example.invalid/ffmpeg-b": "ffmpeg-b",
    "https://example.invalid/ffprobe": "ffprobe",
    "https://example.invalid/yt-dlp": "yt-dlp",
}}.get(url, key)
if MODE == "mismatch" and key == "ffmpeg-a":
    key = "bad"
Path(out).write_bytes(PAYLOADS[key])
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
    stub.write_text(
        STUB_CURL.format(
            py=sys.executable,
            mode=mode,
            macho=MACHO,
            macho_alt=MACHO_ALT,
            zipapp=ZIPAPP,
            partial=PARTIAL,
        ),
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    parts = [str(stub_dir), str(tools)]
    if with_local_ffmpeg:
        local = tmp_path / "localbin"
        local.mkdir(exist_ok=True)
        for name, payload in (("ffmpeg", MACHO), ("ffprobe", MACHO), ("yt-dlp", ZIPAPP)):
            binary = local / name
            binary.write_bytes(payload)
            binary.chmod(0o755)
        parts.append(str(local))
    return os.pathsep.join(parts)


def _write_lock(root: Path, ffmpeg_second: bool = True) -> Path:
    """A temporary lock whose hashes match the stub payloads."""
    lines = [
        "# test lock",
        f"macos-arm64\tffmpeg\traw\t-\t{sha(MACHO)}\thttps://example.invalid/ffmpeg-a",
    ]
    if ffmpeg_second:
        lines.append(f"macos-arm64\tffmpeg\traw\t-\t{sha(MACHO_ALT)}\thttps://example.invalid/ffmpeg-b")
    lines.append(f"macos-arm64\tffprobe\traw\t-\t{sha(MACHO)}\thttps://example.invalid/ffprobe")
    lines.append(f"macos-arm64\tyt-dlp\traw\t-\t{sha(ZIPAPP)}\thttps://example.invalid/yt-dlp")
    lock = root / "scripts" / "media-binaries.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lock


def _run(
    tmp_path: Path,
    mode: str,
    with_local_ffmpeg: bool = False,
    allow_unpinned: bool = False,
    ffmpeg_second: bool = True,
) -> tuple[subprocess.CompletedProcess, Path]:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, root / "scripts" / "fetch-media-binaries.sh")
    lock = _write_lock(root, ffmpeg_second=ffmpeg_second)
    env = {
        "PATH": _isolated_path(tmp_path, mode, with_local_ffmpeg),
        "HOME": str(tmp_path),
        "MEDIA_BINARIES_LOCK": str(lock),
        "MEDIA_BINARIES_CACHE": str(tmp_path / "cache"),
        "FETCH_RETRY_DELAY": "0",
        "FETCH_RETRIES": "1",
        "FETCH_ROUNDS": "1",
        "FETCH_CONNECT_TIMEOUT": "5",
        "FETCH_MAX_TIME": "60",
    }
    if allow_unpinned:
        env["XPST_ALLOW_UNPINNED_MEDIA"] = "1"
    proc = subprocess.run(
        ["bash", str(root / "scripts" / "fetch-media-binaries.sh"), "macos-arm64"],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    return proc, root


def _installed(root: Path, name: str = "ffmpeg") -> Path:
    return root / "src-tauri" / "binaries" / "ffmpeg" / name


def _provenance(root: Path) -> str:
    return (root / "src-tauri" / "binaries" / "PROVENANCE.txt").read_text(encoding="utf-8")


# --- static contract ---------------------------------------------------------

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


def test_script_verifies_checksums_and_refuses_mismatches() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "media-binaries.lock" in text, "script does not read the pinned lock file"
    assert "PROVENANCE MISMATCH" in text, "no explicit provenance-mismatch failure"
    assert "MEDIA BINARY FETCH FAILED" in text, "no loud failure report"
    # An unpinned mirror must not be referenced any more.
    for unpinned in ("osxexperts", "evermeet", "gyan.dev", "johnvansickle", "releases/latest"):
        assert unpinned not in text, f"unpinned source {unpinned!r} still referenced"


def test_script_has_no_unconditional_path_fallback() -> None:
    """A host ffmpeg on PATH must never silently satisfy a release build."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "XPST_ALLOW_UNPINNED_MEDIA" in text, "opt-in escape hatch missing"
    fallback = text.split("XPST_ALLOW_UNPINNED_MEDIA", 1)[1]
    assert "UNPINNED" in fallback, "unpinned fallback is not recorded in provenance"


def test_lock_file_is_pinned_and_checksummed() -> None:
    assert LOCK.is_file(), "scripts/media-binaries.lock is missing"
    rows = []
    for raw in LOCK.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        assert len(fields) == 6, f"lock row must have 6 tab-separated fields: {raw!r}"
        platform, artifact, kind, member, digest, url = fields
        assert platform in {"macos-arm64", "macos-x64", "linux-x64", "linux-arm64", "win-x64"}
        assert artifact in {"ffmpeg", "ffprobe", "yt-dlp"}
        assert kind in {"raw", "zip", "tar.xz"}
        assert re.fullmatch(r"[0-9a-f]{64}", digest), f"sha256 not a hex digest: {digest!r}"
        assert url.startswith("https://"), f"not https: {url!r}"
        # Immutability: a pinned url names a version tag, never a moving target.
        assert "/releases/download/" in url, f"not a versioned release asset: {url!r}"
        assert not re.search(r"/(latest|getrelease)/", url), f"moving-target url: {url!r}"
        if kind == "raw":
            assert member == "-"
        else:
            assert member != "-"
        rows.append(fields)

    platforms = {r[0] for r in rows}
    assert platforms == {"macos-arm64", "macos-x64", "linux-x64", "linux-arm64", "win-x64"}
    for platform in platforms:
        for artifact in ("ffmpeg", "ffprobe", "yt-dlp"):
            assert any(r[0] == platform and r[1] == artifact for r in rows), (
                f"{platform}/{artifact} has no pinned candidate"
            )


def test_workflow_asserts_a_fully_pinned_build() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "tauri-release.yml").read_text(encoding="utf-8")
    assert "verify-media-binaries-provenance.sh" in workflow, (
        "release lane does not check the media-binary provenance record"
    )
    assert "media-binaries-PROVENANCE-${{ matrix.target }}.txt" in workflow, (
        "release lane does not attach the provenance record to the release"
    )
    checker = (REPO_ROOT / "scripts" / "verify-media-binaries-provenance.sh").read_text(encoding="utf-8")
    assert "UNPINNED" in checker, "checker does not reject an unpinned build"
    for artifact in ("ffmpeg", "ffprobe", "yt-dlp"):
        assert artifact in checker, f"checker does not require {artifact}"


PINNED_ROW = "pinned\t{platform}\t{artifact}\tsha256={sha}\tinstalled_sha256={sha}\tbytes=1\turl=https://example.invalid/{artifact}\n"


def _provenance_file(tmp_path: Path, rows: str) -> Path:
    path = tmp_path / "PROVENANCE.txt"
    path.write_text(rows, encoding="utf-8")
    return path


def _run_checker(tmp_path: Path, path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "scripts/verify-media-binaries-provenance.sh", str(path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_provenance_checker_accepts_a_fully_pinned_build(tmp_path: Path) -> None:
    rows = "".join(
        PINNED_ROW.format(platform="macos-arm64", artifact=artifact, sha="a" * 64)
        for artifact in ("ffmpeg", "ffprobe", "yt-dlp")
    )
    proc = _run_checker(tmp_path, _provenance_file(tmp_path, rows))
    assert proc.returncode == 0, proc.stderr
    assert "PASS: every bundled media binary" in proc.stdout


def test_provenance_checker_rejects_an_unpinned_build(tmp_path: Path) -> None:
    rows = "UNPINNED\tmacos-arm64\tffmpeg\tpath=/usr/local/bin/ffmpeg\n"
    rows += "".join(
        PINNED_ROW.format(platform="macos-arm64", artifact=artifact, sha="a" * 64)
        for artifact in ("ffprobe", "yt-dlp")
    )
    proc = _run_checker(tmp_path, _provenance_file(tmp_path, rows))
    assert proc.returncode == 1
    assert "UNPINNED" in proc.stderr


def test_provenance_checker_rejects_a_missing_artifact(tmp_path: Path) -> None:
    rows = "".join(
        PINNED_ROW.format(platform="macos-arm64", artifact=artifact, sha="a" * 64)
        for artifact in ("ffmpeg", "ffprobe")
    )
    proc = _run_checker(tmp_path, _provenance_file(tmp_path, rows))
    assert proc.returncode == 1
    assert "yt-dlp has no pinned provenance entry" in proc.stderr


def test_provenance_checker_rejects_a_missing_record(tmp_path: Path) -> None:
    proc = _run_checker(tmp_path, tmp_path / "absent.txt")
    assert proc.returncode == 1
    assert "no media-binary provenance record" in proc.stderr


# --- behaviour ---------------------------------------------------------------

@requires_shell_tools
def test_pinned_source_installs_and_records_provenance(tmp_path: Path) -> None:
    proc, root = _run(tmp_path, "ok")
    assert proc.returncode == 0, proc.stderr
    for name in ("ffmpeg", "ffprobe"):
        binary = _installed(root, name)
        assert binary.is_file() and os.access(binary, os.X_OK)
    assert (root / "src-tauri" / "binaries" / "ytdlp" / "yt-dlp").is_file()
    provenance = _provenance(root)
    assert provenance.count("pinned\tmacos-arm64") == 3
    assert "UNPINNED" not in provenance
    assert sha(MACHO) in provenance


@requires_shell_tools
def test_candidate_checksum_mismatch_fails_closed(tmp_path: Path) -> None:
    proc, root = _run(tmp_path, "mismatch")
    assert proc.returncode == 1
    assert "MEDIA BINARY PROVENANCE MISMATCH" in proc.stderr
    assert "scripts/media-binaries.lock" in proc.stderr
    assert not _installed(root).exists(), "unverified bytes were installed"


@requires_shell_tools
def test_second_pinned_candidate_covers_a_broken_primary(tmp_path: Path) -> None:
    proc, root = _run(tmp_path, "primary-fails")
    assert proc.returncode == 0, proc.stderr
    assert "ffmpeg-b" in proc.stderr, "pinned secondary candidate was not used"
    assert sha(MACHO_ALT) in _provenance(root)


@requires_shell_tools
def test_all_pinned_candidates_failing_reports_loudly(tmp_path: Path) -> None:
    proc, root = _run(tmp_path, "all-fail")
    assert proc.returncode == 1
    assert "MEDIA BINARY FETCH FAILED (macos-arm64)" in proc.stderr
    assert "Could not obtain: ffmpeg ffprobe yt-dlp" in proc.stderr
    assert "https://example.invalid/ffmpeg-a" in proc.stderr  # names what was tried
    assert not _installed(root).exists()


@requires_shell_tools
def test_local_binary_is_not_used_without_the_optin(tmp_path: Path) -> None:
    proc, root = _run(tmp_path, "all-fail", with_local_ffmpeg=True)
    assert proc.returncode == 1, "an unpinned host ffmpeg was bundled into the build"
    assert not _installed(root).exists()


@requires_shell_tools
def test_local_binary_optin_is_recorded_as_unpinned(tmp_path: Path) -> None:
    proc, root = _run(tmp_path, "all-fail", with_local_ffmpeg=True, allow_unpinned=True)
    assert proc.returncode == 0, proc.stderr
    assert "UNPINNED" in proc.stderr
    assert "UNPINNED" in _provenance(root), "unpinned fallback was not recorded"
