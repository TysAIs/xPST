"""Scan repository files, built artifacts and git history for personal data, secrets and unsafe content.

The scanner has three modes:

* **tree** (default) - scan the publishable working-tree files (``git ls-files``
  tracked + untracked, minus ignored).
* **artifacts** - scan built artifacts: ``.dmg``, ``.zip``/``.whl``, ``.tar.gz``,
  ``.app`` bundles and PyInstaller ``onedir`` directories, by walking their text
  members (and extracting printable strings from executable members).
* **history** - walk every commit and blob reachable from any ref
  (``git rev-list --objects --all`` + ``git cat-file``) and report which
  categories appear in how many commits.

Privacy guarantee: findings never carry the matched value. A finding records the
file (or artifact member), the category/kind and a neutral detail such as an
offset or occurrence count. Nothing that reaches stdout, a JSON report or a
committed file ever contains the matched string.

Categories detected:

``sensitive_file``, ``private_key``, ``aws_access_key``, ``aws_secret_key``,
``gcp_service_account``, ``google_api_key``, ``github_token``, ``api_token``,
``home_path_macos``, ``home_path_linux``, ``home_path_windows``,
``personal_email``, ``phone_number``, ``machine_name``, ``user_name``.

Absolute home paths for any OS use a shared placeholder allow-list so that
documentation like ``/Users/<user>``, ``C:\\Users\\tester`` or ``/home/xpst``
stays clean while a real machine username is reported.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only (runtime uses postponed evaluation)
    from collections.abc import Iterable, Iterator

ROOT = Path(__file__).resolve().parents[1]
MAX_SCAN_BYTES = 2_000_000
MAX_BINARY_SCAN_BYTES = 8_000_000
MAX_ARTIFACT_MEMBER_BYTES = 64_000_000
PRINTABLE_MIN_RUN = 6

# Artifact-only categories that are reported but do not gate CI: built bundles
# vendor third-party ``*.dist-info/METADATA`` whose maintainer emails/phones are
# legitimate published metadata, not leaks we control.
ARTIFACT_INFORMATIONAL_KINDS = frozenset({"personal_email", "phone_number"})

SENSITIVE_FILE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"(^|[/\\])\.env(\..*)?$",
        r"(^|[/\\])credentials([/\\]|$)",
        r"(^|[/\\])sessions([/\\]|$)",
        r"(^|[/\\])\.xpst([/\\]|$)",
        r"(^|[/\\])\.crosspstr([/\\]|$)",
        r"(^|[/\\])state(_.*)?\.json$",
        r"(^|[/\\])quotas\.json$",
        r".*\.(pem|key|p12|pfx|keystore)$",
        r".*(token|cookies|session|client_secret|credentials).*\.json$",
    ]
]

# --- Neutral placeholders -------------------------------------------------
# Tokens that appear in documentation, tests and templates and must never be
# reported. Matching is case-insensitive on the whole segment.
PLACEHOLDER_TOKENS = {
    "xpst",
    "user",
    "users",
    "user_name",
    "username",
    "name",
    "you",
    "your",
    "yourname",
    "your-name",
    "your_name",
    "your-user",
    "test",
    "tests",
    "tester",
    "testuser",
    "test-user",
    "test_user",
    "alice",
    "bob",
    "example",
    "demo",
    "sample",
    "dummy",
    "fake",
    "placeholder",
    "ci",
    "runner",
    "runneradmin",
    "github",
    "actions",
    "root",
    "admin",
    "administrator",
    "local",
    "host",
    "machine",
    "someone",
    "person",
    "me",
    "owner",
    "dev",
    "developer",
    "maintainer",
    "maint",
    "contributor",
    "shared",
    "public",
    "home",
    "app",
    "appdata",
    "userprofile",
    "homepath",
    "systemprofile",
}
_PLACEHOLDER_PREFIXES = (
    "your",
    "my",
    "some",
    "test",
    "dummy",
    "fake",
    "placeholder",
    "example",
    "sample",
    "synthetic",
    "redacted",
    "anon",
    "xpst",
)


def _is_placeholder(segment: str) -> bool:
    """Return True when a path/user segment is a documented neutral placeholder."""
    if not segment:
        return True
    seg = segment.strip().strip("/\\")
    if not seg:
        return True
    if any(ch in seg for ch in "<>$%{}*?"):
        return True
    lowered = seg.lower()
    if lowered in PLACEHOLDER_TOKENS:
        return True
    if lowered.startswith("$") or lowered.startswith("%"):
        return True
    if seg.isdigit():
        return True
    for prefix in _PLACEHOLDER_PREFIXES:
        if lowered.startswith(prefix):
            return True
    # Anchor a home-path segment only when it looks like an account name.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", seg):
        # Windows user segments may contain spaces, so keep letter-initial ones.
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9._ -]*", seg):
            return True
    return False


# --- Secret / credential shapes ------------------------------------------
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP |ENCRYPTED )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("aws_secret_key", re.compile(r"(?i)\baws_?secret_?access_?key\b\s*[:=]\s*[\"']?([A-Za-z0-9/+=]{40})")),
    ("gcp_service_account", re.compile(r'"type"\s*:\s*"service_account"')),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]|ghs)_[A-Za-z0-9_]{36,}\b")),
    (
        "api_token",
        re.compile(
            r"\b(?:"
            r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"
            r"|xox[baprs]-[0-9A-Za-z-]{10,}"
            r"|xapp-[0-9A-Za-z-]{10,}"
            r"|glpat-[A-Za-z0-9_-]{20,}"
            r"|npm_[A-Za-z0-9]{36}"
            r"|pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{10,}"
            r")\b"
        ),
    ),
]

# --- Personal data shapes -------------------------------------------------
HOME_PATH_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("home_path_macos", re.compile(r"(?<![\w])/Users/([A-Za-z0-9._-]+)")),
    ("home_path_linux", re.compile(r"(?<![\w])/home/([A-Za-z0-9._-]+)")),
    (
        "home_path_windows",
        re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:\\{1,2}(?:Users|Documents and Settings)\\{1,2}([A-Za-z0-9._ -]+)"),
    ),
]

EMAIL_RE = re.compile(
    r"(?<![/\w.])"
    r"([A-Za-z0-9._%+-]+)@"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+)"
)
# TLDs reserved for documentation/tests (RFC 2606/6761) are never personal.
EMAIL_ALLOWED_TLDS = {"test", "invalid", "localhost", "example", "local"}
EMAIL_ALLOWED_DOMAINS = {
    "users.noreply.github.com",
    "noreply.github.com",
    "github.com",
    "example.com",
    "example.org",
    "example.net",
    "localhost",
    "xpst.local",
    "opensource.local",
    "xpst.dev",
    "xpst.app",
    "nousresearch.com",
    "python.org",
    "pypi.org",
    "gnu.org",
    "opensource.org",
}
# TLDs that are really file extensions (e.g. "icons/128x128@2x.png").
NON_EMAIL_TLDS = {
    "png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp", "tiff",
    "json", "py", "js", "ts", "txt", "md", "html", "css", "xml", "yaml",
    "yml", "toml", "sh", "exe", "dll", "so", "dylib", "app", "dmg", "zip",
    "gz", "whl", "rar", "mp4", "mov", "wav", "mp3", "pdf", "rst", "lock",
}

# Phone numbers require separators so that version strings, dates, hashes and
# long numeric runs are not reported.
PHONE_RE = re.compile(
    r"(?<![\w.])(?:\+?1[ .\-])?(?:\([2-9]\d{2}\)|[2-9]\d{2})[ .\-]\d{3}[ .\-]\d{4}(?![\w.])"
)

MACHINE_NAME_RE = re.compile(
    r"""(?ix)
    (?:
        (?:host(?:name)?|nodename|machine|server)\s*[:=]\s*['"]?
            ([A-Za-z0-9][A-Za-z0-9.-]*\.(?:local|lan|home|internal|localdomain))
      | (?<![\w.-])([A-Z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+\.(?:local|lan|home|internal|localdomain))\b
    )
    """
)

# Only uppercase environment-style assignments are treated as machine/user
# names; a lowercase ``username = ...`` in application code is a config field,
# not an identity. Real account names leaked inside paths are reported by the
# home-path categories above, and known personal identifiers can be supplied
# through the ``XPST_PUBLIC_SAFETY_DENYLIST`` environment variable.
USER_NAME_RE = re.compile(
    r"""(?x)(?<![A-Za-z0-9_])(?:USER|USERNAME|LOGNAME)\s*[:=]\s*['"]?([A-Za-z][A-Za-z0-9._-]{2,})"""
)
_USER_NAME_NON_VALUES = ("os", "environ", "getenv", "none", "null", "self", "str", "param", "arg", "env")

_DENYLIST_ENV = "XPST_PUBLIC_SAFETY_DENYLIST"


def _denylist() -> list[re.Pattern[str]]:
    raw = os.environ.get(_DENYLIST_ENV, "")
    tokens = [t for t in re.split(r"[,\s]+", raw) if t]
    return [re.compile(r"(?<![\w.-])" + re.escape(t) + r"(?![\w.-])", re.IGNORECASE) for t in tokens]


@dataclass
class Finding:
    path: str
    kind: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "kind": self.kind, "detail": self.detail}


def _summarize(findings: Iterable[Finding]) -> dict[str, Any]:
    by_category: dict[str, int] = {}
    by_file: dict[str, int] = {}
    for finding in findings:
        by_category[finding.kind] = by_category.get(finding.kind, 0) + 1
        by_file[finding.path] = by_file.get(finding.path, 0) + 1
    return {"counts_by_category": by_category, "counts_by_file": by_file}


# --- Core text scanner ---------------------------------------------------
def _scan_text(
    text: str,
    label: str,
    *,
    denylist: list[re.Pattern[str]] | None = None,
    binary: bool = False,
) -> list[Finding]:
    """Scan a single text blob. Never returns the matched value.

    ``binary`` marks text recovered from executable/compressed members: email and
    phone shapes are skipped there because such data is almost always noise.
    """
    findings: list[Finding] = []
    denylist = denylist or []

    for kind, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            findings.append(Finding(label, kind, f"Matched high-confidence pattern near offset {match.start()}."))

    for kind, pattern in HOME_PATH_PATTERNS:
        for match in pattern.finditer(text):
            segment = match.group(1)
            if _is_placeholder(segment):
                continue
            findings.append(Finding(label, kind, "Absolute home path with a non-placeholder user segment."))

    if not binary:
        for match in EMAIL_RE.finditer(text):
            local, domain = match.group(1), match.group(2).lower()
            tld = domain.rsplit(".", 1)[-1]
            if len(local) < 2 or not any(ch.isalpha() for ch in local):
                # Compressed/binary data frequently yields single-char local parts.
                continue
            local_head = re.split(r"[._%+-]", local.lower())[0]
            if local_head in PLACEHOLDER_TOKENS or local_head.startswith(_PLACEHOLDER_PREFIXES):
                # Documentation placeholders like your.email@domain.
                continue
            if any(label.isdigit() for label in domain.split(".")):
                continue
            if tld in NON_EMAIL_TLDS or tld in EMAIL_ALLOWED_TLDS:
                continue
            if domain in EMAIL_ALLOWED_DOMAINS:
                continue
            findings.append(Finding(label, "personal_email", "Non-project email address."))

        for match in PHONE_RE.finditer(text):
            value = match.group(0)
            digits = re.sub(r"\D", "", value)
            if len(digits) not in (10, 11):
                continue
            findings.append(Finding(label, "phone_number", "Phone-number-shaped value."))

    for match in MACHINE_NAME_RE.finditer(text):
        value = (match.group(1) or match.group(2) or "").strip()
        host = value.split(".")[0]
        if _is_placeholder(host):
            continue
        findings.append(Finding(label, "machine_name", "Hostname-style machine name."))

    for match in USER_NAME_RE.finditer(text):
        segment = match.group(1)
        lowered = segment.lower()
        if lowered in _USER_NAME_NON_VALUES or any(lowered.startswith(p) for p in _USER_NAME_NON_VALUES):
            continue
        if _is_placeholder(segment):
            continue
        findings.append(Finding(label, "user_name", "Explicit machine/user-name assignment."))

    for pattern in denylist:
        if pattern.search(text):
            findings.append(Finding(label, "user_name", "Matched configured personal-identifier deny list."))

    return findings


def scan_text(text: str, label: str) -> list[Finding]:
    """Public wrapper used by tests and CI to scan an in-memory blob."""
    return _scan_text(text, label, denylist=_denylist())


# --- Working-tree scan ---------------------------------------------------
def _git_publishable_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return [path for path in root.rglob("*") if path.is_file() and ".git" not in path.parts]
    return [root / line.strip() for line in result.stdout.splitlines() if line.strip()]


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:4096]


