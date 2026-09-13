"""Verify xPST desktop packaging inputs and built release artifacts.

The repository checks remain available through :func:`verify_desktop_package`.
For a built ``.app``, ``.dmg``, ``.zip`` or installer, use
:func:`verify_artifact`; it materializes the bundle read-only, verifies the
runtime resources and release version, records SHA-256/SHA-512 hashes, checks
permissions, scans content for personal data/secrets, and writes evidence.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

try:
    from scripts.scan_public_safety import SECRET_PATTERNS
    from scripts.verify_release_version import release_versions, verify_release_version
except ModuleNotFoundError:  # pragma: no cover - exercised by direct script execution
    from scan_public_safety import SECRET_PATTERNS
    from verify_release_version import release_versions, verify_release_version

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_HIDDEN_IMPORTS = {
    "xpst",
    "xpst.cli",
    "xpst.desktop_app.backend",
    "xpst.desktop_app.models",
    "xpst.diagnostics",
    "xpst.providers",
    "xpst.readiness",
    "xpst.updater",
    "xpst.platforms.base",
    "xpst.platforms.youtube",
    "xpst.platforms.instagram",
    "xpst.platforms.x",
    "xpst.sources.base",
    "xpst.sources.local",
    "xpst.sources.tiktok",
    "xpst.sources.youtube",
    "xpst.sources.instagram",
    "xpst.sources.x",
    "xpst.utils.credentials",
    "PySide6.QtQuick",
    "PySide6.QtQuickControls2",
    "PySide6.QtQml",
    "PySide6.QtWidgets",
}

# Paths are relative to Contents. The media names match the directories
# created by scripts/fetch-media-binaries.sh. ``.exe`` alternatives allow the
# same verifier to inspect a Windows bundle unpacked by an installer tool.
_REQUIRED_RESOURCES: tuple[tuple[str, tuple[Path, ...], bool], ...] = (
    ("ui/index.html", (Path("Resources/ui/index.html"),), False),
    ("binaries/engine/xpst-engine", (Path("Resources/binaries/engine/xpst-engine"),), True),
    (
        "binaries/ffmpeg/ffmpeg",
        (Path("Resources/binaries/ffmpeg/ffmpeg"), Path("Resources/binaries/ffmpeg/ffmpeg.exe")),
        True,
    ),
    (
        "binaries/ffmpeg/ffprobe",
        (Path("Resources/binaries/ffmpeg/ffprobe"), Path("Resources/binaries/ffmpeg/ffprobe.exe")),
        True,
    ),
    (
        "binaries/ytdlp/yt-dlp",
        (Path("Resources/binaries/ytdlp/yt-dlp"), Path("Resources/binaries/ytdlp/yt-dlp.exe")),
        True,
    ),
)
_HOME_PATH_PATTERN = re.compile(
    r"(?i)(?:/(?:users|home)/[a-z0-9._-]+(?:/[a-z0-9._+~:@%=-]+)*)|"
    r"(?:[a-z]:[\\/]+users[\\/][a-z0-9._-]+(?:[\\/][a-z0-9._+~:@%=-]+)*)"
)
_EMAIL_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9._%+\-])[a-z0-9._%+\-]{1,64}@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
    r"(?![a-z0-9._%+\-])"
)
_PHONE_PATTERNS = (
    re.compile(r"(?<!\d)(?:\+?1[\s.-]*)?\(?[2-9]\d{2}\)?[\s.-]+\d{3}[\s.-]\d{4}(?!\d)"),
    re.compile(r"(?<!\d)\+\d{1,3}[\s.-]+(?:\d{2,4}[\s.-]){1,3}\d{3,4}(?!\d)"),
)
_GENERIC_SECRET_PATTERN = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|bearer)\b"
    r"\s*[:=]\s*[\"']?(?!your\b|example\b|test\b|none\b|null\b|changeme\b)[a-z0-9_\-\.]{16,}"
)
_JWT_PATTERN = re.compile(r"\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\b")


class ArtifactVerificationError(RuntimeError):
    """Raised when an artifact cannot be safely materialized for inspection."""


def _hidden_imports(text: str) -> set[str]:
    match = re.search(r"hiddenimports=\[(.*?)\],", text, flags=re.DOTALL)
    if not match:
        return set()
    return set(re.findall(r"""["']([^"'\n]+)["']""", match.group(1)))


def _check_spec(path: Path, root: Path = ROOT) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    hidden_imports = _hidden_imports(text)
    missing_imports = sorted(REQUIRED_HIDDEN_IMPORTS - hidden_imports)
    issues: list[str] = []

    if 'str(qml_dir), "xpst/desktop_app/qml"' not in text:
        issues.append("QML directory is not bundled into xpst/desktop_app/qml")
    if "src_dir / \"xpst\" / \"desktop_app\" / \"main.py\"" not in text:
        issues.append("Desktop main.py is not the PyInstaller entrypoint")

    if missing_imports:
        issues.append("Missing hidden imports: " + ", ".join(missing_imports))

    if path.name == "build_windows.spec":
        icon_path = root / "assets" / "icon.ico"
        if not icon_path.exists():
            issues.append("Windows icon asset is missing: assets/icon.ico")
        if "console=False" not in text:
            issues.append("Windows desktop build should be windowed with console=False")

    if path.name == "build_macos.spec":
        icon_path = root / "docs" / "assets" / "xpst-icon.icns"
        if not icon_path.exists():
            issues.append("macOS icon asset is missing: docs/assets/xpst-icon.icns")
        if "BUNDLE(" not in text or 'name="xPST.app"' not in text:
            issues.append("macOS spec does not create xPST.app")
        if "bundle_identifier=\"com.tysais.xpst\"" not in text:
            issues.append("macOS bundle identifier is missing")

    return {
        "path": str(path.relative_to(root)),
        "ok": not issues,
        "issues": issues,
    }


def _check_qt_lgpl_notice(root: Path) -> dict[str, Any]:
    """Verify the Qt/PySide6 LGPL notice and relink offer is present.

    Desktop bundles dynamically link Qt through PySide6, so LICENSING_REPORT.md
    requires the LGPL notice and a written relink offer to ship with the
    desktop artifact. A desktop package missing this notice is non-compliant.
    """
    notice = root / "NOTICES_QT_LGPL.md"
    issues: list[str] = []

    if not notice.exists():
        issues.append("Qt/PySide6 LGPL notice is missing: NOTICES_QT_LGPL.md")
        return {"path": "NOTICES_QT_LGPL.md", "ok": False, "issues": issues}

    text = notice.read_text(encoding="utf-8")
    if "LGPL" not in text:
        issues.append("Qt/PySide6 LGPL notice does not reference the LGPL")
    if "relink" not in text.lower():
        issues.append("Qt/PySide6 LGPL notice is missing a written relink offer")
    if "PySide6" not in text and "Qt" not in text:
        issues.append("Qt/PySide6 LGPL notice does not attribute Qt/PySide6")

    return {"path": "NOTICES_QT_LGPL.md", "ok": not issues, "issues": issues}


def verify_desktop_package(
    root: Path = ROOT,
    artifact: Path | None = None,
    evidence_output: Path | None = None,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """Return static checks, or artifact checks when ``artifact`` is supplied."""
    if artifact is not None:
        return verify_artifact(artifact, root=root, evidence_output=evidence_output, expected_version=expected_version)
    specs = [root / "build_windows.spec", root / "build_macos.spec"]
    results = [_check_spec(path, root) for path in specs]

    results.append(_check_qt_lgpl_notice(root))

    qml_pages = sorted((root / "src" / "xpst" / "desktop_app" / "qml" / "pages").glob("*.qml"))
    qml_issue = None if qml_pages else "No QML pages found"
    if qml_issue:
        results.append({"path": "src/xpst/desktop_app/qml/pages", "ok": False, "issues": [qml_issue]})

    return {
        "ok": all(item["ok"] for item in results),
        "checks": results,
        "qml_pages": [page.name for page in qml_pages],
    }


@dataclass(frozen=True)
class _MaterializedArtifact:
    artifact_type: str
    app_root: Path
    contents: Path


def _is_contents(path: Path) -> bool:
    return path.is_dir() and ((path / "Info.plist").is_file() or (path / "Resources").is_dir())


def _locate_bundle(root: Path) -> tuple[Path, Path]:
    """Find an app root and Contents directory below an extracted tree."""
    candidates: list[tuple[Path, Path]] = []
    if root.name.endswith(".app") and _is_contents(root / "Contents"):
        candidates.append((root, root / "Contents"))
    if root.name == "Contents" and _is_contents(root):
        candidates.append((root.parent, root))
    if _is_contents(root / "Contents"):
        candidates.append((root, root / "Contents"))

    for app in sorted(root.rglob("*.app")):
        if app.is_dir() and _is_contents(app / "Contents"):
            candidates.append((app, app / "Contents"))
    for contents in sorted(root.rglob("Contents")):
        if contents.is_dir() and _is_contents(contents):
            candidates.append((contents.parent, contents))

    # An unpacked Contents directory is also accepted when it was renamed or
    # extracted without its .app parent.
    if _is_contents(root):
        candidates.append((root.parent, root))

    unique: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    for app_root, contents in candidates:
        resolved = contents.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append((app_root, contents))
    if not unique:
        raise ArtifactVerificationError("artifact does not contain an unpacked .app/Contents bundle")
    return unique[0]


def _safe_extraction_target(root: Path, member_name: str) -> Path:
    normalized = member_name.replace("\\", "/")
    member = PurePosixPath(normalized)
    if member.is_absolute() or ".." in member.parts:
        raise ArtifactVerificationError(f"archive contains unsafe path: {member_name}")
    target = (root / Path(*member.parts)).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ArtifactVerificationError(f"archive contains unsafe path: {member_name}")
    return target


def _extract_zip(archive_path: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                target = _safe_extraction_target(destination, info.filename)
                mode = (info.external_attr >> 16) & 0o7777
                if stat.S_ISLNK(mode):
                    raise ArtifactVerificationError(f"archive contains unsupported symlink: {info.filename}")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod(mode or 0o755)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(mode or 0o644)
    except zipfile.BadZipFile as exc:
        raise ArtifactVerificationError(f"invalid ZIP archive: {archive_path.name}") from exc


def _extract_tar(archive_path: Path, destination: Path) -> None:
    try:
        with tarfile.open(archive_path, "r:*") as archive:
            members = archive.getmembers()
            for member in members:
                target = _safe_extraction_target(destination, member.name)
                if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    raise ArtifactVerificationError(f"archive contains unsupported link/device: {member.name}")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod(member.mode & 0o7777 or 0o755)
                    continue
                if not member.isfile():
                    raise ArtifactVerificationError(f"archive contains unsupported entry: {member.name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ArtifactVerificationError(f"archive entry cannot be read: {member.name}")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(member.mode & 0o7777 or 0o644)
    except tarfile.TarError as exc:
        raise ArtifactVerificationError(f"invalid tar archive: {archive_path.name}") from exc


def _run_extractor(command: list[str], cwd: Path, error_prefix: str) -> None:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=180, check=False)
    if result.returncode != 0:
        raise ArtifactVerificationError(f"{error_prefix} (exit {result.returncode})")


@contextlib.contextmanager
def _materialize_artifact(artifact: Path) -> Iterator[_MaterializedArtifact]:
    """Yield an inspectable bundle, cleaning temporary extraction/mounts."""
    artifact = artifact.expanduser().resolve()
    if not artifact.exists():
        raise ArtifactVerificationError(f"artifact does not exist: {artifact}")

    if artifact.is_dir():
        app_root, contents = _locate_bundle(artifact)
        artifact_type = "app" if artifact.name.endswith(".app") else "bundle"
        yield _MaterializedArtifact(artifact_type, app_root, contents)
        return

    with tempfile.TemporaryDirectory(prefix="xpst-artifact-verify-") as temp_name:
        temp_root = Path(temp_name)
        name = artifact.name.lower()
        if name.endswith(".dmg"):
            if platform.system() != "Darwin" or shutil.which("hdiutil") is None:
                raise ArtifactVerificationError("DMG inspection requires macOS hdiutil")
            mountpoint = temp_root / "mount"
            mountpoint.mkdir()
            result = subprocess.run(
                ["hdiutil", "attach", "-nobrowse", "-readonly", "-noverify", "-mountpoint", str(mountpoint), str(artifact)],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            if result.returncode != 0:
                raise ArtifactVerificationError(f"DMG could not be mounted (exit {result.returncode})")
            try:
                app_root, contents = _locate_bundle(mountpoint)
                yield _MaterializedArtifact("dmg", app_root, contents)
            finally:
                subprocess.run(
                    ["hdiutil", "detach", str(mountpoint), "-force"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            return

        extracted = temp_root / "extracted"
        extracted.mkdir()
        if zipfile.is_zipfile(artifact):
            _extract_zip(artifact, extracted)
            artifact_type = "zip"
        elif tarfile.is_tarfile(artifact):
            _extract_tar(artifact, extracted)
            artifact_type = "installer"
        elif name.endswith(".pkg") and shutil.which("pkgutil"):
            _run_extractor(["pkgutil", "--expand-full", str(artifact), str(extracted)], temp_root, "PKG could not be expanded")
            artifact_type = "installer"
        elif name.endswith(".appimage") and os.access(artifact, os.X_OK):
            _run_extractor([str(artifact), "--appimage-extract"], temp_root, "AppImage could not be extracted")
            extracted = temp_root / "squashfs-root"
            artifact_type = "installer"
        else:
            extractor = shutil.which("7z") or shutil.which("7zz")
            if extractor and name.endswith((".exe", ".msi")):
                _run_extractor([extractor, "x", "-y", f"-o{extracted}", str(artifact)], temp_root, "installer could not be extracted")
                artifact_type = "installer"
            else:
                raise ArtifactVerificationError(
                    "opaque installer cannot be inspected; provide an unpacked bundle or an installer with an available extractor"
                )
        app_root, contents = _locate_bundle(extracted)
        yield _MaterializedArtifact(artifact_type, app_root, contents)


def _hash_path(path: Path) -> dict[str, str]:
    """Hash a file, or a directory's canonical path/mode/content manifest."""
    hashers = {name: hashlib.new(name) for name in ("sha256", "sha512")}

    def update(data: bytes) -> None:
        for hasher in hashers.values():
            hasher.update(data)

    if path.is_file():
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                update(chunk)
    elif path.is_dir():
        update(b"xPST artifact directory manifest\0")
        for child in sorted(path.rglob("*")):
            relative = child.relative_to(path).as_posix().encode("utf-8")
            mode = child.lstat().st_mode & 0o7777
            update(len(relative).to_bytes(8, "big") + relative + mode.to_bytes(4, "big"))
            if child.is_symlink():
                update(os.readlink(child).encode("utf-8"))
            elif child.is_file():
                with child.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        update(chunk)
    else:
        raise ArtifactVerificationError(f"artifact is neither a file nor a directory: {path}")
    return {name: hasher.hexdigest() for name, hasher in hashers.items()}


