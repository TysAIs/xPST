"""Local high-signal secret scanner (offline fallback for gitleaks).

``.github/workflows/security-scan.yml`` prefers gitleaks (checksum-verified
release binary). This script is the deterministic, network-free fallback — it
runs anywhere, including a laptop with no internet, and covers the token shapes
xPST actually handles (Meta/Instagram, Telegram, Discord webhooks, X, Google,
GitHub, OpenAI, Slack, Stripe, JWTs, private keys).

Design rules:
  * high signal only — no generic "long hex string" or entropy rule, because
    the repo legitimately contains checksums, minisign key ids and SHA256SUMS;
  * a match is skipped when the surrounding text is obviously synthetic
    (``SYNTHETIC``, ``do_not_use``, ``example``, ``redacted``, ``placeholder``,
    ``${{ secrets.``), so the scanner does not fail on its own test fixtures or
    on a GitHub Actions secret reference;
  * exits non-zero on any finding when ``--fail-on-findings`` is given.

Unlike ``scripts/scan_public_safety.py`` (which scopes to *publishable* files),
this scans every git-tracked file.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_SCAN_BYTES = 2_000_000

# Binary-ish paths that never contain reviewable secrets and slow the scan down.
SKIP_DIR_PREFIXES = (
    ".git/",
    "node_modules/",
    "ui/node_modules/",
    "dist/",
    "build/",
    "src-tauri/target/",
)
SKIP_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".icns", ".pdf",
    ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".mov", ".zip", ".gz",
    ".tar", ".dmg", ".exe", ".so", ".dylib", ".dll", ".pyc", ".db", ".bin",
)

# Text that marks a match as deliberately fake / a reference rather than a leak.
ALLOWLIST_MARKERS = (
    "SYNTHETIC",
    "DO_NOT_USE",
    "do_not_use",
    "notreal",
    "not_real",
    "not-a-real",
    "example.com",
    "example.test",
    "your_token",
    "YOUR_TOKEN",
    "your_",
    "YOUR_",
    "changeme",
    "CHANGEME",
    "replace-me",
    "REPLACE_ME",
    "placeholder",
    "PLACEHOLDER",
    "redacted",
    "REDACTED",
    "xxxx",
    "XXXX",
    "${{ secrets.",
    "${{secret.",
    "os.environ",
    "getenv",
)

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("aws_secret_access_key", re.compile(r"(?i)(?:aws)?_?secret_?access_?key[\"'\s:=]{1,6}([A-Za-z0-9/+=]{40})")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{36,}\b")),
    ("github_fine_grained_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("openai_token", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("anthropic_token", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("stripe_secret_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b")),
    ("discord_webhook", re.compile(r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d{1,20}/[A-Za-z0-9_-]{50,}")),
    ("telegram_bot_token", re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("meta_app_secret_assignment",
     re.compile(r"(?i)\b(?:app_?secret|client_secret|consumer_secret|page_?access_?token|access_?token)\b\s*[:=]\s*[\"']([A-Za-z0-9_-]{24,})[\"']")),
    ("password_assignment", re.compile(r"(?i)\bpassword\b\s*[:=]\s*[\"']([^\"'\s]{8,})[\"']")),
]


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    excerpt: str

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "line": self.line, "kind": self.kind, "excerpt": self.excerpt}


def _tracked_files(root: Path) -> list[Path]:
    # ``--others --exclude-standard`` also covers new, not-yet-committed files:
    # without it a pre-commit run silently skips exactly the file being added,
    # which is how a fixture leak slips into a commit.
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:  # pragma: no cover - not a git checkout
        return [p for p in root.rglob("*") if p.is_file()]
    return [root / line.strip() for line in result.stdout.splitlines() if line.strip()]


def _is_scannable(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - defensive
        return False
    if any(rel.startswith(prefix) for prefix in SKIP_DIR_PREFIXES):
        return False
    if rel.endswith(SKIP_SUFFIXES):
        return False
    try:
        return path.is_file() and path.stat().st_size <= MAX_SCAN_BYTES
    except OSError:  # pragma: no cover - defensive
        return False


def _excerpt(line: str, start: int, end: int) -> str:
    """Return a short window around the match with the value itself masked.

    The excerpt is for a human reading CI output, so the secret is replaced —
    the scanner must never print a live credential into a public build log.
    """
    lo = max(0, start - 30)
    hi = min(len(line), end + 10)
    masked = line[lo:start] + "<matched>" + line[end:hi]
    return masked.strip()[:160]


def _is_allowed(window: str) -> bool:
    """Whether a match window is obviously synthetic / a reference.

    Compared case-insensitively: a fixture written ``synthetic-password`` must be
    as clearly marked as ``SYNTHETIC_TOKEN``.
    """
    lowered = window.lower()
    return any(marker.lower() in lowered for marker in ALLOWLIST_MARKERS)


def scan_text(text: str, rel_path: str) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        for kind, pattern in PATTERNS:
            for match in pattern.finditer(line):
                window = line[max(0, match.start() - 60): match.end() + 60]
                if _is_allowed(window):
                    continue
                findings.append(
                    Finding(
                        path=rel_path,
                        line=lineno,
                        kind=kind,
                        excerpt=_excerpt(line, match.start(), match.end()),
                    )
                )
    return findings


def scan(root: Path = ROOT) -> list[Finding]:
    findings: list[Finding] = []
    for path in _tracked_files(root):
        if not _is_scannable(path, root):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:  # pragma: no cover - defensive
            continue
        findings.extend(scan_text(text, path.relative_to(root).as_posix()))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local high-signal secret scanner")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--fail-on-findings",
        action="store_true",
        help="exit 1 when any finding is present (CI mode)",
    )
    parser.add_argument("--root", default=str(ROOT))
    args = parser.parse_args(argv)

    findings = scan(Path(args.root).resolve())
    if args.json:
        print(json.dumps([f.to_dict() for f in findings], indent=2, sort_keys=True))
    else:
        if not findings:
            print("No secrets found.")
        for finding in findings:
            print(f"{finding.path}:{finding.line}: {finding.kind}: {finding.excerpt}")

    if args.fail_on_findings and findings:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