def _read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > MAX_SCAN_BYTES:
        data = data[:MAX_SCAN_BYTES]
    return data.decode("utf-8", errors="ignore")


def scan_public_safety(root: Path = ROOT, paths: list[Path] | None = None) -> dict[str, Any]:
    """Return publishable-file scan results (no matched values)."""
    root = root.resolve()
    files = paths if paths is not None else _git_publishable_files(root)
    denylist = _denylist()
    findings: list[Finding] = []
    scanned = 0

    for path in files:
        path = Path(path)
        path = (root / path).resolve() if not path.is_absolute() else path.resolve()
        if not path.exists() or not path.is_file():
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = str(path)

        if any(pattern.match(rel) for pattern in SENSITIVE_FILE_PATTERNS):
            findings.append(
                Finding(rel, "sensitive_file", "Sensitive runtime/credential-looking file would be published.")
            )
            continue

        try:
            data = path.read_bytes()
        except OSError:
            continue
        if len(data) > MAX_SCAN_BYTES and _is_binary(data):
            if len(data) > MAX_BINARY_SCAN_BYTES:
                continue
            data = data[:MAX_BINARY_SCAN_BYTES]
        elif len(data) > MAX_SCAN_BYTES:
            data = data[:MAX_SCAN_BYTES]
        scanned += 1
        # Binary assets (icons, archives, fonts) can embed absolute build paths
        # in their metadata; scan extracted printable strings, not the raw bytes.
        binary = _is_binary(data)
        text = "\n".join(_printable_runs(data)) if binary else data.decode("utf-8", errors="ignore")
        findings.extend(_scan_text(text, rel, denylist=denylist, binary=binary))

    summary = _summarize(findings)
    return {
        "mode": "tree",
        "ok": not findings,
        "scanned_files": scanned,
        "findings": [f.to_dict() for f in findings],
        **summary,
    }


