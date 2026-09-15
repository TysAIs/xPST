#!/usr/bin/env python3
"""Generate the static manifest consumed by the Tauri updater.

The updater signs each platform artifact, not ``latest.json`` itself.  This
script copies the complete ``.sig`` payload into the manifest and records a
SHA-512 digest of the exact artifact bytes.  It never creates or substitutes a
signature.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

PLATFORM_KEYS = (
    "darwin-aarch64",
    "darwin-x86_64",
    "windows-x86_64",
    "linux-x86_64",
)
_VERSION_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


def load_updater_pubkey(config_path: Path) -> str:
    """Load and validate the release public key from a Tauri config."""
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        pubkey = config["plugins"]["updater"]["pubkey"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read updater pubkey from {config_path}: {exc}") from exc
    if not isinstance(pubkey, str) or not pubkey.strip():
        raise ValueError(f"updater pubkey is empty in {config_path}")
    return pubkey.strip()


def _signature_path(artifact: Path) -> Path:
    """Return the Tauri signature sidecar path for an artifact."""
    return artifact.with_name(artifact.name + ".sig")


def _read_signature(platform: str, artifact: Path, signatures: Mapping[str, Path]) -> str:
    signature_path = signatures.get(platform, _signature_path(artifact))
    if not signature_path.is_file():
        raise FileNotFoundError(
            f"missing updater signature for {platform}: expected {signature_path} "
            f"next to artifact {artifact}"
        )
    signature = signature_path.read_text(encoding="utf-8").strip()
    if not signature:
        raise ValueError(f"empty updater signature for {platform}: {signature_path}")
    return signature


def _sha512(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_artifacts(artifacts: Mapping[str, Path], platforms: Sequence[str]) -> None:
    missing = [platform for platform in platforms if platform not in artifacts]
    if missing:
        raise ValueError(f"missing updater artifacts for platform(s): {', '.join(missing)}")
    extra = sorted(set(artifacts) - set(PLATFORM_KEYS))
    if extra:
        raise ValueError(f"unsupported updater platform(s): {', '.join(extra)}")
    for platform in platforms:
        artifact = Path(artifacts[platform])
        if not artifact.is_file():
            raise FileNotFoundError(f"missing updater artifact for {platform}: {artifact}")


def generate_manifest(
    *,
    version: str,
    notes: str,
    artifacts: Mapping[str, Path],
    base_url: str,
    pubkey: str,
    signatures: Mapping[str, Path] | None = None,
    pub_date: str | None = None,
    platforms: Sequence[str] | None = None,
) -> dict[str, object]:
    """Build a deterministic Tauri static update manifest.

    ``pubkey`` is intentionally an input even though Tauri keeps it in the
    application config rather than in ``latest.json``.  The CLI obtains it
    from that config and refuses to publish if the configured trust root is
    absent.  Verification of each artifact signature is then performed by the
    updater using the same key at install time.
    """
    if not _VERSION_RE.fullmatch(version):
        raise ValueError(f"invalid SemVer version: {version!r}")
    if not isinstance(notes, str):
        raise TypeError("notes must be text")
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("base URL must not be empty")
    if not isinstance(pubkey, str) or not pubkey.strip():
        raise ValueError("updater pubkey must not be empty")

    selected_platforms = tuple(platforms or PLATFORM_KEYS)
    unknown_platforms = sorted(set(selected_platforms) - set(PLATFORM_KEYS))
    if unknown_platforms:
        raise ValueError(f"unsupported updater platform(s): {', '.join(unknown_platforms)}")
    if len(selected_platforms) != len(set(selected_platforms)):
        raise ValueError("duplicate updater platform")
    _validate_artifacts(artifacts, selected_platforms)
    signature_paths = signatures or {}
    manifest_platforms: dict[str, dict[str, str]] = {}
    for platform in selected_platforms:
        artifact = Path(artifacts[platform])
        manifest_platforms[platform] = {
            "signature": _read_signature(platform, artifact, signature_paths),
            "sha512": _sha512(artifact),
            "url": f"{base_url.rstrip('/')}/{quote(artifact.name)}",
        }

    manifest: dict[str, object] = {
        "version": version,
        "notes": notes,
        "platforms": manifest_platforms,
    }
    if pub_date is not None:
        if not pub_date.strip():
            raise ValueError("pub_date must not be empty when provided")
        manifest["pub_date"] = pub_date
    return manifest


def _platform_path(value: str, option: str) -> tuple[str, Path]:
    platform, separator, raw_path = value.partition("=")
    if not separator or platform not in PLATFORM_KEYS or not raw_path:
        expected = " or ".join(f"{platform}=PATH" for platform in PLATFORM_KEYS)
        raise ValueError(f"{option} must be PLATFORM=PATH ({expected})")
    return platform, Path(raw_path)


def _unique_specs(values: Sequence[str], option: str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        platform, path = _platform_path(value, option)
        if platform in result:
            raise ValueError(f"duplicate {option} for platform {platform}")
        result[platform] = path
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="release SemVer (for example 1.2.3)")
    notes = parser.add_mutually_exclusive_group(required=True)
    notes.add_argument("--notes", help="release notes text")
    notes.add_argument("--notes-file", type=Path, help="UTF-8 release notes file")
    parser.add_argument("--base-url", required=True, help="base URL containing the uploaded artifacts")
    parser.add_argument("--output", type=Path, required=True, help="path to write latest.json")
    parser.add_argument(
        "--platform",
        action="append",
        choices=PLATFORM_KEYS,
        help="limit output to a platform (local E2E only; release manifests use all four by default)",
    )
    parser.add_argument(
        "--artifact",
        action="append",
        required=True,
        metavar="PLATFORM=PATH",
        help="one built updater artifact; repeat once for each supported platform",
    )
    parser.add_argument(
        "--signature",
        action="append",
        default=[],
        metavar="PLATFORM=PATH",
        help="optional signature sidecar override; defaults to PATH.sig",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("src-tauri/tauri.conf.json"),
        help="Tauri config containing the updater public key",
    )
    parser.add_argument("--pub-date", help="optional RFC 3339 publication timestamp")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        artifacts = _unique_specs(args.artifact, "--artifact")
        signatures = _unique_specs(args.signature, "--signature")
        notes = args.notes_file.read_text(encoding="utf-8").rstrip() if args.notes_file is not None else args.notes
        manifest = generate_manifest(
            version=args.version,
            notes=notes,
            artifacts=artifacts,
            base_url=args.base_url,
            pubkey=load_updater_pubkey(args.config),
            signatures=signatures,
            pub_date=args.pub_date,
            platforms=args.platform,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    except (OSError, TypeError, ValueError) as exc:
        print(f"gen-updater-manifest: error: {exc}", file=sys.stderr)
        return 2

    platform_count = len(args.platform or PLATFORM_KEYS)
    print(f"wrote {args.output} for {args.version} ({platform_count} platform(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