def _path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(child.stat().st_size for child in path.rglob("*") if child.is_file())


def _relative_entry(path: Path, app_root: Path) -> str:
    return path.relative_to(app_root).as_posix()


def _check_required_resources(contents: Path, app_root: Path) -> dict[str, Any]:
    missing: list[str] = []
    issues: list[str] = []
    present: list[str] = []
    for label, candidates, _ in _REQUIRED_RESOURCES:
        found = next((contents / candidate for candidate in candidates if (contents / candidate).is_file()), None)
        if found is None:
            missing.append(label)
            issues.append(f"required resource is missing: Contents/{label}")
        else:
            present.append(_relative_entry(found, app_root))
    return {
        "id": "required_resources",
        "ok": not issues,
        "required": [label for label, _, _ in _REQUIRED_RESOURCES],
        "present": present,
        "missing": missing,
        "issues": issues,
    }


def _check_permissions(contents: Path, app_root: Path) -> dict[str, Any]:
    issues: list[str] = []
    checked_files = 0
    if not contents.exists():
        issues.append("bundle Contents directory is missing")
    else:
        for path in sorted(contents.rglob("*")):
            try:
                mode = path.lstat().st_mode
            except OSError as exc:
                issues.append(f"cannot inspect permissions for {_relative_entry(path, app_root)}: {exc.__class__.__name__}")
                continue
            relative = _relative_entry(path, app_root)
            if stat.S_ISLNK(mode):
                issues.append(f"symbolic links are not allowed in bundle: {relative}")
                continue
            if mode & 0o6000:
                issues.append(f"setuid/setgid permission is not allowed: {relative}")
            if mode & 0o022:
                issues.append(f"group/world-writable permission is not allowed: {relative}")
            if stat.S_ISREG(mode):
                checked_files += 1

    for label, candidates, executable in _REQUIRED_RESOURCES:
        if not executable:
            continue
        path = next((contents / candidate for candidate in candidates if (contents / candidate).is_file()), None)
        if path is not None and not os.access(path, os.X_OK):
            issues.append(f"required executable is not executable: Contents/{label}")

    main_executable = contents / "MacOS" / "xPST"
    if main_executable.is_file() and not os.access(main_executable, os.X_OK):
        issues.append("application executable is not executable: Contents/MacOS/xPST")

    return {
        "id": "permissions",
        "ok": not issues,
        "checked_files": checked_files,
        "issues": issues,
    }


