#!/usr/bin/env python3
"""Install and smoke-test a published xPST desktop artifact.

This helper is intentionally dependency-free.  The shell entry point owns the
public interface; this module owns the platform-specific install and launch
logic so failures can be reported in one machine-readable summary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform as host_platform
import plistlib
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


class E2EError(RuntimeError):
    """A user-facing harness failure with a concise message."""


#: macOS puts TMPDIR under ``/var/folders/<id>/T`` and symlinks ``/tmp`` ->
#: ``/private/tmp`` and ``/var`` -> ``/private/var``. The published Tauri shell
#: cannot resolve its bundle resource directory when the ``.app`` is launched
#: through either of those symlinked components in their un-resolved form (it
#: logs ``FATAL: no resource dir`` and never starts the engine), so the
#: clean-profile install defaults to the canonical ``/private/tmp`` — a real,
#: user-visible location a stranger could install into. ``--work-dir`` still
#: overrides it.
DEFAULT_WORK_BASE = (
    str(Path("/tmp").resolve()) if sys.platform == "darwin" else None  # nosec B108 - deliberate canonical location
)

#: The packaged-UI and sidecar observations are retried over a SHORT, BOUNDED
#: window before the harness records a failure, because a single sample races
#: the shell's own startup.  Measured on run 42508682981 (macOS leg): the launch
#: log showed ``Uvicorn running on http://127.0.0.1:<port>`` and ``GET / 200``
#: while the one-shot sample read a non-HTML root and an empty sidecar list;
#: the identical commit passed on a re-run.  Retrying never weakens the
#: assertion — it must still be status 200 + ``<html`` + an xPST marker, and a
#: tauri stack still needs an ``xpst-engine`` sidecar — it only stops the
#: harness from calling a healthy app dead in the instant before its shell
#: finishes re-exec'ing.  Both the window and the attempt count are recorded in
#: the evidence (``ui.attempts``, ``running_process.engine_sidecar_attempts``)
#: so a slow-but-passing boot stays visible instead of silent.
PACKAGED_UI_RETRY_WINDOW_SECONDS = 8.0
SIDECAR_RETRY_WINDOW_SECONDS = 8.0
OBSERVATION_POLL_INTERVAL_SECONDS = 0.25


class WorkDir:
    """Own the throwaway directory and clean it up unless requested otherwise."""

    def __init__(self, requested: str | None) -> None:
        if requested:
            self.path = Path(requested).expanduser().resolve()
            if self.path.exists() and any(self.path.iterdir()):
                raise E2EError(f"--work-dir must be absent or empty: {self.path}")
            self.path.mkdir(parents=True, exist_ok=True)
            self.owned = True
        else:
            try:
                created = tempfile.mkdtemp(prefix="xpst-stranger-install-", dir=DEFAULT_WORK_BASE)
            except OSError:
                created = tempfile.mkdtemp(prefix="xpst-stranger-install-")
            # Canonicalize so the app is never launched through a symlinked
            # directory component (for example /tmp -> /private/tmp).
            self.path = Path(created).resolve()
            self.owned = True

    def cleanup(self, keep: bool) -> bool:
        if keep:
            return False
        # A portable AppImage self-extracts and its WebKit helper processes can
        # still be flushing cache files into the isolated HOME while the tree is
        # walked, so a single delete can leave a partial directory behind. Retry
        # briefly instead of reporting a teardown failure for a filesystem race.
        for attempt in range(6):
            shutil.rmtree(self.path, ignore_errors=True)
            if not self.path.exists():
                return True
            time.sleep(0.5 * (attempt + 1))
        return not self.path.exists()


def command_result(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Run a command without hiding its real exit status or output."""
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return {"returncode": 127, "stdout": "", "stderr": str(exc), "command": command}
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": 124,
            "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "") if isinstance(exc.stderr, str) else "",
            "command": command,
        }
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "command": command,
    }


def short_command_error(result: dict[str, Any]) -> str:
    """Format command failure output without inventing a reason."""
    command = " ".join(str(part) for part in result["command"])
    stdout = str(result.get("stdout", "")).strip()
    stderr = str(result.get("stderr", "")).strip()
    detail = stderr or stdout or "no output"
    return f"{command} exited {result['returncode']}: {detail}"


def fetch_url(url: str, destination: Path) -> None:
    """Download with curl -L, retries, and a bounded transfer."""
    if shutil.which("curl") is None:
        raise E2EError("curl is required for URL artifacts and checksum files")
    result = command_result(
        [
            "curl",
            "-L",
            "--fail",
            "--show-error",
            "--retry",
            "2",
            "--connect-timeout",
            "20",
            "--max-time",
            "900",
            "-o",
            str(destination),
            url,
        ],
        timeout=930,
    )
    if result["returncode"] != 0:
        raise E2EError(f"download failed for {url}: {short_command_error(result)}")
    if not destination.is_file() or destination.stat().st_size == 0:
        raise E2EError(f"download produced no file: {url}")


def artifact_name(source: str) -> str:
    """Return the asset basename for local paths and release URLs."""
    parsed = urllib.parse.urlparse(source)
    name = Path(urllib.parse.unquote(parsed.path)).name if parsed.scheme else Path(source).name
    if not name:
        raise E2EError(f"cannot determine artifact filename from {source!r}")
    return name


def platform_key(asset_name: str, explicit: str | None, sniffed: str | None = None) -> str:
    """Map an artifact to the release checksum asset family."""
    if explicit:
        return explicit
    lower = asset_name.lower()
    if lower.endswith((".dmg", ".pkg")) or "macos" in lower or "darwin" in lower or "arm64" in lower:
        return "macos"
    if lower.endswith((".exe", ".msi")) or "windows" in lower or "win32" in lower:
        return "windows"
    if lower.endswith((".appimage", ".deb", ".rpm")) or "linux" in lower:
        return "linux"
    # Extensionless published assets (for example the Linux `xPST` binary) are
    # classified from their magic bytes instead of their name.
    if sniffed in {"elf"}:
        return "linux"
    if sniffed in {"pe"}:
        return "windows"
    if sniffed in {"macho"}:
        return "macos"
    raise E2EError(
        f"cannot infer checksum family for {asset_name}; use --platform macos|windows|linux"
    )


def derive_checksum_source(
    source: str,
    asset_name: str,
    platform: str,
    repo: str,
    release_tag: str | None,
) -> str:
    """Derive a release SHA256SUMS URL when the caller did not supply one."""
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme in {"http", "https"}:
        path = parsed.path
        marker = "/releases/download/"
        if marker not in path:
            raise E2EError(
                "an asset URL must be a GitHub release download URL or provide --checksums"
            )
        base = path.rsplit("/", 1)[0]
        return urllib.parse.urlunparse(parsed._replace(path=f"{base}/{platform}-SHA256SUMS", query="", fragment=""))
    if not release_tag:
        raise E2EError(
            "a local artifact needs --checksums or --release-tag so its release SHA256SUMS can be fetched"
        )
    return f"https://github.com/{repo}/releases/download/{release_tag}/{platform}-SHA256SUMS"


def checksum_source_candidates(
    source: str,
    asset_name: str,
    platform: str,
    repo: str,
    release_tag: str | None,
) -> list[str]:
    """Candidate checksum files for an artifact, most specific first.

    Releases have published both per-platform ``<platform>-SHA256SUMS`` and a
    single aggregate ``SHA256SUMS``. The Tauri lane publishes only the
    aggregate, so a missing per-platform file must fall back to the aggregate
    instead of aborting the install.
    """
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme in {"http", "https"}:
        path = parsed.path
        marker = "/releases/download/"
        if marker not in path:
            raise E2EError(
                "an asset URL must be a GitHub release download URL or provide --checksums"
            )
        base = path.rsplit("/", 1)[0]
        head = urllib.parse.urlunparse(parsed._replace(path=f"{base}/", query="", fragment=""))
        return [f"{head}{platform}-SHA256SUMS", f"{head}SHA256SUMS"]
    if not release_tag:
        raise E2EError(
            "a local artifact needs --checksums or --release-tag so its release SHA256SUMS can be fetched"
        )
    root = f"https://github.com/{repo}/releases/download/{release_tag}/"
    return [f"{root}{platform}-SHA256SUMS", f"{root}SHA256SUMS"]


def load_first_checksum_source(sources: list[str], destination: Path) -> tuple[str, str]:
    """Load the first checksum source that exists, reporting which one was used."""
    errors: list[str] = []
    for source in sources:
        try:
            return load_checksum_source(source, destination)
        except E2EError as exc:
            errors.append(str(exc))
    joined = "; ".join(errors) or "no candidate was checked"
    raise E2EError(f"no release checksum file could be loaded: {joined}")


def load_checksum_source(source: str, destination: Path) -> tuple[str, str]:
    """Load a local or remote checksum file and return text plus provenance."""
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme in {"http", "https"}:
        fetch_url(source, destination)
        provenance = source
    else:
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise E2EError(f"checksum file does not exist: {path}")
        shutil.copy2(path, destination)
        provenance = str(path)
    try:
        return destination.read_text(encoding="utf-8", errors="replace"), provenance
    except OSError as exc:
        raise E2EError(f"cannot read checksum file: {exc}") from exc


def parse_checksums(text: str) -> dict[str, str]:
    """Parse common sha256sum and shasum output forms."""
    entries: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([0-9a-fA-F]{64})\s+[* ]?(.+?)\s*$", line)
        if match:
            entries[Path(match.group(2)).name] = match.group(1).lower()
            continue
        match = re.match(r"^SHA256\s*\((.+)\)\s*=\s*([0-9a-fA-F]{64})\s*$", line, re.IGNORECASE)
        if match:
            entries[Path(match.group(1)).name] = match.group(2).lower()
    return entries


