#!/usr/bin/env python3
"""Install and smoke-test a published xPST desktop artifact.

This helper is intentionally dependency-free.  The shell entry point owns the
public interface; this module owns the platform-specific install and launch
logic so failures can be reported in one machine-readable summary.

Two inputs are supported:

* an explicit artifact path or URL (``positional``), or
* ``--release <tag>``, which resolves the artifact a stranger would actually
  download from a published GitHub release for the requested platform.

Published macOS assets are ad-hoc signed, so Gatekeeper refusing the app is
expected behaviour.  The harness only *reads* signing and quarantine state; it
never removes quarantine, disables Gatekeeper, or claims an approval it did not
observe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
from typing import Any

GITHUB_API = "https://api.github.com"


class E2EError(RuntimeError):
    """A user-facing harness failure with a concise message."""


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
            self.path = Path(tempfile.mkdtemp(prefix="xpst-stranger-install-"))
            self.owned = True

    def cleanup(self, keep: bool) -> bool:
        if keep:
            return False
        shutil.rmtree(self.path, ignore_errors=True)
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


# ---------------------------------------------------------------------------
# Published-release resolution
#
# A stranger does not know which asset name the release picked; they open the
# releases page and download the one for their platform.  These helpers do the
# same thing from the GitHub release API so the harness always tests something
# that is actually published (asset id/size/created_at come from the API, not
# from the caller's string).
# ---------------------------------------------------------------------------

def github_api_json(url: str) -> Any:
    """GET a GitHub API URL and decode JSON, reporting the real HTTP failure."""
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "xPST-stranger-install-e2e",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise E2EError(f"GitHub API {url} returned HTTP {exc.code}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise E2EError(f"GitHub API {url} is unreachable: {exc}") from exc
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E2EError(f"GitHub API {url} did not return JSON: {exc}") from exc


def release_asset_candidates(repo: str, tag: str) -> list[dict[str, Any]]:
    """Return the published release assets with their API metadata."""
    url = f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}"
    release = github_api_json(url)
    assets = release.get("assets") if isinstance(release, dict) else None
    if not isinstance(assets, list) or not assets:
        raise E2EError(f"release {tag} in {repo} publishes no assets")
    return [
        {
            "name": str(asset.get("name", "")),
            "size": int(asset.get("size", 0) or 0),
            "id": asset.get("id"),
            "created_at": asset.get("created_at"),
            "url": str(asset.get("browser_download_url", "")),
            "content_type": asset.get("content_type"),
        }
        for asset in assets
        if isinstance(asset, dict) and asset.get("name")
    ]


def platform_asset_score(asset_name: str, platform: str) -> int:
    """Rank a published asset for one platform; 0 means 'not a candidate'.

    macOS publishes the legacy `.dmg` and the Tauri updater `.app.tar.gz`;
    Linux publishes an extensionless PyInstaller binary; Windows publishes
    `.exe`/`.msi`.  Keyword and extension rules are ranked so the most
    installable artifact wins rather than the alphabetically first one.
    """
    lower = asset_name.lower()
    if lower.endswith(("sha256sums", "sha512sums", ".json", ".md", ".cdx.json", ".sig")):
        return 0
    if platform == "macos":
        if lower.endswith(".dmg") and "macos" not in lower:
            return 100
        if lower.endswith(".zip") and "macos" not in lower:
            return 60
        if lower.endswith((".pkg",)):
            return 55
        return 0
    if platform == "windows":
        if lower.endswith(".exe"):
            return 100
        if lower.endswith(".msi"):
            return 90
        return 0
    if platform == "linux":
        if lower.endswith((".appimage", ".deb", ".rpm")):
            return 100
        if lower.endswith(".tar.gz") and "linux" in lower:
            return 70
        # The published linux lane uploads an extensionless ELF named `xPST`.
        if "." not in lower and "xpst" in lower:
            return 80
        return 0
    raise E2EError(f"unknown platform for asset ranking: {platform}")


def choose_release_asset(assets: list[dict[str, Any]], platform: str) -> dict[str, Any]:
    """Pick the best published asset for a platform, failing closed when empty."""
    ranked = sorted(
        ((platform_asset_score(asset["name"], platform), asset) for asset in assets),
        key=lambda pair: pair[0],
        reverse=True,
    )
    for score, asset in ranked:
        if score > 0:
            return asset
    names = ", ".join(asset["name"] for asset in assets)
    raise E2EError(f"no published {platform} artifact among release assets: {names}")


def default_platform() -> str:
    """Map the running host to a release asset family."""
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt":
        return "windows"
    return "linux"


def resolve_release_artifact(
    repo: str,
    tag: str,
    platform: str,
    requested_asset: str | None,
) -> dict[str, Any]:
    """Resolve a published release asset to a download URL plus provenance."""
    if not tag:
        raise E2EError("--release requires a tag such as v1.1.0")
    assets = release_asset_candidates(repo, tag)
    if requested_asset:
        chosen = next((asset for asset in assets if asset["name"] == requested_asset), None)
        if chosen is None:
            names = ", ".join(asset["name"] for asset in assets)
            raise E2EError(f"{requested_asset} is not a published asset of {tag}; published: {names}")
    else:
        chosen = choose_release_asset(assets, platform)
    if not chosen["url"]:
        raise E2EError(f"published asset {chosen['name']} has no download URL")
    return {**chosen, "tag": tag, "repo": repo, "asset_count": len(assets)}


def sniff_artifact_kind(path: Path) -> str | None:
    """Identify a downloaded artifact by magic bytes, not only by filename.

    The published Linux lane uploads an extensionless PyInstaller binary, so a
    stranger cannot tell from the name alone; a name-only classifier rejects it.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(4)
            handle.seek(0)
            tail = b""
            size = path.stat().st_size
            if size >= 512:
                handle.seek(-512, os.SEEK_END)
                tail = handle.read(512)
    except OSError:
        return None
    if head.startswith(b"\x7fELF"):
        return "binary"
    if head.startswith(b"MZ"):
        return "exe"
    if head[:4] in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe"):
        return "binary"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if tail[:4] in (b"koly",):
        return "dmg"
    return None