def _text_fragments(data: bytes) -> list[str]:
    if b"\x00" in data:
        return [chunk.decode("ascii", errors="ignore") for chunk in re.findall(rb"[\x20-\x7e]{4,}", data)]
    return [data.decode("utf-8", errors="ignore")]


def _privacy_findings(contents: Path, app_root: Path) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for path in sorted(contents.rglob("*")):
        try:
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                fragments = [os.readlink(path)]
            elif stat.S_ISREG(mode):
                fragments = _text_fragments(path.read_bytes())
            else:
                continue
        except OSError:
            continue

        relative = _relative_entry(path, app_root)
        categories: set[str] = set()
        for text in fragments:
            if _HOME_PATH_PATTERN.search(text):
                categories.add("absolute_home_path")
            if _EMAIL_PATTERN.search(text):
                categories.add("email")
            if any(pattern.search(text) for pattern in _PHONE_PATTERNS):
                categories.add("phone")
            if any(pattern.search(text) for _, pattern in SECRET_PATTERNS):
                categories.add("secret")
            if _GENERIC_SECRET_PATTERN.search(text) or _JWT_PATTERN.search(text):
                categories.add("secret")
        for category in sorted(categories):
            findings.append(
                {
                    "path": relative,
                    "kind": category,
                    "detail": f"bundle content contains a possible {category.replace('_', ' ')}",
                }
            )
    return findings


