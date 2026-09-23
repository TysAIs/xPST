"""Provenance + resilience contract for scripts/fetch-media-binaries.sh.

The script used to fetch ffmpeg/ffprobe/yt-dlp into ``src-tauri/binaries/`` so
Tauri could bundle them. ffmpeg+ffprobe were 87 MB of a 192 MB app and the flaky
mirror behind that download is what kept the macOS release lane red; ffmpeg is
now resolved at runtime (``xpst.media.binaries``: env override > system >
fetched, checksum-verified, on first use), so the script only fetches the ~3 MB
yt-dlp zipapp.

These tests pin the union of the two contracts:

  * ``scripts/media-binaries.lock`` names every BUNDLE input by an immutable url
    plus the SHA-256 of the exact file that url serves, and the script refuses to
    install anything whose bytes do not match (upstream drift is a named failure,
    never a silently different release);
  * transport stays resilient (retries, partial-transfer rejection, resume) and a
    failed fetch reports every pinned candidate it tried;
  * an unverified local binary can never slip into a build: the PATH fallback
    only exists behind XPST_ALLOW_UNPINNED_MEDIA=1 and is recorded as UNPINNED in
    the provenance file the release lane asserts against;
  * the script no longer produces an ffmpeg/ffprobe bundle input at all, and a
    failed yt-dlp fetch is loud but non-fatal (the engine's bundled yt_dlp module
    is the primary path).

No network access is used — ``curl`` is replaced by a stub that serves synthetic
payloads whose SHA-256 the tests compute into a temporary lock file.
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
CHECKER = REPO_ROOT / "scripts" / "verify-media-binaries-provenance.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tauri-release.yml"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="fetch-media-binaries.sh is a bash script",
)

HAVE_SHELL_TOOLS = all(shutil.which(t) for t in ("bash", "head", "wc", "grep", "file"))
HAVE_SHA_TOOL = any(shutil.which(t) for t in ("shasum", "sha256sum", "openssl", "python3", "python"))

# The execution tests invoke the real script, which needs a sha256 tool; skip
# them on hosts without one rather than reporting a false failure (the static
# contract tests below always run).
requires_shell_tools = pytest.mark.skipif(
    not (HAVE_SHELL_TOOLS and HAVE_SHA_TOOL),
    reason="bash/head/grep/file/sha256 tool not available on this host",
)

NEEDED_TOOLS = (
    "bash", "unzip", "tar", "find", "head", "tail", "chmod", "wc", "tr", "grep",
    "sleep", "basename", "dirname", "uname", "file", "mkdir", "cp", "mv", "rm",
    "ls", "cut", "awk", "sort", "cat", "shasum", "sha256sum", "openssl",
    "python3", "python",
)

ZIPAPP = b"#!/usr/bin/env python3\n" + b"x" * 1_100_000
ZIPAPP_ALT = b"#!/usr/bin/env python3\n" + b"y" * 1_100_000
TINY_ZIPAPP = b"#!/usr/bin/env python3\n" + b"x" * 500


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


STUB_CURL = r'''#!{py}
import sys
from pathlib import Path

MODE = "{mode}"
ZIPAPP = {zipapp!r}
ZIPAPP_ALT = {zipapp_alt!r}
TINY = {tiny!r}
BY_URL = {{
    "https://example.invalid/yt-dlp-a": ZIPAPP,
    "https://example.invalid/yt-dlp-b": ZIPAPP_ALT,
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
if MODE == "primary-fails" and url.endswith("yt-dlp-a"):
    fail()
if MODE in ("truncated", "mismatch"):
    Path(out).write_bytes(TINY)
else:
    Path(out).write_bytes(BY_URL.get(url, ZIPAPP))
'''


def _isolated_path(tmp_path: Path, mode: str, with_local_ytdlp: bool = False) -> str:
    """PATH containing symlinked tools + a stub curl (and optionally yt-dlp)."""
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
            zipapp=ZIPAPP,
            zipapp_alt=ZIPAPP_ALT,
            tiny=TINY_ZIPAPP,
        ),
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    parts = [str(stub_dir), str(tools)]
    if with_local_ytdlp:
        local = tmp_path / "localbin"
        local.mkdir(exist_ok=True)
        binary = local / "yt-dlp"
        binary.write_bytes(ZIPAPP)
        binary.chmod(0o755)
        parts.append(str(local))
    return os.pathsep.join(parts)


def _write_lock(root: Path, pinned: tuple[bytes, ...] = (ZIPAPP, ZIPAPP_ALT)) -> Path:
    """A temporary lock whose hashes match the stub payloads for this test."""
    lines = ["# test lock"]
    for index, payload in enumerate(pinned):
        suffix = chr(ord("a") + index)
        lines.append(
            f"macos-arm64\tyt-dlp\traw\t-\t{sha(payload)}\thttps://example.invalid/yt-dlp-{suffix}"
        )
    lock = root / "scripts" / "media-binaries.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lock


def _run(
    tmp_path: Path,
    mode: str,
    with_local_ytdlp: bool = False,
    allow_unpinned: bool = False,
    pinned: tuple[bytes, ...] = (ZIPAPP, ZIPAPP_ALT),
) -> tuple[subprocess.CompletedProcess, Path]:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, root / "scripts" / "fetch-media-binaries.sh")
    lock = _write_lock(root, pinned=pinned)
    env = {
        "PATH": _isolated_path(tmp_path, mode, with_local_ytdlp),
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


def _installed(root: Path, name: str = "yt-dlp") -> Path:
    return root / "src-tauri" / "binaries" / "ytdlp" / name


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


def test_script_is_lock_driven_and_verifies_checksums() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "media-binaries.lock" in text, "script does not read the pinned lock file"
    assert "PROVENANCE MISMATCH" in text, "no explicit provenance-mismatch failure"
    # No download url is baked into the script: they all live in the lock, which
    # is what makes the pin auditable in one place.
    assert "https://" not in text, "a download url leaked into the script instead of the lock"
    for unpinned in ("osxexperts", "evermeet", "gyan.dev", "johnvansickle", "releases/latest"):
        assert unpinned not in text, f"unpinned source {unpinned!r} still referenced"


def test_script_no_longer_fetches_ffmpeg() -> None:
    """ffmpeg/ffprobe must not be a bundle input again — that is 87MB."""
    text = SCRIPT.read_text(encoding="utf-8")
    for gone in ("osxexperts", "evermeet", "ffmpeg6arm", "ffprobe6arm", "FF_DIR=", "fetch_artifact ffmpeg"):
        assert gone not in text, f"{gone!r} is back in fetch-media-binaries.sh"
    assert "eugeneware" not in text, "an ffmpeg mirror is referenced again"
    assert "intentionally NOT bundled" in text, "the script does not say ffmpeg is unbundled"


def test_script_points_at_the_runtime_ffmpeg_path() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "xpst media fetch" in text, "the script does not point at the runtime fetch command"


def test_script_reports_ytdlp_failure_loudly_but_does_not_fail_the_lane() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "YT-DLP FETCH FAILED" in text, "no loud failure report"
    assert "bundled yt_dlp module" in text, "no explanation that this is non-fatal"
    # The failure path must not abort the lane: the engine bundles the yt_dlp
    # Python module, so a yt-dlp zipapp fetch failure is a warning, not red CI.
    assert "exit 0" in text, "the script can still exit non-zero on a fetch failure"


def test_script_has_no_unconditional_path_fallback() -> None:
    """A host yt-dlp on PATH must never silently satisfy a release build."""
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
        assert artifact == "yt-dlp", f"the lock pins a binary the lane does not bundle: {artifact!r}"
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
        assert any(r[0] == platform and r[1] == "yt-dlp" for r in rows), (
            f"{platform}/yt-dlp has no pinned candidate"
        )


def test_workflow_asserts_a_fully_pinned_build() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "verify-media-binaries-provenance.sh" in workflow, (
        "release lane does not check the media-binary provenance record"
    )
    assert "media-binaries-PROVENANCE-${{ matrix.target }}.txt" in workflow, (
        "release lane does not attach the provenance record to the release"
    )
    checker = CHECKER.read_text(encoding="utf-8")
    assert "UNPINNED" in checker, "checker does not reject an unpinned build"
    assert "yt-dlp" in checker, "checker does not cover yt-dlp"


PINNED_ROW = "pinned\t{platform}\tyt-dlp\tsha256={sha}\tinstalled_sha256={sha}\tbytes=1\turl=https://example.invalid/yt-dlp\n"


def _provenance_file(tmp_path: Path, rows: str) -> Path:
    path = tmp_path / "PROVENANCE.txt"
    path.write_text(rows, encoding="utf-8")
    return path


def _run_checker(tmp_path: Path, path: Path, bin_dir: Path | None = None) -> subprocess.CompletedProcess:
    args = ["bash", str(CHECKER), str(path)]
    if bin_dir is not None:
        args.append(str(bin_dir))
    return subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)


def test_provenance_checker_accepts_a_fully_pinned_build(tmp_path: Path) -> None:
    rows = PINNED_ROW.format(platform="macos-arm64", sha="a" * 64)
    proc = _run_checker(tmp_path, _provenance_file(tmp_path, rows))
    assert proc.returncode == 0, proc.stderr
    assert "PASS: every bundled media binary" in proc.stdout


def test_provenance_checker_rejects_an_unpinned_build(tmp_path: Path) -> None:
    rows = "UNPINNED\tmacos-arm64\tyt-dlp\tpath=/usr/local/bin/yt-dlp\n"
    proc = _run_checker(tmp_path, _provenance_file(tmp_path, rows))
    assert proc.returncode == 1
    assert "UNPINNED" in proc.stderr


def test_provenance_checker_rejects_a_bundled_binary_without_a_pinned_row(tmp_path: Path) -> None:
    """A binary sitting in the bundle inputs must be covered by the record."""
    bin_dir = tmp_path / "binaries"
    (bin_dir / "ytdlp").mkdir(parents=True)
    (bin_dir / "ytdlp" / "yt-dlp").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    proc = _run_checker(tmp_path, _provenance_file(tmp_path, ""), bin_dir)
    assert proc.returncode == 1
    assert "no pinned provenance entry" in proc.stderr


def test_provenance_checker_passes_when_nothing_is_bundled(tmp_path: Path) -> None:
    """ffmpeg/ffprobe are not bundled and a yt-dlp fetch may fail; neither is a lane failure."""
    bin_dir = tmp_path / "binaries"
    bin_dir.mkdir()
    proc = _run_checker(tmp_path, tmp_path / "absent.txt", bin_dir)
    assert proc.returncode == 0, proc.stderr
    assert "PASS: every bundled media binary" in proc.stdout


# --- behaviour ---------------------------------------------------------------


@requires_shell_tools
def test_pinned_source_installs_and_records_provenance(tmp_path: Path) -> None:
    proc, root = _run(tmp_path, "ok")
    assert proc.returncode == 0, proc.stderr
    binary = _installed(root)
    assert binary.is_file() and os.access(binary, os.X_OK)
    provenance = _provenance(root)
    assert provenance.count("pinned\tmacos-arm64") == 1
    assert "UNPINNED" not in provenance
    assert sha(ZIPAPP) in provenance
    assert not (root / "src-tauri" / "binaries" / "ffmpeg").exists(), (
        "the script must not create a bundled ffmpeg directory"
    )


@requires_shell_tools
def test_truncated_ytdlp_download_is_rejected(tmp_path: Path) -> None:
    proc, root = _run(tmp_path, "truncated", pinned=(TINY_ZIPAPP,))
    assert "rejecting partial transfer" in proc.stderr
    assert not _installed(root).exists()
    assert "YT-DLP FETCH FAILED" in proc.stderr


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
    assert "yt-dlp-b" in proc.stderr, "pinned secondary candidate was not used"
    assert sha(ZIPAPP_ALT) in _provenance(root)


@requires_shell_tools
def test_all_pinned_candidates_failing_reports_loudly_but_exits_zero(tmp_path: Path) -> None:
    """The engine bundles the yt_dlp module, so this must not fail the lane."""
    proc, root = _run(tmp_path, "all-fail")
    assert proc.returncode == 0, proc.stderr
    assert "YT-DLP FETCH FAILED" in proc.stderr
    assert "https://example.invalid/yt-dlp-a" in proc.stderr  # names what was tried
    assert "bundled yt_dlp module" in proc.stderr
    assert not _installed(root).exists()


@requires_shell_tools
def test_local_binary_is_not_used_without_the_optin(tmp_path: Path) -> None:
    _proc, root = _run(tmp_path, "all-fail", with_local_ytdlp=True)
    assert not _installed(root).exists(), "an unpinned host yt-dlp was bundled into the build"


@requires_shell_tools
def test_local_binary_optin_is_recorded_as_unpinned(tmp_path: Path) -> None:
    proc, root = _run(tmp_path, "all-fail", with_local_ytdlp=True, allow_unpinned=True)
    assert proc.returncode == 0, proc.stderr
    assert "UNPINNED" in proc.stderr
    assert "UNPINNED" in _provenance(root), "unpinned fallback was not recorded"