def platform_key(asset_name: str, explicit: str | None) -> str:
    """Map an artifact name to the release checksum asset family."""
    if explicit:
        return explicit
    lower = asset_name.lower()
    if lower.endswith((".dmg", ".pkg")) or "macos" in lower or "darwin" in lower or "arm64" in lower:
        return "macos"
    if lower.endswith((".exe", ".msi")) or "windows" in lower or "win32" in lower:
        return "windows"
    if lower.endswith((".appimage", ".deb", ".rpm")) or "linux" in lower:
        return "linux"
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
) -> dict[str, Any]:
    """Verify the bytes against the named release checksum.

    A local file may have been renamed after download.  In that case an
    explicit --checksum-asset is preferred; otherwise a single same-suffix
    entry is accepted, while ambiguous files fail closed.
    """
    entries = parse_checksums(checksum_text)
    release_asset = checksum_asset or asset.name
    expected = entries.get(release_asset)
    if expected is None and checksum_asset is None:
        candidates = [
            (name, digest)
            for name, digest in entries.items()
            if Path(name).suffix.lower() == asset.suffix.lower()
        ]
        if len(candidates) == 1:
            release_asset, expected = candidates[0]
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
    }
    if actual != expected:
        raise E2EError(
            f"checksum mismatch for {asset.name}: expected {expected}, got {actual}"
        )
    return result


def safe_extract_zip(archive: Path, destination: Path) -> None:
    """Extract a zip without allowing entries to escape the throwaway root."""
    destination = destination.resolve()
    try:
        with zipfile.ZipFile(archive) as handle:
            for member in handle.infolist():
                target = (destination / member.filename).resolve()
                if target != destination and destination not in target.parents:
                    raise E2EError(f"zip entry escapes install root: {member.filename}")
                handle.extract(member, destination)
    except zipfile.BadZipFile as exc:
        raise E2EError(f"invalid zip artifact {archive.name}: {exc}") from exc