def _check_privacy(contents: Path, app_root: Path) -> dict[str, Any]:
    findings = _privacy_findings(contents, app_root)
    return {
        "id": "privacy",
        "ok": not findings,
        "finding_count": len(findings),
        "findings": findings,
        "issues": [f"possible {finding['kind']} in {finding['path']}" for finding in findings],
    }


def _read_bundle_version(contents: Path) -> tuple[str | None, list[str]]:
    plist_path = contents / "Info.plist"
    if not plist_path.is_file():
        return None, ["bundle version is unavailable: Contents/Info.plist is missing"]
    try:
        with plist_path.open("rb") as source:
            data = plistlib.load(source)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        return None, [f"bundle version cannot be read from Contents/Info.plist: {exc.__class__.__name__}"]
    versions = [str(data[key]) for key in ("CFBundleShortVersionString", "CFBundleVersion") if data.get(key) is not None]
    if not versions:
        return None, ["bundle version is missing from Contents/Info.plist"]
    issues = []
    if len(set(versions)) > 1:
        issues.append("CFBundleShortVersionString and CFBundleVersion do not match")
    return versions[0], issues


def _check_version(root: Path, contents: Path, expected_version: str | None) -> dict[str, Any]:
    issues: list[str] = []
    try:
        sources = release_versions(root)
        expected = expected_version or sources["python"]
        try:
            verify_release_version(expected, root=root)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            issues.append(f"release source version verification failed: {exc}")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        sources = {}
        expected = expected_version
        issues.append(f"release source versions cannot be read: {exc}")

    bundle_version, bundle_issues = _read_bundle_version(contents)
    issues.extend(bundle_issues)
    if expected is not None and bundle_version is not None and bundle_version != expected:
        issues.append(f"bundle version {bundle_version} does not match expected {expected}")
    return {
        "id": "version",
        "ok": not issues,
        "expected": expected,
        "bundle": bundle_version,
        "sources": sources,
        "issues": issues,
    }