def sha256(path: Path) -> str:
    """Hash a file without loading a desktop image into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_checksum(
    asset: Path,
    checksum_text: str,
    checksum_source: str,
    checksum_asset: str | None = None,
    release_digest: str | None = None,
) -> dict[str, Any]:
    """Verify the bytes against the named release checksum.

    A local file may have been renamed after download.  In that case an
    explicit --checksum-asset is preferred; otherwise a single same-suffix
    entry is accepted, while ambiguous files fail closed.

    When the release's checksum file omits an asset entirely (this happened for
    the published v1.0.0 ``xPST-macos-arm64.zip``), the digest GitHub itself
    reports for that asset is accepted instead, and the verdict records that it
    came from the API rather than from the lane checksum file.
    """
    entries = parse_checksums(checksum_text)
    release_asset = checksum_asset or asset.name
    expected = entries.get(release_asset)
    verdict_source = "checksum-file"
    if expected is None and checksum_asset is None:
        candidates = [
            (name, digest)
            for name, digest in entries.items()
            if Path(name).suffix.lower() == asset.suffix.lower()
        ]
        if len(candidates) == 1:
            release_asset, expected = candidates[0]
    if expected is None and release_digest:
        expected = release_digest.lower()
        release_asset = checksum_asset or asset.name
        checksum_source = f"{checksum_source} (asset digest reported by the release API)"
        verdict_source = "release-api-digest"
    if expected is None:
        raise E2EError(f"{asset.name} is not listed in {checksum_source}; use --checksum-asset for a renamed local file")
    actual = sha256(asset)
    result = {
        "ok": actual == expected,
        "asset": asset.name,
        "release_asset": release_asset,
        "bytes": asset.stat().st_size,
        "expected_sha256": expected,
        "actual_sha256": actual,
        "checksum_source": checksum_source,
        "verdict_source": verdict_source,
    }
    if actual != expected:
        raise E2EError(
            f"checksum mismatch for {asset.name}: expected {expected}, got {actual}"
        )
    return result


def safe_extract_zip(archive: Path, destination: Path) -> None:
    """Extract a zip without allowing entries to escape the throwaway root.

    Zip entry permission bits are restored explicitly: a ``.app`` extracted
    without its executable bit cannot be launched, which would otherwise look
    like a broken published artifact instead of a harness bug.
    """
    destination = destination.resolve()
    try:
        with zipfile.ZipFile(archive) as handle:
            for member in handle.infolist():
                target = (destination / member.filename).resolve()
                if target != destination and destination not in target.parents:
                    raise E2EError(f"zip entry escapes install root: {member.filename}")
                handle.extract(member, destination)
                mode = member.external_attr >> 16
                if mode and target.exists() and not target.is_symlink():
                    os.chmod(target, mode & 0o7777)
    except zipfile.BadZipFile as exc:
        raise E2EError(f"invalid zip artifact {archive.name}: {exc}") from exc


def ensure_bundle_executable(app: Path) -> None:
    """Make a zip-extracted bundle launchable even if the zip lost its modes."""
    macos = app / "Contents" / "MacOS"
    if not macos.is_dir():
        return
    for path in macos.iterdir():
        if path.is_file():
            path.chmod(path.stat().st_mode | stat.S_IXUSR)


def host_binary_kind() -> str | None:
    """The executable format this host can actually run."""
    return {"darwin": "macho", "linux": "elf", "win32": "pe"}.get(sys.platform)


def cross_platform_hint(content_kind: str | None, host_kind: str | None) -> str:
    """Explain a launch failure caused by testing an artifact on the wrong OS."""
    if not content_kind or not host_kind or content_kind == host_kind:
        return ""
    return (
        f" — this published artifact is {content_kind} and this host is {host_kind}; "
        f"run the harness on {content_kind}'s own platform to complete a real launch smoke"
    )


def walk_app_bundles(root: Path, max_depth: int = 6) -> list[Path]:
    """Find ``.app`` directories without descending through symlinks.

    A macOS installer image carries a symlink to ``/Applications``; following it
    would scan the whole machine and could return an unrelated installed bundle
    instead of the one shipped in the artifact.
    """
    found: list[Path] = []
    queue: list[tuple[Path, int]] = [(root, 0)]
    while queue:
        directory, depth = queue.pop()
        if depth > max_depth:
            continue
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
            except OSError:
                continue
            if not entry.is_dir():
                continue
            if entry.name.lower().endswith(".app"):
                found.append(entry)
            else:
                queue.append((entry, depth + 1))
    return found


def find_app(root: Path) -> Path | None:
    """Find an application bundle, preferring xPST.app."""
    direct = sorted(root.glob("*.app"))
    recursive = walk_app_bundles(root)
    candidates = list(dict.fromkeys(direct + recursive))
    for candidate in candidates:
        if candidate.name.lower() == "xpst.app":
            return candidate
    return candidates[0] if candidates else None


def bundle_executable(app: Path) -> Path:
    """Resolve CFBundleExecutable, falling back to the first native binary."""
    info_path = app / "Contents" / "Info.plist"
    executable: str | None = None
    if info_path.is_file():
        try:
            info = plistlib.loads(info_path.read_bytes())
            value = info.get("CFBundleExecutable")
            executable = str(value) if value else None
        except (OSError, plistlib.InvalidFileException, ValueError):
            executable = None
    if executable:
        candidate = app / "Contents" / "MacOS" / executable
        if candidate.is_file():
            return candidate
    macos = app / "Contents" / "MacOS"
    candidates = sorted(path for path in macos.iterdir() if path.is_file()) if macos.is_dir() else []
    if not candidates:
        raise E2EError(f"no executable found in {app}/Contents/MacOS")
    return candidates[0]


def bundle_info(app: Path) -> dict[str, Any]:
    """Read public bundle identity fields."""
    info_path = app / "Contents" / "Info.plist"
    if not info_path.is_file():
        return {}
    try:
        info = plistlib.loads(info_path.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return {}
    fields = (
        "CFBundleIdentifier",
        "CFBundleName",
        "CFBundleDisplayName",
        "CFBundleVersion",
        "CFBundleShortVersionString",
        "CFBundleExecutable",
        "XPSTSourceCommit",
    )
    return {field: info.get(field) for field in fields if field in info}


def classify_bundle(app: Path) -> tuple[str, dict[str, bool]]:
    """Identify the Tauri shell versus a pre-Tauri PySide/QML desktop build."""
    resources = app / "Contents" / "Resources"
    tauri_markers = {
        "ui_index": (resources / "ui" / "index.html").is_file(),
        "engine": (resources / "binaries" / "engine" / "xpst-engine").is_file(),
    }
    legacy_markers = {
        # Pre-Tauri desktop bundles linked PySide6 and shipped the QML UI. This
        # marker is deliberately a property of the BUNDLE (a framework that a
        # Tauri build never contains) rather than a path inside the source
        # tree of an app this repository no longer builds, so the harness can
        # still name a wrong (legacy) published artifact.
        "pyside6": (app / "Contents" / "Frameworks" / "PySide6").exists(),
    }
    if all(tauri_markers.values()):
        return "tauri", {**tauri_markers, **legacy_markers}
    if any(legacy_markers.values()):
        return "legacy-pyside-qml", {**tauri_markers, **legacy_markers}
    return "unknown", {**tauri_markers, **legacy_markers}


def xattr_value(path: Path, name: str) -> str | None:
    """Read, but never remove or alter, one macOS extended attribute."""
    if sys.platform != "darwin" or shutil.which("xattr") is None:
        return None
    result = command_result(["xattr", "-p", name, str(path)])
    if result["returncode"] != 0:
        return None
    return str(result["stdout"]).strip() or None


def signature_assessment(app: Path) -> dict[str, Any]:
    """Run the requested macOS signing and Gatekeeper commands verbatim."""
    if sys.platform != "darwin":
        return {"checked": False, "reason": "macOS-only check"}
    codesign = command_result(["codesign", "-dv", "--verbose=4", str(app)])
    spctl = command_result(["spctl", "-a", "-vv", str(app)])
    return {
        "checked": True,
        "codesign_exit": codesign["returncode"],
        "codesign_stdout": codesign["stdout"],
        "codesign_stderr": codesign["stderr"],
        "spctl_exit": spctl["returncode"],
        "spctl_stdout": spctl["stdout"],
        "spctl_stderr": spctl["stderr"],
        "quarantine": xattr_value(app, "com.apple.quarantine"),
        "ad_hoc": "Signature=adhoc" in str(codesign["stderr"]),
        "gatekeeper_accepted": spctl["returncode"] == 0,
    }


def prepare_window_probe(work: Path) -> Path | None:
    """Compile a tiny CoreGraphics on-screen-window probe before app launch."""
    if sys.platform != "darwin" or shutil.which("swiftc") is None:
        return None
    source = work / "window_probe.swift"
    binary = work / "window_probe"
    source.write_text(
        """import CoreGraphics\nimport Foundation\nlet pid = Int32(CommandLine.arguments[1])!\nlet options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]\nlet windows = (CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]]) ?? []\nlet count = windows.filter { item in\n    guard let owner = item[kCGWindowOwnerPID as String] as? NSNumber else { return false }\n    return owner.int32Value == pid\n}.count\nprint(count)\n""",
        encoding="utf-8",
    )
    result = command_result(["swiftc", str(source), "-o", str(binary)], cwd=work, timeout=60)
    if result["returncode"] != 0 or not binary.is_file():
        return None
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


def visible_window(probe: Path | None, pid: int) -> tuple[bool, str]:
    """Return a measured on-screen-window result, never equating liveness with visibility."""
    if os.name == "nt":
        command = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"$p=Get-Process -Id {pid} -ErrorAction SilentlyContinue; if ($p -and $p.MainWindowHandle -ne 0) {{ '1' }} else {{ '0' }}",
        ]
        result = command_result(command, timeout=5)
        return str(result["stdout"]).strip() == "1", "Windows MainWindowHandle"
    if sys.platform == "linux" and shutil.which("xdotool"):
        result = command_result(["xdotool", "search", "--onlyvisible", "--pid", str(pid)], timeout=5)
        return result["returncode"] == 0 and bool(str(result["stdout"]).strip()), "xdotool visible window search"
    if probe is None:
        return False, "on-screen window probe unavailable on this platform"
    result = command_result([str(probe), str(pid)], timeout=5)
    if result["returncode"] != 0:
        return False, f"window probe failed: {short_command_error(result)}"
    try:
        return int(str(result["stdout"]).strip()) > 0, "CoreGraphics on-screen window count"
    except ValueError:
        return False, f"window probe returned non-numeric output: {result['stdout']!r}"


def read_log(path: Path, limit: int = 128 * 1024) -> str:
    """Read a bounded launch log while the process is running."""
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return data[-limit:]


def local_urls_from_log(text: str) -> list[str]:
    """Find loopback HTTP roots emitted by Tauri or an engine."""
    found: list[str] = []
    for match in re.finditer(r"https?://127\.0\.0\.1:\d+/?", text):
        url = match.group(0)
        if not url.endswith("/"):
            url += "/"
        if url not in found:
            found.append(url)
    for match in re.finditer(r"(?:engine port|dashboard port)\s*[:=]\s*(\d+)", text, re.IGNORECASE):
        url = f"http://127.0.0.1:{match.group(1)}/"
        if url not in found:
            found.append(url)
    return found


def http_get(url: str, timeout: float = 2.0) -> tuple[int | None, bytes, str | None]:
    """GET a loopback URL and retain HTTP status even for error responses."""
    request = urllib.request.Request(url, headers={"User-Agent": "xPST-stranger-install-e2e"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 - URL is a loopback probe built by this script
            return response.status, response.read(2 * 1024 * 1024), None
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(2 * 1024 * 1024)
        except OSError:
            body = b""
        return exc.code, body, str(exc)
    except (OSError, urllib.error.URLError) as exc:
        return None, b"", str(exc)


def packaged_ui_sample(url: str, timeout: float = 2.0) -> dict[str, Any]:
    """Take one packaged-HTML sample of a loopback root.

    The assertion is deliberately strict and unchanged: HTTP 200, an ``<html``
    document and an xPST marker.  Anything else is a miss, which the caller is
    allowed to retry a bounded number of times.
    """
    status, body, error = http_get(url, timeout=timeout)
    decoded = body.decode("utf-8", errors="replace")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", decoded, re.IGNORECASE | re.DOTALL)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
    ok = (
        status == 200
        and "<html" in decoded.lower()
        and ("xPST" in decoded or "xpst" in decoded.lower())
    )
    return {
        "ok": ok,
        "status": status,
        "title": title,
        "reason": (
            "packaged HTML served at the healthy loopback root"
            if ok
            else f"root response was not the packaged HTML UI (status={status}, error={error})"
        ),
    }


def observe_packaged_ui(
    url: str,
    *,
    window_seconds: float = PACKAGED_UI_RETRY_WINDOW_SECONDS,
    poll_interval: float = OBSERVATION_POLL_INTERVAL_SECONDS,
    sampler: Callable[[str], dict[str, Any]] | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Re-sample the packaged UI over a bounded window before declaring a miss.

    A macOS Tauri shell re-execs during startup, and its engine can answer
    ``/health`` plus a root request before the packaged index is what the shell
    serves.  One sample taken in that instant reads a healthy app as broken.
    The window is short and bounded (``PACKAGED_UI_RETRY_WINDOW_SECONDS``) and
    the sample is still the same strict check; only exhaustion fails.
    """
    sample_once = sampler or packaged_ui_sample
    pause = sleep or time.sleep
    now = clock or time.monotonic
    deadline = now() + max(0.0, window_seconds)
    attempts = 0
    last: dict[str, Any] = {"ok": False, "status": None, "title": "", "reason": "not sampled"}
    while True:
        attempts += 1
        last = sample_once(url)
        if last.get("ok") or now() >= deadline:
            break
        pause(poll_interval)
    ok = bool(last.get("ok"))
    reason = str(last.get("reason") or "not sampled")
    if not ok:
        reason = f"{reason} (retried {attempts} time(s) over {window_seconds:.1f}s)"
    return {
        "ok": ok,
        "status": last.get("status"),
        "title": str(last.get("title") or ""),
        "reason": reason,
        "attempts": attempts,
        "retry_window_seconds": window_seconds,
        "attempts_exhausted": not ok,
    }


