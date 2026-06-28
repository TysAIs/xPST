"""Scan publishable repository files for high-confidence secret leaks."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MAX_SCAN_BYTES = 2_000_000
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
SECRET_PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{36,}\b")),
    ("openai_token", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b")),
]


@dataclass
class Finding:
    path: str
    kind: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "kind": self.kind, "detail": self.detail}


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


def _is_binary(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:4096]
    except OSError:
        return True
    return b"\x00" in chunk


def scan_public_safety(root: Path = ROOT, paths: list[Path] | None = None) -> dict[str, Any]:
    """Return publishable-file secret scan results."""
    root = root.resolve()
    files = paths if paths is not None else _git_publishable_files(root)
    findings: list[Finding] = []
    scanned = 0

    for path in files:
        path = path.resolve()
        if not path.exists() or not path.is_file():
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = str(path)

        if any(pattern.match(rel) for pattern in SENSITIVE_FILE_PATTERNS):
            findings.append(Finding(rel, "sensitive_file", "Sensitive runtime/credential-looking file would be published."))
            continue

        if path.stat().st_size > MAX_SCAN_BYTES or _is_binary(path):
            continue

        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        scanned += 1
        for kind, pattern in SECRET_PATTERNS:
            match = pattern.search(text)
            if match:
                findings.append(Finding(rel, kind, f"Matched high-confidence pattern near offset {match.start()}."))

    return {
        "ok": not findings,
        "scanned_files": scanned,
        "findings": [finding.to_dict() for finding in findings],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan publishable files for high-confidence secret leaks")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    result = scan_public_safety()
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Scanned {result['scanned_files']} publishable text files.")
        for finding in result["findings"]:
            print(f"{finding['path']}: {finding['kind']} - {finding['detail']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