def _redact_output(output: str) -> str:
    home = str(Path.home())
    redacted = output.replace(home, "<redacted-home>")
    return _HOME_PATH_PATTERN.sub("<redacted-home>", redacted)


def _run_signature_command(command: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "command": [Path(command[0]).name, *command[1:-1], "<redacted-artifact>"],
            "returncode": None,
            "ok": False,
            "output": exc.__class__.__name__,
        }
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    return {
        "command": [Path(command[0]).name, *command[1:-1], "<redacted-artifact>"],
        "returncode": result.returncode,
        "ok": result.returncode == 0,
        "output": _redact_output(output),
    }


def _check_signature(app_root: Path, artifact_type: str, artifact_path: Path | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": "signing",
        "ok": True,
        "required": False,
        "signed": False,
        "notarized": "unknown",
        "verified_by": "unknown",
        "codesign": {"status": "not-run"},
        "spctl": {"status": "not-run"},
    }
    if platform.system() != "Darwin" or artifact_type not in {"app", "bundle", "zip", "dmg", "installer"}:
        result["reason"] = "codesign/spctl checks are only run for macOS artifacts on macOS"
        return result

    codesign = shutil.which("codesign")
    spctl = shutil.which("spctl")
    if codesign:
        verify = _run_signature_command([codesign, "--verify", "--deep", "--strict", str(app_root)])
        display = _run_signature_command([codesign, "--display", "--verbose=4", str(app_root)])
        result["codesign"] = {"verify": verify, "display": display}
        result["signed"] = bool(verify["ok"])
        if "Signature=adhoc" in display.get("output", ""):
            result["signature_type"] = "adhoc"
        elif result["signed"]:
            result["signature_type"] = "signed"
    else:
        result["codesign"] = {"status": "not-installed"}

    if spctl:
        assess = _run_signature_command([spctl, "--assess", "--type", "execute", "--verbose=4", str(app_root)])
        result["spctl"] = assess
        assessments = [assess]
        if artifact_type == "dmg" and artifact_path is not None:
            dmg_assess = _run_signature_command(
                [spctl, "--assess", "--type", "open", "--context", "context:primary-signature", str(artifact_path)]
            )
            result["spctl_dmg"] = dmg_assess
            assessments.append(dmg_assess)
        if any(item["ok"] and "notarized" in item.get("output", "").lower() for item in assessments):
            result["notarized"] = True
        elif all(not item["ok"] for item in assessments):
            result["notarized"] = False
    else:
        result["spctl"] = {"status": "not-installed"}

    verifiers = []
    if result["signed"]:
        verifiers.append("codesign")
    if result["notarized"] is True:
        verifiers.append("spctl")
    if verifiers:
        result["verified_by"] = "+".join(verifiers)
    return result