def health_and_ui(
    log_path: Path,
    process: subprocess.Popen[Any],
    start: float,
    health_timeout: float,
    visible_budget: float,
    probe: Path | None,
    require_visible: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
    """Poll visibility and /health, then assert the HTML UI at the same root."""
    visible = False
    visible_seconds: float | None = None
    visible_reason = "not observed"
    health_status: int | None = None
    health_url: str | None = None
    observed_urls: list[str] = []
    deadline = start + health_timeout
    while time.monotonic() < deadline:
        if not visible and process.poll() is None:
            visible, visible_reason = visible_window(probe, process.pid)
            if visible:
                visible_seconds = time.monotonic() - start
        text = read_log(log_path)
        for url in local_urls_from_log(text):
            if url not in observed_urls:
                observed_urls.append(url)
        for base in observed_urls:
            status, _body, _error = http_get(urllib.parse.urljoin(base, "health"))
            if status is not None:
                health_status = status
            if status == 200:
                health_url = base
                break
        if health_url and (visible or not require_visible):
            break
        if process.poll() is not None:
            break
        time.sleep(0.2)

    if not visible:
        visible_seconds = None
    ui_status: int | None = None
    ui_title = ""
    ui_ok = False
    ui_attempts = 0
    ui_window = 0.0
    ui_reason = "health endpoint did not return HTTP 200"
    if health_url:
        observation = observe_packaged_ui(health_url)
        ui_ok = bool(observation["ok"])
        ui_status = observation["status"]
        ui_title = str(observation["title"])
        ui_attempts = int(observation["attempts"])
        ui_window = float(observation["retry_window_seconds"])
        ui_reason = str(observation["reason"])

    window_ok = visible and visible_seconds is not None and visible_seconds <= visible_budget
    if require_visible:
        visible_ok = window_ok
        if not window_ok:
            visible_reason = (
                f"visible after {visible_seconds:.3f}s, over budget {visible_budget:.3f}s"
                if visible and visible_seconds is not None
                else visible_reason
            )
    else:
        visible_ok = True
        visible_reason = (
            "on-screen-window assertion skipped by --no-require-visible-window; "
            "this run is not release evidence"
        )
    boot = {
        "ok": visible_ok,
        "required": require_visible,
        "window_observed": visible,
        "boot_to_visible_seconds": visible_seconds,
        "budget_seconds": visible_budget,
        "window_probe": visible_reason,
    }
    health = {
        "ok": health_status == 200,
        "status": health_status,
        "url": health_url,
        "observed_loopback_urls": observed_urls,
        "timeout_seconds": health_timeout,
        "process_exit_code_during_poll": process.poll(),
    }
    ui = {
        "ok": ui_ok,
        "status": ui_status,
        "title": ui_title,
        "url": health_url,
        "reason": ui_reason,
        "attempts": ui_attempts,
        "retry_window_seconds": ui_window,
    }
    failures: list[str] = []
    if not visible_ok:
        failures.append(f"boot-to-visible failed: {visible_reason}")
    if health_status != 200:
        failures.append(
            f"engine /health did not return HTTP 200 within {health_timeout:.1f}s "
            f"(last_status={health_status}, urls={observed_urls or 'none'})"
        )
    if not ui_ok:
        failures.append(f"packaged UI assertion failed: {ui_reason}")
    return boot, health, ui, failures


def linux_proc_cmdlines() -> list[tuple[str, str]]:
    """Read every visible /proc/<pid>/cmdline (Linux), dependency-free.

    Used as a fallback because GNU ``ps`` truncates its command column to the
    terminal width, which can hide an ``xpst-engine`` path in a long throwaway
    directory name and make a live sidecar look absent.
    """
    entries: list[tuple[str, str]] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return entries
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        command = " ".join(part for part in raw.decode("utf-8", "replace").split("\x00") if part)
        if command:
            entries.append((entry.name, command))
    return entries


def engine_processes() -> list[dict[str, str]]:
    """List real xpst-engine executable processes, not shell text containing the name."""
    if os.name == "nt":
        result = command_result(["tasklist", "/FO", "CSV", "/NH"])
        if result["returncode"] != 0:
            return []
        processes: list[dict[str, str]] = []
        for line in str(result["stdout"]).splitlines():
            if "xpst-engine.exe" in line.lower():
                fields = [field.strip('"') for field in line.split('","')]
                pid = fields[1] if len(fields) > 1 else "unknown"
                processes.append({"pid": pid, "command": line})
        return processes
    candidates: list[tuple[str, str]] = []
    result = command_result(["ps", "-ww", "-axo", "pid=,command="])
    if result["returncode"] == 0:
        for line in str(result["stdout"]).splitlines():
            match = re.match(r"\s*(\d+)\s+(.*)$", line)
            if match:
                candidates.append((match.group(1), match.group(2)))
    pattern = re.compile(r"(?:^|/)xpst-engine(?:\.exe)?(?:\s|$)", re.IGNORECASE)
    processes = [
        {"pid": pid, "command": command}
        for pid, command in candidates
        if pattern.search(command)
    ]
    if not processes and sys.platform.startswith("linux"):
        for pid, command in linux_proc_cmdlines():
            if pattern.search(command):
                processes.append({"pid": pid, "command": command})
    return processes


def observe_engine_sidecar(
    *,
    exclude_pids: set[str] | None = None,
    window_seconds: float = SIDECAR_RETRY_WINDOW_SECONDS,
    poll_interval: float = OBSERVATION_POLL_INTERVAL_SECONDS,
    enumerate_processes: Callable[[], list[dict[str, str]]] | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Re-enumerate ``xpst-engine`` processes over a bounded window.

    On macOS the Tauri shell re-execs and the engine is a short-lived child
    during that window; a single ``ps`` taken right after the first ``/health``
    200 can list the app as ``(xPST)`` and no sidecar at all (run 42508682981).
    The requirement is unchanged — a tauri stack still needs a real
    ``xpst-engine`` process — but the harness re-enumerates for a short bounded
    window (``SIDECAR_RETRY_WINDOW_SECONDS``) and fails only on exhaustion.
    Processes that were already running before launch (``exclude_pids``) belong
    to a foreign install and are not evidence for this run.
    """
    list_processes = enumerate_processes or engine_processes
    pause = sleep or time.sleep
    now = clock or time.monotonic
    excluded = exclude_pids or set()
    deadline = now() + max(0.0, window_seconds)
    attempts = 0
    observed: list[dict[str, str]] = []
    while True:
        attempts += 1
        observed = [entry for entry in list_processes() if entry.get("pid") not in excluded]
        if observed or now() >= deadline:
            break
        pause(poll_interval)
    return {
        "processes": observed,
        "observed": bool(observed),
        "attempts": attempts,
        "retry_window_seconds": window_seconds,
        "attempts_exhausted": not observed,
    }


def terminate_process(process: subprocess.Popen[Any]) -> dict[str, Any]:
    """Stop the app and its process group without touching unrelated processes."""
    if process.poll() is None:
        try:
            if os.name == "nt":
                tree_stop = command_result(["taskkill", "/PID", str(process.pid), "/T", "/F"], timeout=15)
                if tree_stop["returncode"] != 0:
                    process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    try:
        returncode = process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        returncode = process.wait(timeout=10)
    return {"pid": process.pid, "exit_code": returncode}


def work_processes(work: Path) -> list[dict[str, str]]:
    """List processes whose command line points into this run's throwaway dir.

    A portable AppImage self-extracts and its WebKit helpers are started by the
    shell, not by the process the harness launched directly, so they can outlive
    the process-group kill: the runner had to terminate orphaned
    ``xPST_1.2.1_amd64.AppImage`` and ``WebKitWebProcess`` processes *after* the
    job, and while they lived they kept writing into the run's isolated HOME and
    held its extracted mount point, which is why the teardown could not delete
    the work directory. Matching on the unique throwaway path is what makes the
    sweep safe: it can only ever hit processes this run started.
    """
    marker = str(work)
    own = os.getpid()
    candidates: list[tuple[str, str]] = []
    result = command_result(["ps", "-ww", "-axo", "pid=,command="])
    if result["returncode"] == 0:
        for line in str(result["stdout"]).splitlines():
            match = re.match(r"\s*(\d+)\s+(.*)$", line)
            if match:
                candidates.append((match.group(1), match.group(2)))
    if sys.platform.startswith("linux"):
        candidates.extend(linux_proc_cmdlines())
    processes: list[dict[str, str]] = []
    seen: set[str] = set()
    for pid, command in candidates:
        if pid in seen or not pid.isdigit() or int(pid) == own:
            continue
        if marker not in command:
            continue
        seen.add(pid)
        processes.append({"pid": pid, "command": command})
    return processes


def stop_work_processes(work: Path, settle_seconds: float = 5.0) -> list[dict[str, str]]:
    """Stop processes this run left behind, escalating TERM then KILL."""
    stopped = work_processes(work)
    if not stopped:
        return []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for entry in work_processes(work):
            try:
                os.kill(int(entry["pid"]), sig)
            except (OSError, ProcessLookupError):
                pass
        deadline = time.monotonic() + settle_seconds
        while time.monotonic() < deadline:
            if not work_processes(work):
                return stopped
            time.sleep(0.1)
    return stopped


# ---------------------------------------------------------------------------
# Real installer payloads.
#
# A .deb and a Windows NSIS/WiX installer are *installers*: there is no product
# to boot until one has run. Copying either file and exec'ing it launches the
# installer, not the app — the v1.2.1 windows run did exactly that, watched the
# setup wizard's window, and never saw an engine sidecar or a /health answer.
# The readers below are stdlib-only so the harness stays dependency-free.
# ---------------------------------------------------------------------------

AR_MAGIC = b"!<arch>\n"
NSIS_MARKER = b"Nullsoft"


def ar_members(path: Path) -> dict[str, bytes]:
    """Return the members of a GNU/BSD ``ar`` archive (the outer .deb container)."""
    raw = path.read_bytes()
    if not raw.startswith(AR_MAGIC):
        raise E2EError(f"{path.name} is not an ar archive, so it is not a .deb")
    members: dict[str, bytes] = {}
    offset = len(AR_MAGIC)
    while offset + 60 <= len(raw):
        header = raw[offset : offset + 60]
        name = header[:16].decode("utf-8", "replace").strip()
        try:
            size = int(header[48:58].decode("ascii", "replace").strip() or "0")
        except ValueError:
            break
        offset += 60
        members[name.rstrip("/")] = raw[offset : offset + size]
        offset += size + (size % 2)
    return members


def tar_entries(payload: bytes) -> list[tuple[str, int, bool]]:
    """List ``(path, mode, is_file)`` for a (possibly compressed) tar payload."""
    import io
    import tarfile

    entries: list[tuple[str, int, bool]] = []
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
        for member in archive.getmembers():
            name = member.name
            while name.startswith("./"):
                name = name[2:]
            entries.append((name, member.mode, member.isfile()))
    return entries


def deb_install_record(deb: Path) -> dict[str, Any]:
    """Read a .deb's package identity and the executable it installs.

    Returns ``package``, ``version``, ``depends`` and ``executable`` (the
    absolute path the package installs, for example ``/usr/bin/xPST``).
    """
    members = ar_members(deb)
    control_name = next((name for name in members if name.startswith("control.tar")), None)
    data_name = next((name for name in members if name.startswith("data.tar")), None)
    if control_name is None or data_name is None:
        raise E2EError(f"{deb.name} has no control.tar*/data.tar* member (not a .deb)")
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(members[control_name]), mode="r:*") as archive:
        handle = None
        for member_name in ("control", "./control"):
            try:
                handle = archive.extractfile(member_name)
            except KeyError:
                continue
            if handle is not None:
                break
        control = handle.read().decode("utf-8", "replace") if handle else ""
    data_entries = tar_entries(members[data_name])
    fields: dict[str, str] = {}
    for line in control.splitlines():
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            fields[key.strip().lower()] = value.strip()
    executables = [
        "/" + name
        for name, mode, is_file in data_entries
        if is_file and mode & 0o111 and name.startswith("usr/bin/")
    ]
    if not executables:
        raise E2EError(f"{deb.name} installs no executable under usr/bin")
    preferred = [path for path in executables if path.lower().endswith(("/xpst", "/xpst.exe"))]
    record = {
        "package": fields.get("package", ""),
        "version": fields.get("version", ""),
        "depends": [part.strip() for part in fields.get("depends", "").split(",") if part.strip()],
        "executable": (preferred or sorted(executables))[0],
        "files": len([entry for entry in data_entries if entry[2]]),
        "control_member": control_name,
        "data_member": data_name,
    }
    print(
        f"[e2e] .deb identity: package={record['package']!r} version={record['version']!r} "
        f"executable={record['executable']} depends={record['depends']}",
        flush=True,
    )
    return record


def elevated_prefix() -> list[str]:
    """Return the prefix needed to install a system package, or [] if unusable."""
    if os.name == "nt":
        return []
    try:
        if os.geteuid() == 0:
            return []
    except AttributeError:  # pragma: no cover - non-POSIX hosts
        return []
    return ["sudo", "-n"] if shutil.which("sudo") else []


def install_deb(deb: Path, work: Path) -> tuple[Path, dict[str, Any]]:
    """Install a published .deb the way a stranger on Debian/Ubuntu would.

    ``apt-get install ./package.deb`` resolves the package's own declared
    ``Depends`` (the GTK/WebKit stack a Tauri bundle needs), which is exactly
    what a headless runner lacks. If apt cannot be used the payload is
    unpacked into the throwaway root with ``dpkg-deb -x`` and the skipped
    dependency resolution is reported as a finding instead of being silently
    assumed.
    """
    record = deb_install_record(deb)
    prefix = elevated_prefix()
    findings: list[str] = []
    method = "apt-get install"
    attempts: list[dict[str, Any]] = []
    if shutil.which("apt-get") is None:
        method = "dpkg-deb -x extraction"
    else:
        command = [*prefix, "apt-get", "install", "-y", str(deb)]
        result = command_result(command, timeout=1800)
        attempts.append({"command": command, "returncode": result["returncode"]})
        detail = short_command_error(result)
        if result["returncode"] != 0 and (
            "Unable to locate package" in detail or "no installation candidate" in detail
        ):
            refresh = [*prefix, "apt-get", "update", "-qq"]
            refreshed = command_result(refresh, timeout=1800)
            attempts.append({"command": refresh, "returncode": refreshed["returncode"]})
            if refreshed["returncode"] == 0:
                result = command_result(command, timeout=1800)
                attempts.append({"command": command, "returncode": result["returncode"]})
        if result["returncode"] != 0:
            method = "dpkg-deb -x extraction"
            findings.append(
                "apt-get could not install the published .deb "
                f"({short_command_error(result)}); the payload was unpacked with dpkg-deb -x, so "
                "the package's declared Depends were NOT resolved and a missing desktop library "
                "will surface as a launch failure rather than as an install step."
            )
    if method == "apt-get install":
        installed = Path(record["executable"])
        if not installed.exists():
            raise E2EError(
                f"apt-get reported success but the package executable {installed} is missing"
            )
        uninstall: dict[str, Any] = {
            "kind": "apt",
            "package": record["package"],
            "command": [*prefix, "apt-get", "remove", "-y", record["package"]],
        }
    else:
        if shutil.which("dpkg-deb") is None:
            raise E2EError("neither apt-get nor dpkg-deb is available to install a .deb")
        root = work / "install" / "deb-root"
        root.mkdir(parents=True, exist_ok=True)
        extracted = command_result(["dpkg-deb", "-x", str(deb), str(root)], timeout=1200)
        if extracted["returncode"] != 0:
            raise E2EError(f"dpkg-deb -x failed: {short_command_error(extracted)}")
        installed = root / record["executable"].lstrip("/")
        if not installed.exists():
            raise E2EError(f"dpkg-deb -x did not produce {installed}")
        installed.chmod(installed.stat().st_mode | stat.S_IXUSR)
        uninstall = {"kind": "dir", "path": str(root)}
    return installed, {
        "method": method,
        "path": str(installed),
        "package": record,
        "attempts": attempts,
        "uninstall": uninstall,
        "findings": findings,
    }


def is_nsis_installer(path: Path) -> bool:
    """True when the installer is an NSIS package (Tauri's Windows .exe bundle)."""
    try:
        with path.open("rb") as handle:
            return NSIS_MARKER in handle.read(1024 * 1024)
    except OSError:
        return False


def product_executable(root: Path) -> Path | None:
    """Find the bootable product executable inside an installed Windows tree."""
    if not root.is_dir():
        return None
    candidates = [
        path
        for path in root.rglob("*.exe")
        if path.is_file() and not path.name.lower().startswith("uninstall")
    ]
    if not candidates:
        return None
    preferred = [path for path in candidates if path.stem.lower().startswith("xpst")]
    pool = preferred or candidates
    return sorted(pool, key=lambda path: (len(path.parts), -path.stat().st_size, str(path)))[0]


def install_windows_installer(installer: Path, work: Path) -> tuple[Path, dict[str, Any]]:
    """Silently install a published Windows installer and return the product exe.

    NSIS takes ``/S`` (silent) and ``/D=<dir>`` (default install directory,
    unquoted and last). WiX packages are unpacked administratively with
    ``msiexec /a``, which needs no elevation and leaves a runnable tree.
    """
    target = work / "install" / "app"
    target.mkdir(parents=True, exist_ok=True)
    if installer.suffix.lower() == ".msi":
        command = ["msiexec", "/a", str(installer), "/qn", f"TARGETDIR={target}"]
        method = "msiexec /a administrative extraction"
    else:
        command = [str(installer), "/S", f"/D={target}"]
        method = "silent NSIS install (/S)"
    print(f"[e2e] installer command: {' '.join(command)}", flush=True)
    result = command_result(command, timeout=1800)
    if result["returncode"] != 0:
        raise E2EError(f"installer failed: {short_command_error(result)}")
    executable = product_executable(target)
    if executable is None:
        raise E2EError(f"the installer exited 0 but no product .exe exists under {target}")
    uninstaller = next(
        (path for path in target.rglob("uninstall*.exe")),
        None,
    )
    return executable, {
        "method": method,
        "path": str(executable),
        "install_root": str(target),
        "installer_command": command,
        "installer_stdout_tail": str(result.get("stdout", ""))[-1000:],
        "uninstall": {
            "kind": "dir",
            "path": str(target),
            "uninstaller": str(uninstaller) if uninstaller else None,
        },
        "findings": [],
    }


def display_available() -> bool:
    """True when this host has an X11 or Wayland display for a GUI app to use."""
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def headless_display_prefix() -> list[str]:
    """Wrap a GTK/Tauri launch in ``xvfb-run`` on a Linux host with no display.

    A Tauri/GTK app aborts on a headless host with "Failed to initialize GTK",
    which the v1.2.1 linux run reported only as "no /health in 90s". CI gets a
    display from xvfb; a developer machine already has one and needs nothing.
    """
    if not sys.platform.startswith("linux") or display_available():
        return []
    runner = shutil.which("xvfb-run")
    return [runner, "-a", "--"] if runner else []


def install_artifact(artifact: Path, artifact_type: str, work: Path) -> tuple[Path, dict[str, Any]]:
    """Mount/extract/install an artifact into the throwaway install root."""
    install_root = work / "install"
    install_root.mkdir(parents=True, exist_ok=True)
    if artifact_type == "deb":
        return install_deb(artifact, work)
    if artifact_type == "msi":
        return install_windows_installer(artifact, work)
    if artifact_type == "exe" and os.name == "nt":
        if is_nsis_installer(artifact):
            return install_windows_installer(artifact, work)
        print(
            "[e2e] the .exe is not an NSIS installer; launching it as the product itself",
            flush=True,
        )
    if artifact_type == "dmg":
        if sys.platform != "darwin":
            raise E2EError(".dmg installation requires macOS (hdiutil is unavailable)")
        attach = command_result(["hdiutil", "attach", "-readonly", "-nobrowse", "-plist", str(artifact)])
        if attach["returncode"] != 0:
            raise E2EError(f"DMG mount failed: {short_command_error(attach)}")
        mount_points: list[str] = []
        try:
            try:
                plist = plistlib.loads(str(attach["stdout"]).encode())
            except (plistlib.InvalidFileException, ValueError) as exc:
                raise E2EError(f"hdiutil returned invalid mount plist: {exc}") from exc
            mount_points = [
                str(entity["mount-point"])
                for entity in plist.get("system-entities", [])
                if entity.get("mount-point")
            ]
            if not mount_points:
                raise E2EError("hdiutil mounted the DMG but returned no mount point")
            mount_point = Path(mount_points[0])
            app = find_app(mount_point)
            if app is None:
                raise E2EError(f"DMG contains no .app bundle: {mount_point}")
            destination = install_root / app.name
            ditto = shutil.which("ditto")
            if ditto:
                copied = command_result([ditto, str(app), str(destination)], timeout=180)
                if copied["returncode"] != 0:
                    raise E2EError(f"app copy failed: {short_command_error(copied)}")
            else:
                shutil.copytree(app, destination, symlinks=True)
        finally:
            if mount_points:
                detached = command_result(["hdiutil", "detach", mount_points[0]])
                if detached["returncode"] != 0:
                    raise E2EError(f"DMG detach failed: {short_command_error(detached)}")
        return destination, {"method": "hdiutil read-only mount + app copy", "path": str(destination)}
    if artifact_type == "zip":
        extracted = install_root / "extracted"
        extracted.mkdir()
        safe_extract_zip(artifact, extracted)
        app = find_app(extracted)
        if app:
            destination = install_root / app.name
            shutil.copytree(app, destination, symlinks=True)
            ensure_bundle_executable(destination)
            return destination, {"method": "zip extraction + app copy", "path": str(destination)}
        executable_candidates = sorted(
            path
            for path in extracted.rglob("*")
            if path.is_file() and path.suffix.lower() in {".exe", ".appimage", ""}
        )
        if not executable_candidates:
            raise E2EError("zip contains no .app, .exe, .AppImage, or extensionless executable")
        source = next((p for p in executable_candidates if p.name.lower().startswith("xpst")), executable_candidates[0])
        destination = install_root / source.name
        shutil.copy2(source, destination)
        destination.chmod(destination.stat().st_mode | stat.S_IXUSR)
        return destination, {"method": "zip extraction + executable copy", "path": str(destination)}
    if artifact_type in {"exe", "appimage", "binary"}:
        destination = install_root / artifact.name
        shutil.copy2(artifact, destination)
        if artifact_type in {"appimage", "binary"}:
            destination.chmod(destination.stat().st_mode | stat.S_IXUSR)
        return destination, {"method": "throwaway executable copy", "path": str(destination)}
    raise E2EError(f"unsupported artifact type: {artifact_type}")


def artifact_type(name: str, artifact: Path | None = None) -> str:
    """Return the supported installer kind, falling back to magic bytes."""
    lower = name.lower()
    if lower.endswith(".dmg"):
        return "dmg"
    if lower.endswith(".zip"):
        return "zip"
    if lower.endswith(".exe"):
        return "exe"
    if lower.endswith(".msi"):
        return "msi"
    if lower.endswith(".deb"):
        return "deb"
    if lower.endswith(".appimage"):
        return "appimage"
    if artifact is not None:
        sniffed = sniff_artifact_kind(artifact)
        if sniffed == "zip":
            return "zip"
        if sniffed == "pe":
            return "exe"
        if sniffed in {"elf", "macho"}:
            return "binary"
    raise E2EError(
        "artifact must be a .dmg, .zip, .exe, or .AppImage, or a published "
        "extensionless ELF/Mach-O executable (for example the Linux `xPST` asset)"
    )


def launch_environment(work: Path, config_dir: Path, home_dir: Path) -> dict[str, str]:
    """Build the clean-profile environment used by the stranger launch."""
    temp_dir = work / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home_dir),
            "USERPROFILE": str(home_dir),
            "APPDATA": str(home_dir / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(home_dir / "AppData" / "Local"),
            "XDG_CONFIG_HOME": str(home_dir / ".config"),
            "XDG_DATA_HOME": str(home_dir / ".local" / "share"),
            "XDG_CACHE_HOME": str(home_dir / ".cache"),
            "TMPDIR": str(temp_dir),
            "TEMP": str(temp_dir),
            "TMP": str(temp_dir),
            "XPST_CONFIG_DIR": str(config_dir),
            "XPST_NO_KEYRING": "1",
        }
    )
    return env


def first_run_findings(
    stack: str,
    config_dir: Path,
    home_dir: Path,
    signature: dict[str, Any],
    health: dict[str, Any],
    ui: dict[str, Any],
    log_text: str,
) -> list[str]:
    """Turn measured results into stranger-facing findings without guessing."""
    findings: list[str] = []
    home_state = home_dir / ".xpst"
    config_files = any(path.is_file() for path in config_dir.rglob("*")) if config_dir.exists() else False
    home_files = any(path.is_file() for path in home_state.rglob("*")) if home_state.exists() else False
    if home_files and not config_files:
        findings.append(
            "XPST_CONFIG_DIR was ignored by the published app: first-run state was written under HOME/.xpst instead."
        )
    if stack == "legacy-pyside-qml":
        findings.append(
            "The published app is the legacy PySide6/QML stack; it has no packaged Tauri ui/index.html or xpst-engine sidecar."
        )
    if health["status"] != 200:
        findings.append("No engine /health HTTP 200 was observed during the health budget.")
    if not ui["ok"]:
        findings.append("The packaged UI was not served as an HTTP UI by the launched artifact.")
    if signature.get("ad_hoc"):
        findings.append("The app is ad-hoc signed, not Developer ID signed; TeamIdentifier is not set.")
    if signature.get("checked") and not signature.get("gatekeeper_accepted"):
        findings.append("spctl rejected the app, so Gatekeeper acceptance was not proven.")
    if signature.get("quarantine"):
        findings.append(
            "The artifact carries com.apple.quarantine; macOS may block launch until the user uses Finder's Open/Privacy & Security approval path."
        )
    if "no resource dir" in log_text:
        findings.append(
            "The published shell logged 'FATAL: no resource dir' and never started the engine. "
            "This is observed when the .app is launched through a symlinked path component in its "
            "un-resolved form (/tmp -> /private/tmp, /var -> /private/var): the bundle depends on "
            "Tauri's resource_dir() resolving there, and it returns UnknownPath. Launch the app "
            "from a canonical path (resolve the install location first)."
        )
    error_markers = [
        line.strip()
        for line in log_text.splitlines()
        if re.search(r"Traceback|FATAL|Failed to load QML|cannot start|ImportError|ModuleNotFoundError", line, re.IGNORECASE)
    ]
    if error_markers:
        findings.append(f"Launch log contained {len(error_markers)} fatal/import/UI error line(s).")
    return findings


# ---------------------------------------------------------------------------
# Published-release resolution
# ---------------------------------------------------------------------------


def http_get_text(url: str, *, accept: str | None = None) -> str:
    """GET a public URL and return its body as text.

    An optional ``GH_TOKEN``/``GITHUB_TOKEN`` (as CI provides) is sent when
    present so the unauthenticated GitHub API limit is not the only path.
    """
    headers = {"User-Agent": "xPST-stranger-install-e2e"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if accept:
        headers["Accept"] = accept
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310 - public release URL built by this script
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace").strip()
        except OSError:
            body = ""
        detail = f": {body[:200]}" if body else ""
        raise E2EError(f"HTTP {exc.code} for {url}{detail}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise E2EError(f"request failed for {url}: {exc}") from exc


def github_api_json(url: str) -> Any:
    """GET a public GitHub API URL without a third-party client.

    Sends ``GH_TOKEN``/``GITHUB_TOKEN`` when the environment provides one; the
    caller falls back to the public release page when the unauthenticated
    rate limit (60/hour) is exhausted.
    """
    headers = {
        "User-Agent": "xPST-stranger-install-e2e",
        "Accept": "application/vnd.github+json",
    }
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310 - public release URL built by this script
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace").strip()
        except OSError:
            body = ""
        detail = f": {body[:200]}" if body else ""
        raise E2EError(f"GitHub API returned HTTP {exc.code} for {url}{detail}") from exc
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise E2EError(f"GitHub API request failed for {url}: {exc}") from exc


def default_platform() -> str:
    """Return the platform key for the machine running the harness."""
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt":
        return "windows"
    return "linux"


#: Installer basenames the release lanes actually publish, per platform.
PUBLISHED_ASSET_NAMES = {
    "macos": ("xpst.dmg", "xpst-macos-arm64.zip", "xpst-macos.zip", "xpst.pkg"),
    "windows": ("xpst.exe", "xpst.msi"),
    "linux": ("xpst", "xpst.appimage", "xpst.deb", "xpst.rpm"),
}

#: Installer extensions per platform, used to recognise the Tauri asset naming
#: (``xPST_<version>_<arch>.<ext>``) that the published releases actually use.
PLATFORM_ASSET_SUFFIXES = {
    "macos": (".dmg", ".pkg"),
    "windows": (".exe", ".msi"),
    "linux": (".appimage", ".deb", ".rpm"),
}

#: Rank order when a release publishes more than one installer for a platform.
#: The installable package (`.deb`, NSIS `.exe`) outranks the portable copy:
#: an AppImage needs FUSE *and* the distro's GTK/WebKit stack, so on a headless
#: runner it fails before it can boot, which says nothing about the product.
ASSET_SUFFIX_PREFERENCE = {
    "macos": (".dmg", ".pkg", ".zip"),
    "windows": (".exe", ".msi"),
    "linux": (".deb", ".rpm", ".appimage"),
}


def asset_preference_score(name: str, platform: str) -> int:
    """Rank an installer asset by how well it represents a stranger install."""
    suffixes = ASSET_SUFFIX_PREFERENCE.get(platform, ())
    lower = name.lower()
    for rank, suffix in enumerate(suffixes):
        if lower.endswith(suffix):
            return len(suffixes) - rank
    return 0


#: Architecture tokens, most specific first, so a host prefers its own build.
ARCH_ALIASES = {
    "arm64": ("aarch64", "arm64"),
    "aarch64": ("aarch64", "arm64"),
    "x86_64": ("x86_64", "x64", "amd64"),
    "amd64": ("x86_64", "x64", "amd64"),
}


def host_arch() -> str:
    """Return the host CPU token used to rank architecture-specific assets."""
    return host_platform.machine().lower()


def architecture_match_score(name: str, arch: str) -> int:
    """Rank how well an asset name matches the host architecture (higher wins)."""
    aliases = ARCH_ALIASES.get(arch)
    if not aliases:
        return 0
    lower = name.lower()
    for rank, alias in enumerate(aliases):
        if alias in lower:
            return len(aliases) - rank
    return 0


def installer_candidates(
    assets: list[dict[str, Any]], platform: str
) -> list[dict[str, Any]]:
    """Return release assets that are installers for ``platform``.

    This is the fallback for releases whose assets follow the Tauri
    ``xPST_<version>_<arch>.<ext>`` naming instead of the flat names in
    :data:`PUBLISHED_ASSET_NAMES`. Updater sidecars and signature files are
    excluded so a ``.sig`` can never be mistaken for the installer.
    """
    suffixes = PLATFORM_ASSET_SUFFIXES.get(platform, ())
    candidates: list[dict[str, Any]] = []
    for asset in assets:
        name = str(asset.get("name", ""))
        lower = name.lower()
        if not suffixes or not lower.endswith(suffixes):
            continue
        if lower.endswith(".sig") or ".tar" in lower:
            continue
        candidates.append(asset)
    return candidates


def release_assets_from_html(repo: str, tag: str) -> list[dict[str, Any]]:
    """List release assets from the public asset page, with no API quota.

    GitHub renders ``/releases/expanded_assets/<tag>`` as a static fragment that
    links every asset. This is the fallback a stranger with no token can use when
    the unauthenticated API limit (60/hour) is exhausted.
    """
    url = f"https://github.com/{repo}/releases/expanded_assets/{urllib.parse.quote(tag)}"
    html = http_get_text(url)
    assets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in re.finditer(r'href="(/[^"]+/releases/download/[^"]+)"', html):
        path = match.group(1)
        name = Path(urllib.parse.unquote(path)).name
        if not name or name in seen:
            continue
        seen.add(name)
        assets.append({"name": name, "browser_download_url": f"https://github.com{path}"})
    if not assets:
        raise E2EError(f"could not list any asset for release {tag} in {repo}")
    return assets


def release_assets(repo: str, tag: str) -> list[dict[str, Any]]:
    """Return the asset records of one *published* GitHub release."""
    url = f"https://api.github.com/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}"
    try:
        payload = github_api_json(url)
    except E2EError as exc:
        assets = release_assets_from_html(repo, tag)
        print(
            f"[e2e] GitHub API unusable ({exc}); listed {len(assets)} assets from the "
            "public release page instead (no asset size/digest cross-check)",
            flush=True,
        )
        return assets
    assets = payload.get("assets") if isinstance(payload, dict) else None
    if not isinstance(assets, list) or not assets:
        raise E2EError(f"published release {tag} in {repo} has no assets")
    return [asset for asset in assets if isinstance(asset, dict)]


def select_release_asset(
    assets: list[dict[str, Any]], platform: str, asset_name: str | None = None
) -> dict[str, Any]:
    """Pick the installer a stranger on ``platform`` is told to download."""
    if asset_name:
        for asset in assets:
            if str(asset.get("name", "")) == asset_name:
                return asset
        available = ", ".join(sorted(str(asset.get("name", "")) for asset in assets))
        raise E2EError(f"release has no asset named {asset_name!r}; published assets: {available}")
    by_name = {str(asset.get("name", "")).lower(): asset for asset in assets}
    for candidate in PUBLISHED_ASSET_NAMES.get(platform, ()):
        if candidate in by_name:
            return by_name[candidate]
    installers = installer_candidates(assets, platform)
    if installers:
        arch = host_arch()
        ranked = sorted(
            installers,
            key=lambda asset: (
                -architecture_match_score(str(asset.get("name", "")), arch),
                -asset_preference_score(str(asset.get("name", "")), platform),
                str(asset.get("name", "")).lower(),
            ),
        )
        return ranked[0]
    available = ", ".join(sorted(str(asset.get("name", "")) for asset in assets))
    raise E2EError(
        f"published release has no known {platform} installer "
        f"(looked for {', '.join(PUBLISHED_ASSET_NAMES.get(platform, ()))}); "
        f"published assets: {available}. Pass --asset-name for a renamed asset."
    )


def resolve_published_artifact(
    repo: str, tag: str, platform: str, asset_name: str | None = None
) -> dict[str, Any]:
    """Resolve the published installer URL and its release-reported metadata."""
    asset = select_release_asset(release_assets(repo, tag), platform, asset_name)
    url = asset.get("browser_download_url")
    name = str(asset.get("name", ""))
    if not isinstance(url, str) or not url:
        raise E2EError(f"published asset {name!r} has no browser_download_url")
    return {
        "repo": repo,
        "tag": tag,
        "platform": platform,
        "name": name,
        "id": asset.get("id"),
        "url": url,
        "size": asset.get("size"),
        "digest": asset.get("digest"),
        "created_at": asset.get("created_at"),
    }


def sniff_artifact_kind(path: Path) -> str | None:
    """Identify a file by magic bytes, so extensionless assets are supported."""
    try:
        with path.open("rb") as handle:
            head = handle.read(8)
    except OSError:
        return None
    if head[:4] == b"\x7fELF":
        return "elf"
    if head[:2] == b"MZ":
        return "pe"
    if head[:2] == b"PK":
        return "zip"
    if head[:4] in {
        b"\xcf\xfa\xed\xfe",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xce",
        b"\xfe\xed\xfa\xcf",
        b"\xca\xfe\xba\xbe",
    }:
        return "macho"
    return None


def process_command_line(pid: int) -> str | None:
    """Read back the real command line of a launched PID, or None if unreadable."""
    if os.name == "nt":
        # `tasklist /FO CSV` prints name,pid,session,sess#,memory — never a
        # command line, so the old implementation always "read back" a CSV row
        # (or, worse, the name of a process that was not the app at all).
        powershell = shutil.which("powershell") or "powershell"
        result = command_result(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter \"ProcessId = {pid}\").CommandLine",
            ],
            timeout=30,
        )
        if result["returncode"] != 0:
            return None
        return str(result["stdout"]).strip() or None
    result = command_result(["ps", "-o", "command=", "-p", str(pid)], timeout=10)
    if result["returncode"] != 0:
        return None
    return str(result["stdout"]).strip() or None


def remove_path(target: Path) -> bool:
    """Delete a file, symlink or directory tree if it exists."""
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        try:
            target.unlink()
        except OSError:
            pass
    elif target.exists():
        shutil.rmtree(target, ignore_errors=True)
    return not target.exists()


def uninstall_artifact(
    installed: Path,
    config_dir: Path,
    home_dir: Path,
    strategy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Uninstall the product and remove the isolated config dir and HOME.

    ``strategy`` carries how the artifact was installed, so the undo is the
    real one: ``apt-get remove`` for a .deb, the bundled uninstaller for a
    Windows installer, and a directory delete for a portable copy.
    """
    strategy = strategy or {}
    kind = str(strategy.get("kind") or "")
    steps: list[dict[str, Any]] = []
    ran_uninstaller = False
    if kind == "apt":
        command = [str(part) for part in strategy.get("command") or []]
        if command:
            result = command_result(command, timeout=900)
            steps.append({"command": command, "returncode": result["returncode"]})
    elif kind == "dir" and strategy.get("uninstaller") and os.name == "nt":
        command = [str(strategy["uninstaller"]), "/S"]
        result = command_result(command, timeout=900)
        steps.append({"command": command, "returncode": result["returncode"]})
        ran_uninstaller = True
    targets: list[tuple[str, Path]] = []
    if kind == "dir" and strategy.get("path"):
        targets.append(("install_root_removed", Path(str(strategy["path"]))))
    targets.extend(
        [
            ("installed_artifact_removed", installed),
            ("config_dir_removed", config_dir),
            ("home_profile_removed", home_dir),
        ]
    )
    removed = {label: remove_path(target) for label, target in targets}
    if kind == "apt":
        method = (
            "apt-get remove the installed package, delete the throwaway install root, "
            "isolated config dir and isolated HOME"
        )
    elif ran_uninstaller:
        method = (
            "run the bundled uninstaller, delete the throwaway install root, isolated "
            "config dir and isolated HOME"
        )
    else:
        method = "delete the throwaway install root, isolated config dir and isolated HOME"
    return {
        "method": method,
        **removed,
        "commands": steps,
    }


def gatekeeper_report(
    signature: dict[str, Any], quarantine: str | None, required: bool
) -> dict[str, Any]:
    """Describe the macOS trust boundary without weakening or faking it."""
    stderr = str(signature.get("codesign_stderr", ""))
    team_identifier = None
    for line in stderr.splitlines():
        if line.startswith("TeamIdentifier="):
            value = line.split("=", 1)[1].strip()
            team_identifier = None if value.lower() in {"not set", ""} else value
    if sys.platform != "darwin":
        return {
            "checked": False,
            "reason": "macOS-only trust assessment",
            "required_accepted": required,
        }
    return {
        "checked": True,
        "codesign_verified": signature.get("codesign_exit") == 0,
        "ad_hoc_signed": bool(signature.get("ad_hoc")),
        "team_identifier": team_identifier,
        "developer_id_signed": bool(team_identifier),
        "spctl_accepted": bool(signature.get("gatekeeper_accepted")),
        "required_accepted": required,
        "quarantine_attribute": quarantine,
        "launch_used_direct_exec": True,
        "launchservices_assessment_exercised": False,
        "remediation": (
            "Published macOS builds are unsigned/ad-hoc, so Gatekeeper refusing the app is "
            "expected. A stranger who downloads it in a browser must approve the first launch "
            "via Finder's contextual Open, or System Settings -> Privacy & Security -> Open "
            "Anyway, and relaunch. This harness never removes com.apple.quarantine and never "
            "disables Gatekeeper; a direct exec does not exercise the LaunchServices assessment, "
            "so this run does not prove Gatekeeper acceptance."
        ),
    }


def emit_summary(summary: dict[str, Any], evidence_out: str | None) -> None:
    """Print the JSON summary and, when asked, persist it as evidence."""
    if evidence_out:
        path = Path(evidence_out).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        summary["evidence_written_to"] = str(path)
        try:
            path.write_text(
                json.dumps(summary, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"[e2e] WARNING: cannot write evidence to {path}: {exc}", file=sys.stderr, flush=True)
            summary["evidence_written_to"] = None
    # ASCII-escaped on stdout only: the summary can carry a child process's own
    # output, and a Windows console is cp1252 — an unescaped U+FFFD from that
    # output killed the whole run *after* the app had booted healthy.
    print(json.dumps(summary, sort_keys=True, ensure_ascii=True))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install a local or published GitHub release xPST desktop artifact in a throwaway profile and smoke it."
    )
    parser.add_argument(
        "artifact",
        nargs="?",
        help="local .dmg/.zip/.exe/.AppImage/extensionless executable path, or GitHub release asset URL",
    )
    parser.add_argument(
        "--release",
        help="published release tag whose platform installer should be resolved and tested (for example v1.1.0)",
    )
    parser.add_argument(
        "--asset-name",
        help="exact published asset basename when --release cannot infer the installer name",
    )
    parser.add_argument(
        "--checksums",
        help="local or URL SHA256SUMS file; otherwise derive <platform>-SHA256SUMS from a GitHub release URL",
    )
    parser.add_argument(
        "--checksum-asset",
        help="release asset basename when a local file was renamed (for example xPST.dmg)",
    )
    parser.add_argument("--repo", default="TysAIs/xPST", help="GitHub owner/repository for a local artifact")
    parser.add_argument("--release-tag", help="release tag for a local artifact, for example v1.1.0")
    parser.add_argument("--platform", choices=["macos", "windows", "linux"], help="checksum family when it cannot be inferred")
    parser.add_argument(
        "--require-published",
        action="store_true",
        help=(
            "fail unless the artifact is a published GitHub release download; a local file is "
            "rejected so a smoke of an unbuilt working tree can never be reported as a "
            "stranger-install pass"
        ),
    )
    parser.add_argument(
        "--boot-budget-seconds",
        type=float,
        default=15.0,
        help="maximum process-spawn-to-on-screen-window time (default: 15)",
    )
    parser.add_argument(
        "--health-timeout-seconds",
        type=float,
        default=60.0,
        help="maximum wait for loopback /health HTTP 200 (default: 60)",
    )
    parser.add_argument(
        "--evidence-out",
        help="write the machine-readable evidence JSON to this path as well as stdout",
    )
    parser.add_argument(
        "--no-require-visible-window",
        action="store_true",
        help=(
            "skip the on-screen-window assertion (headless CI only). The run is recorded as "
            "non-release evidence and must not be reported as a clean-profile pass."
        ),
    )
    parser.add_argument(
        "--require-gatekeeper-accepted",
        action="store_true",
        help="fail when spctl rejects the app; only meaningful for a Developer ID signed release",
    )
    parser.add_argument("--work-dir", help="empty directory to use instead of an automatically deleted temp directory")
    parser.add_argument("--keep-work", action="store_true", help="keep the temp directory for debugging (cleanup is then not asserted)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    summary: dict[str, Any] = {
        "ok": False,
        "status": "failed",
        "artifact_source": None,
        "artifact": None,
        "platform": args.platform,
        "release": None,
        "checks": {},
        "findings": [],
        "failures": [],
    }
    work: WorkDir | None = None
    process: subprocess.Popen[Any] | None = None
    try:
        if args.release and args.artifact:
            raise E2EError("pass either an artifact path/URL or --release <tag>, not both")
        if not args.release and not args.artifact:
            raise E2EError("pass an artifact path/URL, or --release <tag>")
        work = WorkDir(args.work_dir)
        work_path = work.path
        log_path = work_path / "launch.log"
        download_dir = work_path / "download"
        download_dir.mkdir()
        early_failures: list[str] = []
        early_findings: list[str] = []
        release_info: dict[str, Any] | None = None
        if args.release:
            resolved_platform = args.platform or default_platform()
            release_info = resolve_published_artifact(
                args.repo, args.release, resolved_platform, args.asset_name
            )
            source = str(release_info["url"])
            source_name = str(release_info["name"])
            summary["release"] = release_info
            print(
                f"[e2e] published {args.release} {resolved_platform} asset: {source_name} "
                f"({release_info.get('size')} bytes, digest={release_info.get('digest')})",
                flush=True,
            )
        else:
            source = str(args.artifact)
            source_name = artifact_name(source)
        summary["artifact_source"] = source
        summary["artifact"] = source_name
        artifact = download_dir / source_name
        parsed = urllib.parse.urlparse(source)
        if parsed.scheme in {"http", "https"}:
            print(f"[e2e] downloading {source_name} with curl -L", flush=True)
            fetch_url(source, artifact)
            source_kind = "url"
        else:
            local = Path(source).expanduser().resolve()
            if not local.is_file():
                raise E2EError(f"artifact does not exist: {local}")
            shutil.copy2(local, artifact)
            source_kind = "local"
        if args.require_published and source_kind != "url":
            raise E2EError(
                "--require-published rejects a local artifact: pass a GitHub release asset URL or "
                "--release <tag> so the run can only ever test something a stranger can download"
            )
        if artifact.stat().st_size == 0:
            raise E2EError(f"artifact is empty: {artifact.name}")
        if release_info and release_info.get("size") is not None:
            published_bytes = int(release_info["size"])
            if artifact.stat().st_size != published_bytes:
                early_failures.append(
                    f"downloaded {artifact.stat().st_size} bytes but the published asset is "
                    f"{published_bytes} bytes"
                )
        content_kind = sniff_artifact_kind(artifact)
        kind = artifact_type(source_name, artifact)
        family = platform_key(source_name, args.platform, content_kind)
        summary["platform"] = family
        published_digest = (release_info or {}).get("digest")
        digest_hex = None
        if isinstance(published_digest, str) and published_digest.startswith("sha256:"):
            digest_hex = published_digest.split(":", 1)[1].lower()
        if args.checksums:
            checksum_text, checksum_provenance = load_checksum_source(
                args.checksums, download_dir / f"{family}-SHA256SUMS"
            )
        else:
            candidates = checksum_source_candidates(
                source, source_name, family, args.repo, args.release_tag
            )
            try:
                checksum_text, checksum_provenance = load_first_checksum_source(
                    candidates, download_dir / f"{family}-SHA256SUMS"
                )
            except E2EError:
                if not digest_hex:
                    raise
                checksum_text, checksum_provenance = "", "none (no checksum file in the release)"
                early_findings.append(
                    "No release checksum file could be downloaded; the sha256 verdict comes "
                    "only from the asset digest reported by the release API."
                )
        checksum = verify_checksum(
            artifact, checksum_text, checksum_provenance, args.checksum_asset, digest_hex
        )
        print(
            f"[e2e] checksum {checksum['actual_sha256']} == release {checksum['expected_sha256']} ({checksum['bytes']} bytes)",
            flush=True,
        )
        if checksum["verdict_source"] == "release-api-digest":
            early_findings.append(
                "The release's own <platform>-SHA256SUMS does not list this asset; the verdict "
                "comes from the asset digest reported by the release API instead."
            )
        if digest_hex and digest_hex != checksum["actual_sha256"]:
            early_failures.append(
                "the release asset digest does not match the downloaded sha256 "
                f"({published_digest} vs {checksum['actual_sha256']})"
            )
        installed, install = install_artifact(artifact, kind, work_path)
        early_findings.extend(str(item) for item in install.pop("findings", []))
        app = installed if installed.suffix == ".app" else find_app(installed.parent)
        if app and app != installed and installed.is_dir():
            app = installed
        stack = "executable"
        markers: dict[str, bool] = {}
        bundle = {}
        signature: dict[str, Any] = {"checked": False}
        quarantine = xattr_value(artifact, "com.apple.quarantine")
        installed_quarantine = xattr_value(installed, "com.apple.quarantine")
        if app and app.is_dir():
            bundle = bundle_info(app)
            stack, markers = classify_bundle(app)
            signature = signature_assessment(app)
            print(f"[e2e] installed {app.name}: stack={stack}", flush=True)
            print(
                f"[e2e] codesign={'ok' if signature.get('codesign_exit') == 0 else 'failed'} "
                f"spctl={'accepted' if signature.get('gatekeeper_accepted') else 'rejected'}",
                flush=True,
            )
            executable = bundle_executable(app)
        else:
            executable = installed
            print(f"[e2e] installed executable: {executable.name}", flush=True)
        config_dir = work_path / "config"
        home_dir = work_path / "home"
        config_dir.mkdir(parents=True)
        home_dir.mkdir(parents=True)
        env = launch_environment(work_path, config_dir, home_dir)
        probe = None if args.no_require_visible_window else prepare_window_probe(work_path)
        host_kind = host_binary_kind()
        cross_platform_artifact = bool(
            host_kind and content_kind in {"elf", "macho", "pe"} and content_kind != host_kind
        )
        # Any engine already running belongs to a foreign install (for example the
        # developer's own app). Record it now so the leftover assertion only
        # blames a sidecar this run actually started.
        preexisting_engines = engine_processes()
        preexisting_pids = {process["pid"] for process in preexisting_engines}
        if preexisting_engines:
            early_findings.append(
                f"{len(preexisting_engines)} xpst-engine process(es) were already running before "
                "this run (a foreign install); they are excluded from the leftover check."
            )
        with log_path.open("w", encoding="utf-8") as log_handle:
            start = time.monotonic()
            launch_prefix = headless_display_prefix()
            launch_command = [*launch_prefix, str(executable)]
            if launch_prefix:
                print(
                    f"[e2e] no DISPLAY on this Linux host: launching under "
                    f"{Path(launch_prefix[0]).name} so the GTK/Tauri shell can start",
                    flush=True,
                )
            try:
                process = subprocess.Popen(
                    launch_command,
                    cwd=str(work_path),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=(os.name != "nt"),
                )
            except OSError as exc:
                hint = cross_platform_hint(content_kind, host_kind) if cross_platform_artifact else ""
                raise E2EError(f"launch failed for {executable.name}: {exc}{hint}") from exc
            boot, health, ui, poll_failures = health_and_ui(
                log_path,
                process,
                start,
                args.health_timeout_seconds,
                args.boot_budget_seconds,
                probe,
                require_visible=not args.no_require_visible_window,
            )
            # A single ps sample races the macOS shell's re-exec: the sidecar
            # can be missing from the process table in the instant right after
            # the first /health 200 (run 42508682981).  Only the tauri stack
            # requires a sidecar, so only it pays for the bounded re-check.
            if stack == "tauri":
                sidecar = observe_engine_sidecar(exclude_pids=preexisting_pids)
            else:
                observed_processes = engine_processes()
                sidecar = {
                    "processes": observed_processes,
                    "attempts": 1,
                    "retry_window_seconds": 0.0,
                    "attempts_exhausted": False,
                }
            running = {
                "app_process_alive_after_boot": process.poll() is None,
                "app_pid": process.pid,
                "app_command_line": process_command_line(process.pid),
                "engine_sidecar_processes": sidecar["processes"],
                "engine_sidecar_attempts": sidecar["attempts"],
                "engine_sidecar_retry_window_seconds": sidecar["retry_window_seconds"],
                "engine_sidecar_attempts_exhausted": sidecar["attempts_exhausted"],
            }
            launch_record = {
                "command": launch_command,
                "headless_display_prefix": launch_prefix,
                "log_path": str(log_path),
                "log_tail": read_log(log_path)[-4000:],
                "exit_code_before_boot": process.poll(),
            }
        stop = terminate_process(process)
        print(
            f"[e2e] boot_to_visible={boot['boot_to_visible_seconds']!r}s "
            f"health={health['status']!r} ui={ui['status']!r} "
            f"ui_attempts={ui['attempts']!r} "
            f"sidecar_attempts={running['engine_sidecar_attempts']!r}",
            flush=True,
        )
        time.sleep(1)
        still_running = engine_processes()
        leftovers = [process for process in still_running if process["pid"] not in preexisting_pids]
        # Whatever the process-group kill missed (an AppImage's self-extracted
        # shell, WebKit helpers) still holds this run's work dir. Stop it before
        # the teardown walks the tree, and say so in the evidence.
        strays = stop_work_processes(work_path)
        if strays:
            print(
                f"[e2e] stopped {len(strays)} process(es) still using the throwaway directory "
                f"after the process-group kill: "
                f"{', '.join(entry['pid'] for entry in strays)}",
                flush=True,
            )
        launch_record["stray_processes_stopped"] = strays
        home_state = home_dir / ".xpst"
        config_files = sorted(str(path.relative_to(config_dir)) for path in config_dir.rglob("*") if path.is_file())
        home_files = sorted(str(path.relative_to(home_dir)) for path in home_dir.rglob("*") if path.is_file())
        cleanup_before = {
            "config_override_honored": bool(config_files),
            "config_files": config_files[:50],
            "home_files": home_files[:50],
            "home_xpst_files": sorted(str(path.relative_to(home_state)) for path in home_state.rglob("*") if path.is_file())[:50]
            if home_state.exists()
            else [],
        }
        gatekeeper = gatekeeper_report(
            signature, quarantine or installed_quarantine, args.require_gatekeeper_accepted
        )
        findings = early_findings + first_run_findings(
            stack, config_dir, home_dir, signature, health, ui, read_log(log_path)
        )
        if args.no_require_visible_window:
            findings.append(
                "The on-screen-window assertion was skipped (--no-require-visible-window); "
                "this run is not a clean-profile release pass."
            )
        if gatekeeper.get("checked") and not gatekeeper.get("spctl_accepted"):
            findings.append(
                "Gatekeeper (spctl) rejected the installed app: the published macOS asset is "
                "ad-hoc signed with no Team ID. A browser download of this artifact needs the "
                "user's Finder Open / Open Anyway approval; this run does not prove Gatekeeper "
                "acceptance."
            )
        real_process_ok = bool(running["app_process_alive_after_boot"]) and bool(
            running["app_command_line"]
        )
        if stack == "tauri" and not running["engine_sidecar_processes"]:
            real_process_ok = False
        failures = list(early_failures) + list(poll_failures)
        if launch_record["exit_code_before_boot"] is not None:
            # The app died before any /health answer: say *that*, with the code
            # and its own output, instead of "no HTTP 200 in 90s" (which reads
            # like a timeout and hid the v1.2.1 linux/windows cause entirely).
            detail = " | ".join(
                line.strip() for line in launch_record["log_tail"].splitlines() if line.strip()
            )[-1200:]
            diagnosis = (
                f"the launched app exited with code {launch_record['exit_code_before_boot']} "
                f"before serving /health (launch command: {' '.join(launch_record['command'])}); "
                f"last launch output: {detail or '<the app wrote nothing to its log>'}"
            )
            failures = [
                diagnosis if failure.startswith("engine /health did not return HTTP 200") else failure
                for failure in failures
            ]
        if not running["app_process_alive_after_boot"]:
            failures.append(
                "the launched application process was not running at the end of the boot poll "
                f"(exit code {health['process_exit_code_during_poll']})"
            )
        if not running["app_command_line"]:
            failures.append(
                f"could not read back a real command line for the launched PID {running['app_pid']}"
            )
        if stack == "tauri" and not running["engine_sidecar_processes"]:
            failures.append(
                "no xpst-engine sidecar process was observed while the packaged app ran "
                f"({running['engine_sidecar_attempts']} ps sample(s) over "
                f"{running['engine_sidecar_retry_window_seconds']:.1f}s)"
            )
        if leftovers:
            failures.append(
                f"leftover xpst-engine process(es) started by this run: {leftovers}"
            )
        if stop["exit_code"] is None:
            failures.append("launched process did not provide an exit code after shutdown")
        if kind == "dmg" and not app:
            failures.append("mounted DMG did not yield an installed .app")
        if args.require_gatekeeper_accepted and gatekeeper.get("checked") and not gatekeeper.get(
            "spctl_accepted"
        ):
            failures.append("--require-gatekeeper-accepted was set but spctl rejected the app")

        uninstall = uninstall_artifact(
            installed, config_dir, home_dir, install.get("uninstall")
        )
        if not args.keep_work:
            cleanup_ok = work.cleanup(False)
        else:
            cleanup_ok = False
            findings.append("work directory retained by --keep-work; uninstall cleanup was not asserted")
        uninstall["work_directory_removed"] = not work_path.exists()
        uninstall["ok"] = bool(
            uninstall["installed_artifact_removed"]
            and uninstall["config_dir_removed"]
            and uninstall["home_profile_removed"]
            and uninstall["work_directory_removed"]
        )
        if not args.keep_work:
            if not cleanup_ok:
                failures.append("throwaway install/config cleanup did not remove the work directory")
            if not uninstall["ok"]:
                failures.append(
                    "uninstall assertions failed: "
                    f"{ {key: value for key, value in uninstall.items() if key.endswith('_removed')} }"
                )
        checks = {
            "checksum": checksum["ok"],
            "install": True,
            "boot_to_visible": boot["ok"],
            "engine_health_200": health["ok"],
            "packaged_ui_served": ui["ok"],
            "real_running_process": real_process_ok,
            "zero_xpst_engine_processes": not leftovers,
            "cleanup": cleanup_ok,
            "uninstall": uninstall["ok"],
        }
        summary.update(
            {
                "ok": not failures,
                "status": "passed" if not failures else "failed",
                "source_kind": source_kind,
                "artifact_type": kind,
                "artifact_content_kind": content_kind,
                "checks": checks,
                "checksum": checksum,
                "install": {"ok": True, **install},
                "bundle": bundle,
                "stack": {"name": stack, "markers": markers},
                "packaged_ui": {
                    "present": markers.get("ui_index", False) or markers.get("pyside6", False),
                    "http_served": ui["ok"],
                    "markers": markers,
                },
                "signature": signature,
                "gatekeeper": gatekeeper,
                "artifact_quarantine": quarantine,
                "installed_quarantine": installed_quarantine,
                "boot": boot,
                "health": health,
                "ui": ui,
                "running_process": running,
                "launch": launch_record,
                "shutdown": stop,
                "engine_processes_after_shutdown": leftovers,
                "preexisting_engine_processes": preexisting_engines,
                "cleanup": {
                    "ok": cleanup_ok,
                    "work_directory_removed": cleanup_ok,
                    "config_override_honored": cleanup_before["config_override_honored"],
                    "config_files_before_cleanup": cleanup_before["config_files"],
                    "home_files_before_cleanup": cleanup_before["home_files"],
                },
                "uninstall": uninstall,
                "findings": findings,
                "failures": failures,
            }
        )
        summary["evidence"] = {
            "artifact_name": artifact.name,
            "artifact_source": source,
            "release": release_info,
            "artifact_bytes": checksum["bytes"],
            "artifact_sha256": checksum["actual_sha256"],
            "release_expected_sha256": checksum["expected_sha256"],
            "checksum_source": checksum["checksum_source"],
            "checksum_verdict_source": checksum["verdict_source"],
            "checksum_ok": checksum["ok"],
            "artifact_type": kind,
            "platform": family,
            "stack": stack,
            "http_status": health["status"],
            "health_ok": health["ok"],
            "health_url": health["url"],
            "packaged_ui_ok": ui["ok"],
            "ui": ui,
            "boot_ok": boot["ok"],
            "boot_to_visible_seconds": boot["boot_to_visible_seconds"],
            "window_assertion_required": boot["required"],
            "running_process": running,
            "app_exit_code_during_poll": health["process_exit_code_during_poll"],
            "shutdown_exit_code": stop["exit_code"],
            "stray_processes_stopped": strays,
            "engine_processes_after_shutdown": leftovers,
            "preexisting_engine_processes": preexisting_engines,
            "cleanup_ok": cleanup_ok,
            "uninstall_ok": uninstall["ok"],
            "gatekeeper": gatekeeper,
            "source_kind": source_kind,
            "published_required": bool(args.require_published),
            "checks": checks,
            "findings": findings,
            "failures": failures,
        }
    except (E2EError, OSError, ValueError, subprocess.SubprocessError) as exc:
        summary["failures"] = [str(exc)]
        summary["findings"] = [str(exc)]
        if process is not None:
            terminate_process(process)
        if work is not None:
            stop_work_processes(work.path)
            summary["cleanup"] = {"ok": work.cleanup(args.keep_work), "work_directory_removed": not args.keep_work}
    except Exception as exc:  # pragma: no cover - defensive boundary for a release gate
        summary["failures"] = [f"unexpected harness error: {type(exc).__name__}: {exc}"]
        summary["findings"] = summary["failures"]
        if process is not None:
            terminate_process(process)
        if work is not None:
            stop_work_processes(work.path)
            summary["cleanup"] = {"ok": work.cleanup(args.keep_work), "work_directory_removed": not args.keep_work}
    if "evidence" not in summary:
        # A run that failed before it could measure anything still emits the same
        # machine-readable shape, so evidence consumers never guess from prose.
        summary["evidence"] = {
            "artifact_name": summary.get("artifact"),
            "artifact_source": summary.get("artifact_source"),
            "release": summary.get("release"),
            "checks": {},
            "findings": summary.get("findings", []),
            "failures": summary.get("failures", []),
        }
    emit_summary(summary, args.evidence_out)
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