def find_app(root: Path) -> Path | None:
    """Find an application bundle, preferring xPST.app."""
    direct = sorted(root.glob("*.app"))
    recursive = sorted(path for path in root.rglob("*.app") if path.is_dir())
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
    """Identify Tauri versus the legacy PySide/QML desktop stack."""
    resources = app / "Contents" / "Resources"
    tauri_markers = {
        "ui_index": (resources / "ui" / "index.html").is_file(),
        "engine": (resources / "binaries" / "engine" / "xpst-engine").is_file(),
    }
    legacy_markers = {
        "qml_main": (resources / "xpst" / "desktop_app" / "qml" / "main.qml").is_file(),
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
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(2 * 1024 * 1024), None
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(2 * 1024 * 1024)
        except OSError:
            body = b""
        return exc.code, body, str(exc)
    except (OSError, urllib.error.URLError) as exc:
        return None, b"", str(exc)


def gui_session_available() -> bool:
    """Return True when a window server session can show a real window.

    A CI/ssh runner has no Aqua session, so asserting an on-screen window there
    would fail a healthy bundle.  This is a measured pre-condition, not an
    assumption: the launchd management domain is reported by the OS itself.
    """
    if sys.platform != "darwin":
        return os.environ.get("DISPLAY") is not None or os.environ.get("WAYLAND_DISPLAY") is not None
    if shutil.which("launchctl") is None:
        return False
    result = command_result(["launchctl", "managername"], timeout=5)
    return str(result.get("stdout", "")).strip() == "Aqua"


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
        if health_url and visible:
            break
        if process.poll() is not None:
            break
        time.sleep(0.2)

    if not visible:
        visible_seconds = None
    ui_status: int | None = None
    ui_title = ""
    ui_ok = False
    ui_reason = "health endpoint did not return HTTP 200"
    if health_url:
        ui_status, body, error = http_get(health_url)
        decoded = body.decode("utf-8", errors="replace")
        title_match = re.search(r"<title[^>]*>(.*?)</title>", decoded, re.IGNORECASE | re.DOTALL)
        ui_title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
        ui_ok = (
            ui_status == 200
            and "<html" in decoded.lower()
            and ("xPST" in decoded or "xpst" in decoded.lower())
        )
        ui_reason = (
            "packaged HTML served at the healthy loopback root"
            if ui_ok
            else f"root response was not the packaged HTML UI (status={ui_status}, error={error})"
        )

    visible_ok = visible and visible_seconds is not None and visible_seconds <= visible_budget
    if not require_visible and not visible_ok:
        # No window server session: record the measured result, do not fail on it.
        visible_reason = f"not required in this session ({visible_reason})"
        visible_ok = True
    elif visible and visible_seconds is not None and not visible_ok:
        visible_reason = f"visible after {visible_seconds:.3f}s, over budget {visible_budget:.3f}s"
    boot = {
        "ok": visible_ok,
        "visible_required": require_visible,
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
        "process_alive_at_probe": process.poll() is None,
    }
    ui = {
        "ok": ui_ok,
        "status": ui_status,
        "title": ui_title,
        "url": health_url,
        "reason": ui_reason,
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
    result = command_result(["ps", "-axo", "pid=,command="])
    if result["returncode"] != 0:
        return []
    processes = []
    pattern = re.compile(r"(?:^|/)xpst-engine(?:\.exe)?(?:\s|$)", re.IGNORECASE)
    for line in str(result["stdout"]).splitlines():
        match = re.match(r"\s*(\d+)\s+(.*)$", line)
        if not match:
            continue
        command = match.group(2)
        if pattern.search(command):
            processes.append({"pid": match.group(1), "command": command})
    return processes


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


def install_artifact(artifact: Path, artifact_type: str, work: Path) -> tuple[Path, dict[str, Any]]:
    """Mount/extract/copy an artifact into the throwaway install root."""
    install_root = work / "install"
    install_root.mkdir(parents=True, exist_ok=True)
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
    """Return the supported installer kind, sniffing extensionless assets."""
    lower = name.lower()
    if lower.endswith(".dmg"):
        return "dmg"
    if lower.endswith(".zip"):
        return "zip"
    if lower.endswith(".exe"):
        return "exe"
    if lower.endswith(".appimage"):
        return "appimage"
    if artifact is not None:
        sniffed = sniff_artifact_kind(artifact)
        if sniffed:
            return sniffed
    raise E2EError("artifact must end in .dmg, .zip, .exe, or .AppImage (or be a recognized binary)")


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
    error_markers = [
        line.strip()
        for line in log_text.splitlines()
        if re.search(r"Traceback|FATAL|Failed to load QML|cannot start|ImportError|ModuleNotFoundError", line, re.IGNORECASE)
    ]
    if error_markers:
        findings.append(f"Launch log contained {len(error_markers)} fatal/import/UI error line(s).")
    return findings


def write_evidence(path: Path, summary: dict[str, Any]) -> None:
    """Persist the machine-readable evidence JSON, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def gatekeeper_report(
    signature: dict[str, Any],
    quarantine: str | None,
    launch_alive: bool | None,
) -> dict[str, Any]:
    """Describe the macOS Gatekeeper situation without changing it.

    macOS desktop artifacts are unsigned (ad-hoc only), so a browser-downloaded
    copy carries com.apple.quarantine and shows the standard "unidentified
    developer" prompt.  The harness never strips quarantine or disables
    Gatekeeper; it records the measured attribute and the exact user-facing
    remedy instead of faking an acceptance.
    """
    if not signature.get("checked"):
        return {"applicable": False, "reason": "macOS-only assessment"}
    accepted = bool(signature.get("gatekeeper_accepted"))
    blocked = (not accepted) and bool(quarantine) and launch_alive is False
    return {
        "applicable": True,
        "download_transport": "curl (does not apply the LaunchServices quarantine attribute)",
        "quarantine_attribute": quarantine,
        "spctl_accepted": accepted,
        "ad_hoc_signature_only": bool(signature.get("ad_hoc")),
        "developer_id_signed": not bool(signature.get("ad_hoc")) and accepted,
        "notarization_proven": False,
        "blocked_launch_detected": blocked,
        "remediation": (
            "Unsigned/not-notarized build: approve the app once via Finder's contextual "
            "Open, or System Settings > Privacy & Security > Open Anyway. This is a "
            "user security decision; the harness does not remove quarantine or weaken Gatekeeper."
        ),
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install a local or GitHub-published xPST desktop artifact in a throwaway profile and smoke it."
    )
    parser.add_argument(
        "artifact",
        nargs="?",
        help="local .dmg/.zip/.exe/.AppImage path or GitHub release asset URL (omit when using --release)",
    )
    parser.add_argument(
        "--release",
        help="published release tag (for example v1.1.0); resolves the platform asset from the release API",
    )
    parser.add_argument(
        "--asset",
        help="exact published asset name to test with --release (default: best asset for --platform)",
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
        help="fail unless the artifact came from a published GitHub release (never a local file)",
    )
    parser.add_argument(
        "--evidence-out",
        help="path to write the machine-readable evidence JSON (a copy of the final summary line)",
    )
    parser.add_argument(
        "--require-visible",
        choices=["auto", "yes", "no"],
        default="auto",
        help="assert an on-screen window; auto asserts it only when a GUI session exists (default: auto)",
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
    parser.add_argument("--work-dir", help="empty directory to use instead of an automatically deleted temp directory")
    parser.add_argument("--keep-work", action="store_true", help="keep the temp directory for debugging (cleanup is then not asserted)")
    args = parser.parse_args(argv)
    if not args.artifact and not args.release:
        parser.error("provide an artifact path/URL or --release <tag>")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    summary: dict[str, Any] = {
        "ok": False,
        "status": "failed",
        "artifact_source": args.artifact,
        "artifact": None,
        "checks": {},
        "findings": [],
        "failures": [],
    }
    work: WorkDir | None = None
    process: subprocess.Popen[Any] | None = None
    try:
        # Resolve a published release asset first, so a stranger-scale run tests
        # whatever the release actually published rather than a hand-copied URL.
        release: dict[str, Any] | None = None
        artifact_source = args.artifact
        if args.release:
            release = resolve_release_artifact(
                args.repo, args.release, args.platform or default_platform(), args.asset
            )
            artifact_source = release["url"]
            print(
                f"[e2e] published {release['tag']} asset '{release['name']}' "
                f"({release['size']} bytes, id={release['id']}, created={release['created_at']}) "
                f"of {release['asset_count']} release assets",
                flush=True,
            )
        summary["artifact_source"] = artifact_source
        summary["artifact"] = artifact_name(artifact_source)
        summary["published"] = {
            "is_published": release is not None,
            "tag": release["tag"] if release else None,
            "asset_id": release["id"] if release else None,
            "asset_created_at": release["created_at"] if release else None,
            "release_asset_count": release["asset_count"] if release else None,
        }
        work = WorkDir(args.work_dir)
        work_path = work.path
        log_path = work_path / "launch.log"
        download_dir = work_path / "download"
        download_dir.mkdir()
        source_name = artifact_name(artifact_source)
        artifact = download_dir / source_name
        parsed = urllib.parse.urlparse(artifact_source)
        if parsed.scheme in {"http", "https"}:
            print(f"[e2e] downloading {source_name} with curl -L", flush=True)
            fetch_url(artifact_source, artifact)
            source_kind = "url"
        else:
            if args.require_published:
                raise E2EError("--require-published rejects a local artifact; use URL or --release")
            local = Path(artifact_source).expanduser().resolve()
            if not local.is_file():
                raise E2EError(f"artifact does not exist: {local}")
            shutil.copy2(local, artifact)
            source_kind = "local"
        if artifact.stat().st_size == 0:
            raise E2EError(f"artifact is empty: {artifact.name}")
        kind = artifact_type(source_name, artifact)
        if release and kind == "binary" and os.name == "nt":
            raise E2EError("a published ELF/Mach-O binary cannot be installed on this host")
        family = args.platform or (default_platform() if release else platform_key(source_name, args.platform))
        checksum_source = args.checksums or derive_checksum_source(
            artifact_source, source_name, family, args.repo, args.release_tag or (release["tag"] if release else None)
        )
        checksum_text, checksum_provenance = load_checksum_source(
            checksum_source, download_dir / f"{family}-SHA256SUMS"
        )
        checksum = verify_checksum(artifact, checksum_text, checksum_provenance, args.checksum_asset)
        print(
            f"[e2e] checksum {checksum['actual_sha256']} == release {checksum['expected_sha256']} ({checksum['bytes']} bytes)",
            flush=True,
        )
        installed, install = install_artifact(artifact, kind, work_path)
        app = installed if installed.suffix == ".app" else find_app(installed.parent)
        if app and app != installed and installed.is_dir():
            app = installed
        stack = "executable"
        markers: dict[str, bool] = {}
        bundle = {}
        signature: dict[str, Any] = {"checked": False}
        quarantine = xattr_value(artifact, "com.apple.quarantine")
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
        probe = prepare_window_probe(work_path)
        gui_available = gui_session_available()
        require_visible = args.require_visible == "yes" or (args.require_visible == "auto" and gui_available)
        print(
            f"[e2e] gui_session={gui_available} require_visible={require_visible}",
            flush=True,
        )
        with log_path.open("w", encoding="utf-8") as log_handle:
            start = time.monotonic()
            try:
                process = subprocess.Popen(
                    [str(executable)],
                    cwd=str(work_path),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=(os.name != "nt"),
                )
            except OSError as exc:
                raise E2EError(f"launch failed for {executable.name}: {exc}") from exc
            launch_pid = process.pid
            boot, health, ui, poll_failures = health_and_ui(
                log_path,
                process,
                start,
                args.health_timeout_seconds,
                args.boot_budget_seconds,
                probe,
                require_visible,
            )
        stop = terminate_process(process)
        print(
            f"[e2e] boot_to_visible={boot['boot_to_visible_seconds']!r}s "
            f"health={health['status']!r} ui={ui['status']!r}",
            flush=True,
        )
        time.sleep(1)
        leftovers = engine_processes()
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
        findings = first_run_findings(stack, config_dir, home_dir, signature, health, ui, read_log(log_path))
        failures = list(poll_failures)
        if leftovers:
            failures.append(f"leftover xpst-engine process(es): {leftovers}")
        if stop["exit_code"] is None:
            failures.append("launched process did not provide an exit code after shutdown")
        if kind == "dmg" and not app:
            failures.append("mounted DMG did not yield an installed .app")
        if args.require_published and source_kind != "url":
            failures.append("--require-published: the artifact was not a published download")
        if args.require_visible == "yes" and not gui_available:
            findings.append("--require-visible=yes forced a window assertion with no GUI session present")
        if not args.keep_work:
            cleanup_ok = work.cleanup(False)
        else:
            cleanup_ok = False
            findings.append("work directory retained by --keep-work; uninstall cleanup was not asserted")
        if not cleanup_ok:
            failures.append("throwaway install/config cleanup did not remove the work directory")
        gatekeeper = gatekeeper_report(signature, quarantine, health.get("process_alive_at_probe"))
        summary.update(
            {
                "ok": not failures,
                "status": "passed" if not failures else "failed",
                "source_kind": source_kind,
                "artifact_type": kind,
                "http_status": health["status"],
                "checks": {
                    "downloaded_published_artifact": source_kind == "url",
                    "checksum": checksum["ok"],
                    "install": True,
                    "boot_to_visible": boot["ok"],
                    "engine_health_200": health["ok"],
                    "packaged_ui_served": ui["ok"],
                    "zero_xpst_engine_processes": not leftovers,
                    "cleanup": cleanup_ok,
                },
                "artifact": {
                    "name": artifact.name,
                    "bytes": checksum["bytes"],
                    "sha256": checksum["actual_sha256"],
                    "type": kind,
                    "source": artifact_source,
                },
                "checksum": checksum,
                "install": {"ok": True, **install},
                "bundle": bundle,
                "stack": {"name": stack, "markers": markers},
                "packaged_ui": {
                    "present": markers.get("ui_index", False) or markers.get("qml_main", False),
                    "http_served": ui["ok"],
                    "markers": markers,
                },
                "signature": signature,
                "gatekeeper": gatekeeper,
                "artifact_quarantine": quarantine,
                "process": {
                    "pid": launch_pid,
                    "alive_at_health_probe": health.get("process_alive_at_probe"),
                    "shutdown_exit_code": stop["exit_code"],
                },
                "boot": boot,
                "health": health,
                "ui": ui,
                "shutdown": stop,
                "engine_processes_after_shutdown": leftovers,
                "cleanup": {
                    "ok": cleanup_ok,
                    "uninstalled": cleanup_ok,
                    "work_directory_removed": cleanup_ok,
                    "config_override_honored": cleanup_before["config_override_honored"],
                    "config_files_before_cleanup": cleanup_before["config_files"],
                    "home_files_before_cleanup": cleanup_before["home_files"],
                },
                "findings": findings,
                "failures": failures,
            }
        )
    except (E2EError, OSError, ValueError, subprocess.SubprocessError) as exc:
        summary["failures"] = [str(exc)]
        summary["findings"] = [str(exc)]
        summary["status"] = "failed"
        summary["ok"] = False
        if process is not None:
            terminate_process(process)
        if work is not None:
            summary["cleanup"] = {"ok": work.cleanup(args.keep_work), "uninstalled": not args.keep_work, "work_directory_removed": not args.keep_work}
    except Exception as exc:  # pragma: no cover - defensive boundary for a release gate
        summary["failures"] = [f"unexpected harness error: {type(exc).__name__}: {exc}"]
        summary["findings"] = summary["failures"]
        summary["status"] = "error"
        summary["ok"] = False
        if process is not None:
            terminate_process(process)
        if work is not None:
            summary["cleanup"] = {"ok": work.cleanup(args.keep_work), "uninstalled": not args.keep_work, "work_directory_removed": not args.keep_work}
    if args.evidence_out:
        write_evidence(Path(args.evidence_out).expanduser(), summary)
    print(json.dumps(summary, sort_keys=True, ensure_ascii=False))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
