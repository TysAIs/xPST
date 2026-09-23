#!/usr/bin/env python3
"""Verify or re-pin scripts/media-binaries.lock against the upstream releases.

The lock file is what makes a release lane reproducible: every media binary the
bundle ships (today that is yt-dlp only - ffmpeg/ffprobe are resolved at runtime
and pinned in ``src/xpst/media/binaries.py``) is named by an immutable url plus
the SHA-256 of the exact file that url serves, and
scripts/fetch-media-binaries.sh refuses to bundle anything else. This tool is the
supported way to keep that file honest.

    # CI / pre-release review: does every pinned url still serve the pinned bytes?
    scripts/update-media-binaries-lock.py --check

    # Re-pin after deliberately moving to a newer upstream release
    # (edit the urls in the lock first, then re-record the digests):
    scripts/update-media-binaries-lock.py --write

Exit codes: 0 = every entry matches upstream, 1 = drift or a missing asset,
2 = the lock file could not be read/parsed.

For GitHub-hosted assets the authoritative digest comes from the release API
(the same bytes GitHub serves); yt-dlp rows are additionally cross-checked
against the release's own SHA2-256SUMS file, so two independent sources have to
agree before a hash is recorded.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOCK = REPO_ROOT / "scripts" / "media-binaries.lock"
GITHUB_ASSET_PREFIX = "https://github.com/"


@dataclass
class LockRow:
    platform: str
    artifact: str
    kind: str
    member: str
    sha256: str
    url: str
    line_no: int

    @property
    def repo(self) -> str:
        """owner/name of the release this row downloads from."""
        parts = self.url.split("/")
        return f"{parts[3]}/{parts[4]}"

    @property
    def tag(self) -> str:
        parts = self.url.split("/")
        if "download" not in parts:
            raise ValueError(f"not a release download url: {self.url}")
        return parts[parts.index("download") + 1]

    @property
    def asset_name(self) -> str:
        return self.url.rsplit("/", 1)[-1]

    def as_row(self) -> str:
        return "\t".join([self.platform, self.artifact, self.kind, self.member, self.sha256, self.url])


def parse_lock(text: str) -> tuple[list[str], list[LockRow]]:
    """Return (original lines, parsed rows)."""
    lines = text.splitlines()
    rows: list[LockRow] = []
    for index, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = raw.split("\t")
        if len(fields) != 6:
            raise ValueError(f"{DEFAULT_LOCK.name}:{index}: expected 6 tab-separated fields, got {len(fields)}")
        rows.append(LockRow(*fields, line_no=index))
    return lines, rows


def _fetch(url: str, token: str | None = None, as_json: bool = False):
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "xpst-media-binaries-lock")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310 - fixed https urls
        body = response.read()
    return json.loads(body) if as_json else body.decode("utf-8", "replace")


def upstream_digests(repo: str, tag: str, token: str | None = None) -> dict[str, str]:
    """asset name -> 'sha256:<hex>' as reported by the release API."""
    payload = _fetch(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", token, as_json=True)
    digests: dict[str, str] = {}
    for asset in payload.get("assets", []):
        if asset.get("digest"):
            digests[asset["name"]] = asset["digest"]
    return digests


def ytdlp_sums(repo: str, tag: str, token: str | None = None) -> dict[str, str]:
    """asset name -> sha256 from the release's SHA2-256SUMS asset."""
    text = _fetch(f"https://github.com/{repo}/releases/download/{tag}/SHA2-256SUMS", token)
    sums: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and len(parts[0]) == 64:
            sums[parts[1].lstrip("*")] = parts[0].lower()
    return sums