# --- Artifact scan -------------------------------------------------------
TEXT_SUFFIXES = {
    ".py", ".txt", ".md", ".rst", ".json", ".js", ".mjs", ".cjs", ".ts", ".qml",
    ".qmltypes", ".html", ".css", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sh",
    ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".rs", ".c", ".h", ".cpp", ".java",
    ".kt", ".swift", ".xml", ".plist", ".strings", ".po", ".pot", ".csv", ".tsv",
    ".lock", ".spec", ".conf", ".env", ".service", ".desktop", ".js.map",
}


def _printable_runs(data: bytes, min_run: int = PRINTABLE_MIN_RUN) -> Iterator[str]:
    """Yield printable ASCII runs from a binary blob (a cheap ``strings``)."""
    current: list[str] = []
    for byte in data:
        if 0x20 <= byte < 0x7F or byte in (0x09,):
            current.append(chr(byte))
        else:
            if len(current) >= min_run:
                yield "".join(current)
            current = []
    if len(current) >= min_run:
        yield "".join(current)


def _scan_member_bytes(data: bytes, label: str, denylist: list[re.Pattern[str]]) -> list[Finding]:
    if len(data) > MAX_ARTIFACT_MEMBER_BYTES:
        data = data[:MAX_ARTIFACT_MEMBER_BYTES]
    # Binary/executable members are scanned as extracted printable strings.
    binary = _is_binary(data)
    text = "\n".join(_printable_runs(data)) if binary else data.decode("utf-8", errors="ignore")
    return _scan_text(text, label, denylist=denylist, binary=binary)