def _artifact_evidence(
    artifact: Path,
    artifact_type: str,
    hashes: dict[str, str],
    checks: list[dict[str, Any]],
    signature: dict[str, Any],
) -> dict[str, Any]:
    version_check = next(check for check in checks if check["id"] == "version")
    resources_check = next(check for check in checks if check["id"] == "required_resources")
    permissions_check = next(check for check in checks if check["id"] == "permissions")
    privacy_check = next(check for check in checks if check["id"] == "privacy")
    attestation = {
        "signed": signature["signed"],
        "notarized": signature["notarized"],
        "verified_by": signature["verified_by"],
    }
    return {
        "schema_version": 2,
        "project": "xpst",
        "artifact": {
            "filename": artifact.name,
            "type": artifact_type,
            "size": _path_size(artifact),
        },
        "quality_checks": {
            "run_by_release_script": True,
            "artifact_verifier": "scripts/verify_desktop_package.py",
        },
        "hashes": hashes,
        "version": {
            "expected": version_check["expected"],
            "bundle": version_check["bundle"],
            "sources": version_check["sources"],
            "verified": version_check["ok"],
        },
        "required_resources": {
            "verified": resources_check["ok"],
            "required": resources_check["required"],
            "missing": resources_check["missing"],
        },
        "permissions": {
            "verified": permissions_check["ok"],
            "checked_files": permissions_check["checked_files"],
        },
        "privacy": {
            "verified": privacy_check["ok"],
            "finding_count": privacy_check["finding_count"],
            "findings": privacy_check["findings"],
        },
        # Keep both the flat fields and a named block so consumers can migrate
        # without inferring signing state from an old hashes-only schema.
        "signed": attestation["signed"],
        "notarized": attestation["notarized"],
        "verified_by": attestation["verified_by"],
        "attestation": attestation,
        "signing": signature,
    }