def check_rows(rows: list[LockRow], token: str | None = None) -> tuple[list[str], list[LockRow]]:
    """Return (problems, rows whose recorded hash differs upstream)."""
    problems: list[str] = []
    drifted: list[LockRow] = []
    cache: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        if not row.url.startswith(GITHUB_ASSET_PREFIX):
            problems.append(f"{row.artifact}: not a GitHub-hosted asset: {row.url}")
            continue
        key = (row.repo, row.tag)
        if key not in cache:
            try:
                cache[key] = upstream_digests(row.repo, row.tag, token)
            except urllib.error.HTTPError as exc:  # pragma: no cover - network path
                problems.append(f"{row.repo}@{row.tag}: release lookup failed ({exc.code})")
                cache[key] = {}
            except urllib.error.URLError as exc:  # pragma: no cover - network path
                problems.append(f"{row.repo}@{row.tag}: release lookup failed ({exc.reason})")
                cache[key] = {}
        digest = cache[key].get(row.asset_name)
        if digest is None:
            problems.append(f"{row.artifact}: {row.asset_name} is not an asset of {row.repo}@{row.tag}")
            continue
        expected = digest.split(":", 1)[-1].lower()
        if expected != row.sha256.lower():
            drifted.append(row)
            problems.append(
                f"{row.artifact}: recorded sha256 {row.sha256} != upstream {expected} ({row.url})"
            )
    return problems, drifted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="verify recorded hashes against upstream")
    mode.add_argument("--write", action="store_true", help="re-record hashes from upstream")
    parser.add_argument("--token", default=None, help="GitHub token (defaults to $GITHUB_TOKEN)")
    args = parser.parse_args(argv)

    token = args.token or os.environ.get("GITHUB_TOKEN")
    try:
        lines, rows = parse_lock(args.lock.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    problems, drifted = check_rows(rows, token)
    if problems:
        for problem in problems:
            print(f"DRIFT: {problem}", file=sys.stderr)
        if not args.write:
            print(
                f"\n{len(problems)} problem(s). If a release legitimately moved, re-pin the urls in\n"
                f"{args.lock} and run --write.",
                file=sys.stderr,
            )
            return 1

    if args.write:
        updates = {row.line_no: row for row in drifted}
        current = {}
        for row in rows:
            key = (row.repo, row.tag)
            if key not in current:
                try:
                    current[key] = upstream_digests(row.repo, row.tag, token)
                except urllib.error.URLError as exc:  # pragma: no cover - network path
                    print(f"error: cannot re-pin {row.repo}@{row.tag}: {exc}", file=sys.stderr)
                    return 1
        for row in rows:
            digest = current[(row.repo, row.tag)].get(row.asset_name)
            if digest is None:
                print(f"error: {row.asset_name} missing from {row.repo}@{row.tag}", file=sys.stderr)
                return 1
            row.sha256 = digest.split(":", 1)[-1].lower()
        for index in range(1, len(lines) + 1):
            if index in updates:
                lines[index - 1] = updates[index].as_row()
        args.lock.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"re-pinned {len(rows)} entr{'y' if len(rows) == 1 else 'ies'} in {args.lock}")
        if drifted:
            print(f"({len(drifted)} hash(es) changed)")

    # yt-dlp rows are cross-checked against the release's own SHA2-256SUMS asset.
    ytdlp_rows = [row for row in rows if row.artifact == "yt-dlp"]
    if ytdlp_rows:
        key = (ytdlp_rows[0].repo, ytdlp_rows[0].tag)
        try:
            sums = ytdlp_sums(*key, token)
        except (urllib.error.URLError, ValueError) as exc:  # pragma: no cover - network path
            print(f"error: cannot fetch SHA2-256SUMS for {key[0]}@{key[1]}: {exc}", file=sys.stderr)
            return 1
        for row in ytdlp_rows:
            published = sums.get(row.asset_name)
            if published is None:
                print(f"error: {row.asset_name} not listed in SHA2-256SUMS", file=sys.stderr)
                return 1
            if published != row.sha256.lower():
                print(f"error: {row.artifact} {row.asset_name}: lock {row.sha256} != SHA2-256SUMS {published}", file=sys.stderr)
                return 1
        print(f"cross-checked {len(ytdlp_rows)} yt-dlp entr{'y' if len(ytdlp_rows) == 1 else 'ies'} against SHA2-256SUMS")

    print(f"OK: {len(rows)} pinned entries verified ({args.lock})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