def _iter_zip_members(path: Path) -> Iterator[tuple[str, bytes]]:
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            try:
                yield info.filename, archive.read(info)
            except (RuntimeError, zipfile.BadZipFile, OSError):
                continue


def _iter_tar_members(path: Path) -> Iterator[tuple[str, bytes]]:
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            try:
                handle = archive.extractfile(member)
            except (tarfile.TarError, OSError):
                continue
            if handle is None:
                continue
            yield member.name, handle.read()


def _iter_dir_members(path: Path) -> Iterator[tuple[str, bytes]]:
    for child in sorted(path.rglob("*")):
        if not child.is_file() or child.is_symlink():
            continue
        try:
            size = child.stat().st_size
        except OSError:
            continue
        if size > MAX_ARTIFACT_MEMBER_BYTES:
            continue
        try:
            data = child.read_bytes()
        except OSError:
            continue
        yield child.relative_to(path).as_posix(), data


def _iter_dmg_members(path: Path) -> Iterator[tuple[str, bytes]]:
    """Mount a .dmg read-only and walk it; fall back to raw string extraction."""
    if sys.platform != "darwin" or shutil.which("hdiutil") is None:
        yield path.name, path.read_bytes()
        return
    mountpoint = Path(tempfile.mkdtemp(prefix="xpst-dmg-"))
    attached = False
    try:
        result = subprocess.run(
            ["hdiutil", "attach", str(path), "-nobrowse", "-readonly", "-mountpoint", str(mountpoint)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            yield path.name, path.read_bytes()
            return
        attached = True
        yield from _iter_dir_members(mountpoint)
    finally:
        if attached:
            subprocess.run(["hdiutil", "detach", str(mountpoint), "-force"], capture_output=True, check=False)
        shutil.rmtree(mountpoint, ignore_errors=True)


def _iter_artifact_members(path: Path) -> Iterator[tuple[str, bytes]]:
    suffix = path.suffix.lower()
    if path.is_dir():
        yield from _iter_dir_members(path)
    elif suffix in {".zip", ".whl", ".egg", ".jar", ".apk"}:
        yield from _iter_zip_members(path)
    elif suffix in {".tar", ".gz", ".tgz", ".bz2", ".xz"}:
        yield from _iter_tar_members(path)
    elif suffix == ".dmg":
        yield from _iter_dmg_members(path)
    else:
        yield path.name, path.read_bytes()


def _split_artifact_findings(findings: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    """Split artifact findings into gating vs informational.

    Built bundles vendor third-party distributions (``*.dist-info/METADATA``)
    whose maintainer emails/phones are legitimate published metadata, so those
    two categories are reported but do not fail the artifact gate. Build-path,
    identity and credential leaks always gate.
    """
    gating = [f for f in findings if f.kind not in ARTIFACT_INFORMATIONAL_KINDS]
    informational = [f for f in findings if f.kind in ARTIFACT_INFORMATIONAL_KINDS]
    return gating, informational


def scan_artifact(path: Path, root: Path | None = None) -> dict[str, Any]:
    """Scan a built artifact (archive, .app tree or PyInstaller onedir)."""
    path = Path(path)
    denylist = _denylist()
    findings: list[Finding] = []
    scanned = 0
    members = 0
    if not path.exists():
        return {"mode": "artifact", "artifact": str(path), "ok": True, "scanned_files": 0, "findings": []}

    for name, data in _iter_artifact_members(path):
        members += 1
        label = f"{path}/{name}" if path.is_dir() else f"{path.name}!{name}"
        scanned += 1
        findings.extend(_scan_member_bytes(data, label, denylist))

    gating, informational = _split_artifact_findings(findings)
    summary = _summarize(gating)
    return {
        "mode": "artifact",
        "artifact": str(path),
        "ok": not gating,
        "scanned_files": scanned,
        "members": members,
        "findings": [f.to_dict() for f in gating],
        "informational_findings": [f.to_dict() for f in informational],
        **summary,
    }


def scan_artifacts(paths: list[Path]) -> dict[str, Any]:
    findings: list[Finding] = []
    informational: list[Finding] = []
    scanned = 0
    artifacts: list[dict[str, Any]] = []
    for path in paths:
        result = scan_artifact(path)
        artifacts.append(
            {
                "artifact": result["artifact"],
                "members": result.get("members", 0),
                "findings": len(result["findings"]),
                "informational_findings": len(result.get("informational_findings", [])),
            }
        )
        scanned += result["scanned_files"]
        findings.extend(Finding(**f) for f in result["findings"])
        informational.extend(Finding(**f) for f in result.get("informational_findings", []))
    summary = _summarize(findings)
    return {
        "mode": "artifacts",
        "ok": not findings,
        "scanned_files": scanned,
        "artifacts": artifacts,
        "findings": [f.to_dict() for f in findings],
        "informational_findings": [f.to_dict() for f in informational],
        **summary,
    }


# --- History scan (report only) -----------------------------------------
def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    return result.stdout


def _tree_blobs(root: Path, commit: str) -> list[str]:
    out = _git(root, "ls-tree", "-r", "-z", commit)
    blobs: list[str] = []
    for entry in out.split("\0"):
        if not entry:
            continue
        meta, _, _name = entry.partition("\t")
        parts = meta.split()
        if len(parts) >= 3 and parts[1] == "blob":
            blobs.append(parts[2])
    return blobs


def scan_history(root: Path = ROOT, *, sample_commits: int | None = None) -> dict[str, Any]:
    """Walk every commit/blob reachable from any ref. Report only, never values."""
    root = root.resolve()
    denylist = _denylist()
    commits = [c for c in _git(root, "rev-list", "--all").split() if c]

    # object -> path hints (informational, masked to paths already in the repo).
    obj_path: dict[str, str] = {}
    for line in _git(root, "rev-list", "--objects", "--all").splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            obj_path[parts[0]] = parts[1]

    # Which blobs each commit's tree contains.
    commit_blobs: dict[str, list[str]] = {}
    all_blobs: set[str] = set()
    for commit in commits:
        blobs = _tree_blobs(root, commit)
        commit_blobs[commit] = blobs
        all_blobs.update(blobs)

    # Read every distinct blob once, in a single cat-file batch.
    blob_bytes: dict[str, bytes] = {}
    blob_list = sorted(all_blobs)
    if blob_list:
        proc = subprocess.Popen(
            ["git", "cat-file", "--batch"],
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        data, _ = proc.communicate(("\n".join(blob_list) + "\n").encode())
        index = 0
        while index < len(data):
            newline = data.index(b"\n", index)
            header = data[index:newline].decode("utf-8", "ignore").split()
            if len(header) < 3:
                break
            sha, _kind, size = header[0], header[1], int(header[2])
            body = data[newline + 1 : newline + 1 + size]
            index = newline + 1 + size + 1
            blob_bytes[sha] = body

    # Scan each blob once; remember which categories it triggers. Binary blobs
    # are reduced to printable strings first, exactly like the tree/artifact scans.
    blob_categories: dict[str, set[str]] = {}
    for sha, raw in blob_bytes.items():
        binary = _is_binary(raw)
        text = "\n".join(_printable_runs(raw)) if binary else raw.decode("utf-8", "ignore")
        kinds = {f.kind for f in _scan_text(text, obj_path.get(sha, sha), denylist=denylist, binary=binary)}
        if kinds:
            blob_categories[sha] = kinds

    category_commits: dict[str, list[str]] = {}
    category_paths: dict[str, set[str]] = {}
    for commit, blobs in commit_blobs.items():
        for sha in blobs:
            kinds = blob_categories.get(sha)
            if not kinds:
                continue
            for kind in kinds:
                category_commits.setdefault(kind, []).append(commit)
                hint = obj_path.get(sha)
                if hint:
                    category_paths.setdefault(kind, set()).add(hint)

    category_commit_counts = {kind: len(set(v)) for kind, v in category_commits.items()}
    result: dict[str, Any] = {
        "mode": "history",
        "ok": not category_commits,
        "commits_scanned": len(commits),
        "blobs_scanned": len(blob_bytes),
        "categories": sorted(category_commit_counts),
        "counts_by_category_commits": category_commit_counts,
        "category_commits": {kind: sorted(set(v)) for kind, v in category_commits.items()},
        "category_paths": {kind: sorted(v) for kind, v in category_paths.items()},
        "raw_values_recorded": False,
    }
    if sample_commits is not None:
        result["category_commits"] = {
            kind: shas[:sample_commits] for kind, shas in result["category_commits"].items()
        }
    return result


# --- CLI -----------------------------------------------------------------
def _print_summary(result: dict[str, Any]) -> None:
    mode = result.get("mode", "tree")
    if mode == "history":
        print(
            f"History scan: {result['commits_scanned']} commits, "
            f"{result['blobs_scanned']} blobs, categories={result['categories']}"
        )
        for kind, count in sorted(result["counts_by_category_commits"].items()):
            paths = result.get("category_paths", {}).get(kind, [])
            print(f"  {kind}: {count} commits; example paths={paths[:5]}")
            print(f"    commits: {result['category_commits'][kind]}")
        return
    print(f"Scanned {result.get('scanned_files', 0)} files in {mode} mode.")
    for finding in result["findings"]:
        print(f"{finding['path']}: {finding['kind']} - {finding['detail']}")
    print(f"Counts by category: {result.get('counts_by_category', {})}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan files, artifacts and history for personal data and secrets")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    parser.add_argument("--report", type=Path, help="Write the JSON report to this path")
    parser.add_argument("--artifact", action="append", default=[], help="Artifact path to scan (repeatable)")
    parser.add_argument("--artifacts", action="append", default=[], help="Directory tree of artifacts to scan")
    parser.add_argument("--history", action="store_true", help="Scan the full git history (report only)")
    parser.add_argument("--sample-commits", type=int, help="Limit commit SHAs per category in history output")
    parser.add_argument("--no-fail", action="store_true", help="Always exit 0 (report only)")
    parser.add_argument("--fail-on-history", action="store_true", help="Exit non-zero when history has findings")
    args = parser.parse_args(argv)

    if args.history:
        result = scan_history(ROOT, sample_commits=args.sample_commits)
        fail = (not result["ok"]) and args.fail_on_history
    elif args.artifact or args.artifacts:
        artifact_paths: list[Path] = []
        for item in args.artifact + args.artifacts:
            p = Path(item)
            if p.is_dir():
                # accept a dist dir: scan each artifact inside it.
                children = [c for c in sorted(p.iterdir()) if c.is_file() and c.suffix.lower() in {
                    ".dmg", ".zip", ".whl", ".tar", ".gz", ".tgz", ".app",
                }]
                if children:
                    artifact_paths.extend(children)
                else:
                    artifact_paths.append(p)
            else:
                artifact_paths.append(p)
        result = scan_artifacts(artifact_paths)
        fail = not result["ok"]
    else:
        result = scan_public_safety(ROOT)
        fail = not result["ok"]

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        _print_summary(result)

    if args.no_fail:
        return 0
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