def verify_artifact(
    artifact: Path,
    root: Path = ROOT,
    evidence_output: Path | None = None,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """Verify a built desktop artifact and optionally write ``RELEASE_EVIDENCE``.

    The artifact itself is never modified. Archives are extracted into a
    temporary directory and DMGs are mounted read-only. Evidence contains
    relative bundle paths and redacted tool output, not the local artifact
    path or matched personal data.
    """
    artifact = Path(artifact).expanduser().resolve()
    root = Path(root).expanduser().resolve()
    hashes: dict[str, str] = {}
    artifact_type = "unknown"
    checks: list[dict[str, Any]] = []
    signature: dict[str, Any] = _check_signature(Path("."), artifact_type)
    materialization_issue: str | None = None

    try:
        hashes = _hash_path(artifact)
    except (OSError, ArtifactVerificationError) as exc:
        materialization_issue = str(exc)

    try:
        with _materialize_artifact(artifact) as materialized:
            artifact_type = materialized.artifact_type
            resources = _check_required_resources(materialized.contents, materialized.app_root)
            version = _check_version(root, materialized.contents, expected_version)
            permissions = _check_permissions(materialized.contents, materialized.app_root)
            privacy = _check_privacy(materialized.contents, materialized.app_root)
            signature = _check_signature(materialized.app_root, artifact_type, artifact)
            checks.extend(
                [
                    resources,
                    version,
                    {
                        "id": "hashes",
                        "ok": bool(hashes),
                        "hashes": hashes,
                        "issues": [] if hashes else [materialization_issue or "artifact hashes could not be computed"],
                    },
                    permissions,
                    privacy,
                ]
            )
    except ArtifactVerificationError as exc:
        materialization_issue = str(exc)
        checks.extend(
            [
                {
                    "id": "required_resources",
                    "ok": False,
                    "required": [label for label, _, _ in _REQUIRED_RESOURCES],
                    "present": [],
                    "missing": [label for label, _, _ in _REQUIRED_RESOURCES],
                    "issues": [materialization_issue],
                },
                {"id": "version", "ok": False, "expected": expected_version, "bundle": None, "sources": {}, "issues": [materialization_issue]},
                {"id": "hashes", "ok": bool(hashes), "hashes": hashes, "issues": [] if hashes else [materialization_issue]},
                {"id": "permissions", "ok": False, "checked_files": 0, "issues": [materialization_issue]},
                {"id": "privacy", "ok": False, "finding_count": 0, "findings": [], "issues": [materialization_issue]},
            ]
        )

    if not any(check["id"] == "hashes" for check in checks):
        checks.append({"id": "hashes", "ok": bool(hashes), "hashes": hashes, "issues": [] if hashes else [materialization_issue or "artifact hashes could not be computed"]})

    ok = all(check["ok"] for check in checks)
    result: dict[str, Any] = {
        "ok": ok,
        "artifact": {
            "path": str(artifact),
            "filename": artifact.name,
            "type": artifact_type,
            "size": _path_size(artifact),
        },
        "checks": checks,
    }
    evidence = _artifact_evidence(artifact, artifact_type, hashes, checks, signature)
    result["evidence"] = evidence

    if evidence_output is None:
        evidence_output = artifact.parent / "RELEASE_EVIDENCE.json"
    evidence_output = Path(evidence_output).expanduser().resolve()
    evidence_output.parent.mkdir(parents=True, exist_ok=True)
    evidence_output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["evidence_output"] = str(evidence_output)
    return result


# Descriptive alias for callers that prefer the artifact-specific name.
verify_desktop_artifact = verify_artifact


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify xPST desktop packaging inputs or a built artifact")
    parser.add_argument("--artifact", type=Path, help="Built .app/.dmg/.zip/installer or unpacked bundle to verify")
    parser.add_argument("--root", type=Path, default=ROOT, help="Repository root containing release version sources")
    parser.add_argument(
        "--evidence-output",
        "--evidence",
        "--output",
        dest="evidence_output",
        type=Path,
        help="Path for RELEASE_EVIDENCE.json (defaults beside the artifact)",
    )
    parser.add_argument("--expected-version", "--expected", dest="expected_version", help="Expected bundle version")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    if args.artifact:
        result = verify_artifact(args.artifact, root=args.root, evidence_output=args.evidence_output, expected_version=args.expected_version)
    else:
        result = verify_desktop_package(args.root)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for check in result["checks"]:
            status = "ok" if check["ok"] else "failed"
            print(f"{check.get('path', check.get('id', 'check'))}: {status}")
            for issue in check.get("issues", []):
                print(f"  - {issue}")
        if args.artifact:
            print(f"RELEASE_EVIDENCE.json: {result['evidence_output']}")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
