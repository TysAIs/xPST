#!/usr/bin/env python3
"""Guard the public updater channel before `updates/latest.json` is replaced.

`updates/latest.json` on `main` is the document every installed xPST client
polls for updates, so replacing it is a promise about code that will be
installed automatically on other people's machines. Three properties must hold
before this repository may replace it, and none of them were checked before
this script existed:

1. **Version provenance.** The advertised version is the version the released
   artifacts were built from - the one in ``src-tauri/tauri.conf.json`` at the
   tag's own tree - never the tag's spelling. A ``vX.Y.Z`` tag whose tree
   declares a different version is a mis-tagged release: publishing it would
   advertise a version whose artifacts do not exist, and every client would be
   offered an "update" that reinstalls the bits it already has.
2. **Monotonicity.** The channel never moves backwards. The updater only offers
   a version it considers newer, so replacing the manifest with an older one
   stops offering updates to everyone, silently. A candidate lower than the
   version currently served on ``main`` is refused unless the operator
   explicitly asks for a rollback.
3. **Integrity.** Every platform the manifest advertises must carry a signature
   that verifies against the updater public key compiled into the released app.
   Tauri signs through rsign2: Ed25519 over Blake2b-512(payload). Verifying the
   Ed25519 signature over the *raw* bytes fails, which is why the prehash is
   applied explicitly here instead of being assumed.

Subcommands
-----------
``version``   resolve the version this release must advertise (offline: reads
              the tag out of the local clone).
``verify``    the full gate: version provenance, a cross-check against the
              release's own asset names, monotonicity, and a valid signature on
              every payload the manifest advertises.

Exit codes: 0 published-able, 2 refused (nothing is written).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.release_version_for_ref import (  # noqa: E402
    normalize_ref,
    resolve_manifest_version,
)
from scripts.verify_release_version import release_versions  # noqa: E402

# Every file `verify_release_version.release_versions` reads. A tag's tree is
# materialised from exactly these paths, so a release surface added there must
# be added here too - `tests/test_updater_channel_guard.py` fails if the two
# drift apart.
RELEASE_SURFACES = (
    "pyproject.toml",
    "Cargo.toml",
    "src/xpst/__init__.py",
    "src-tauri/Cargo.toml",
    "src-tauri/tauri.conf.json",
    "ui/package.json",
    "ui/package-lock.json",
)
# The surface that names the built installers and updater payloads.
ARTIFACT_SURFACE = "tauri"

RELEASE_TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)$")
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")

# The packager's installer names carry the version that produced them, so they
# are an independent check on the tag's tree: `src-tauri/tauri.conf.json` ->
# `xPST_<version>_<arch>.<ext>`. Names that do not follow the convention are
# ignored rather than guessed at.
VERSIONED_ASSET_RE = re.compile(r"^xPST_(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)_")

SUPPORTED_PLATFORMS = ("darwin-aarch64", "darwin-x86_64", "windows-x86_64", "linux-x86_64")


class ChannelError(ValueError):
    """The channel must not be written. The message says why, for the log."""


def _git(repo: Path, *args: str) -> str:
    """Run a git command and return its stdout, raising ChannelError on failure."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ChannelError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def materialize_ref_tree(repo: Path, ref: str) -> Path:
    """Write the release-surface files of ``ref`` into a temporary directory.

    The publishing workflow checks out ``main`` because that is the branch
    GitHub Pages serves, so its working copy is the wrong tree by
    construction: the version to advertise belongs to the tag that was built.
    The caller owns the directory and removes it.
    """
    root = Path(tempfile.mkdtemp(prefix="updater-tag-tree-"))
    for relative in RELEASE_SURFACES:
        content = _git(repo, "show", f"{ref}:{relative}")
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def resolve_channel_version(repo: Path, tag: str) -> str:
    """Return the version this release must advertise, or raise ChannelError.

    Refuses a non-release ref outright (the public channel only follows real
    releases), then applies the shared rule from
    ``scripts/release_version_for_ref.py``: the tag and the tag's own tree must
    name the same version.
    """
    ref = normalize_ref(tag)
    match = RELEASE_TAG_RE.fullmatch(ref)
    if match is None:
        raise ChannelError(
            f"{ref!r} is not a vX.Y.Z release tag; the public updater channel only "
            "follows real releases"
        )
    root: Path | None = None
    try:
        root = materialize_ref_tree(repo, ref)
        try:
            version = resolve_manifest_version(ref, root)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise ChannelError(f"{ref}: {exc}") from exc
        if version != match.group(1):
            raise ChannelError(
                f"tag {ref!r} advertises {match.group(1)} but its tree declares {version!r}"
            )
        return version
    finally:
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)


def pubkey_at_ref(repo: Path, ref: str) -> str:
    """Return the updater public key compiled into the release at ``ref``."""
    try:
        config = json.loads(_git(repo, "show", f"{ref}:src-tauri/tauri.conf.json"))
        pubkey = config["plugins"]["updater"]["pubkey"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ChannelError(f"could not read the updater pubkey from {ref}: {exc}") from exc
    if not isinstance(pubkey, str) or not pubkey.strip():
        raise ChannelError(f"the updater pubkey is empty in {ref}'s tauri.conf.json")
    return pubkey.strip()


def parse_public_key(b64_value: str) -> tuple[bytes, bytes]:
    """Return ``(key_id, key_bytes)`` from a Tauri ``plugins.updater.pubkey``."""
    try:
        text = base64.b64decode(b64_value, validate=False).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ChannelError(f"updater pubkey is not base64 text: {exc}") from exc
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2 or not lines[0].startswith("untrusted comment:"):
        raise ChannelError("updater pubkey is not a minisign public key")
    try:
        inner = base64.b64decode(lines[1], validate=False)
    except ValueError as exc:
        raise ChannelError(f"updater pubkey payload is not base64: {exc}") from exc
    if len(inner) != 42:
        raise ChannelError(f"updater pubkey has {len(inner)} bytes; a minisign key has 42")
    return inner[2:10], inner[10:42]


def parse_signature(text: str) -> tuple[bytes, bytes, bytes]:
    """Return ``(algorithm, key_id, signature)`` from a minisign signature.

    Accepts both shapes this repository produces: the four-line text a ``.sig``
    file holds, and the base64-of-that-text that ``latest.json`` carries.
    """
    candidate = text.strip()
    if "\n" not in candidate:
        try:
            candidate = base64.b64decode(candidate, validate=False).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ChannelError(f"signature is neither minisign text nor base64: {exc}") from exc
    lines = [line for line in candidate.splitlines() if line.strip()]
    if len(lines) < 2:
        raise ChannelError("signature has fewer than two lines; not a minisign signature")
    if not lines[0].startswith("untrusted comment:"):
        raise ChannelError("signature does not start with an untrusted comment")
    try:
        inner = base64.b64decode(lines[1], validate=False)
    except ValueError as exc:
        raise ChannelError(f"signature payload is not base64: {exc}") from exc
    if len(inner) != 74:
        raise ChannelError(f"signature has {len(inner)} bytes; a minisign signature has 74")
    algorithm = inner[:2]
    if algorithm not in (b"ED", b"Ed"):
        raise ChannelError(f"unknown minisign algorithm {algorithm!r}; expected ED or Ed")
    return algorithm, inner[2:10], inner[10:74]


def verify_payload(artifact: Path, signature_text: str, pubkey: str) -> None:
    """Raise ChannelError unless ``signature_text`` signs ``artifact``.

    ``ED`` is minisign's prehashed form - Ed25519 over Blake2b-512 of the
    payload - which is what Tauri's rsign2 produces. ``Ed`` is the legacy raw
    form and is accepted because it is a real minisign algorithm, but nothing
    this repository ships uses it.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    key_id, key_bytes = parse_public_key(pubkey)
    algorithm, signature_key_id, signature = parse_signature(signature_text)
    if signature_key_id != key_id:
        raise ChannelError(
            f"{artifact.name} is signed by key {signature_key_id.hex()} but the released app "
            f"trusts {key_id.hex()}: no client could install this update"
        )
    message = hashlib.blake2b(artifact.read_bytes(), digest_size=64).digest()
    if algorithm == b"Ed":
        message = artifact.read_bytes()
    try:
        Ed25519PublicKey.from_public_bytes(key_bytes).verify(signature, message)
    except InvalidSignature as exc:
        rule = "Blake2b-512 of the payload" if algorithm == b"ED" else "the raw payload"
        raise ChannelError(
            f"{artifact.name}: Ed25519 signature does not verify over {rule}"
        ) from exc


def semver_key(version: str) -> tuple[int, int, int, int, tuple[tuple[int, int, str], ...]]:
    """Order a version the way the updater does, not the way a string sorts."""
    match = SEMVER_RE.fullmatch(version.strip())
    if match is None:
        raise ChannelError(f"{version!r} is not a SemVer version")
    major, minor, patch = (int(match.group(index)) for index in (1, 2, 3))
    prerelease = match.group(4)
    if prerelease is None:
        return (major, minor, patch, 1, ())
    identifiers: list[tuple[int, int, str]] = []
    for part in prerelease.split("."):
        identifiers.append((0, int(part), "") if part.isdigit() else (1, 0, part))
    return (major, minor, patch, 0, tuple(identifiers))


def check_monotonic(served_version: str | None, candidate: str, *, allow_downgrade: bool = False) -> str:
    """Return a sentence describing the comparison, or raise on a downgrade."""
    if served_version is None:
        return f"no manifest is served yet; {candidate} would be the first"
    served = semver_key(served_version)
    candidate_key = semver_key(candidate)
    if candidate_key >= served:
        relation = "re-publishes" if candidate_key == served else "upgrades from"
        return f"{candidate} {relation} the served {served_version}"
    if allow_downgrade:
        return f"{candidate} is lower than the served {served_version}, allowed by an explicit request"
    raise ChannelError(
        f"{candidate} is lower than the {served_version} already served on main. The updater "
        "would offer nobody an update, silently. Re-run with --allow-downgrade (workflow input "
        "allow_downgrade) if this is a deliberate rollback."
    )


def versioned_asset_names(names: list[str]) -> dict[str, str]:
    """Map each asset name that carries a release version to that version."""
    found: dict[str, str] = {}
    for name in names:
        match = VERSIONED_ASSET_RE.match(name.strip())
        if match is not None:
            found[name] = match.group(1)
    return found


def check_asset_names(names: list[str], version: str) -> str:
    """Raise if any installer name disagrees with the resolved version."""
    versioned = versioned_asset_names(names)
    wrong = {name: found for name, found in sorted(versioned.items()) if found != version}
    if wrong:
        detail = "; ".join(f"{name} -> {found}" for name, found in wrong.items())
        raise ChannelError(
            f"the release's own asset names disagree with version {version}: {detail}. "
            "Those artifacts were built from another tree, so the manifest would advertise "
            "files that do not exist."
        )
    return f"{len(versioned)} versioned asset name(s) agree on {version}"


def read_served_manifest(repo: Path, ref: str) -> dict[str, Any] | None:
    """Return the manifest currently served from ``ref``, or None if there is none."""
    exists = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    if exists.returncode != 0:
        return None
    result = subprocess.run(
        ["git", "-C", str(repo), "show", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ChannelError(f"could not read the served manifest {ref}: {result.stderr.strip()}")
    try:
        manifest = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ChannelError(f"the manifest served from {ref} is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ChannelError(f"the manifest served from {ref} is not an object")
    return manifest


def verify_manifest(manifest: dict[str, Any], assets_dir: Path, pubkey: str, version: str) -> list[str]:
    """Raise unless the manifest is internally consistent and every payload is signed."""
    advertised = manifest.get("version")
    if advertised != version:
        raise ChannelError(
            f"the manifest advertises {advertised!r} but this release is {version!r}"
        )
    entries = manifest.get("platforms")
    if not isinstance(entries, dict) or not entries:
        raise ChannelError("the manifest advertises no platforms")
    unknown = sorted(set(entries) - set(SUPPORTED_PLATFORMS))
    if unknown:
        raise ChannelError(f"the manifest advertises unsupported platform(s): {', '.join(unknown)}")

    verified: list[str] = []
    for platform in sorted(entries):
        entry = entries[platform]
        if not isinstance(entry, dict):
            raise ChannelError(f"{platform} manifest entry is not an object")
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            raise ChannelError(f"{platform} manifest entry has no URL")
        name = unquote(Path(urlparse(url).path).name)
        artifact = assets_dir / name
        if not artifact.is_file():
            raise ChannelError(f"{platform} points at {name}, which is not among the release assets")
        digest = hashlib.sha512(artifact.read_bytes()).hexdigest()
        if digest != entry.get("sha512"):
            raise ChannelError(
                f"{platform} sha512 does not match the bytes of {name} "
                f"(manifest {str(entry.get('sha512'))[:12]}, file {digest[:12]})"
            )
        signature = entry.get("signature")
        if not isinstance(signature, str) or not signature.strip():
            raise ChannelError(f"{platform} manifest entry carries no signature")
        verify_payload(artifact, signature, pubkey)
        verified.append(platform)
    return verified


def _report(lines: list[str]) -> None:
    for line in lines:
        print(line, file=sys.stderr)


def _cmd_version(args: argparse.Namespace) -> int:
    version = resolve_channel_version(Path(args.repo), args.tag)
    print(version)
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    version = resolve_channel_version(repo, args.tag)
    report = [f"tag {args.tag} resolves to version {version} (tree, not tag spelling)"]

    if args.asset_names_file:
        names = [
            line.strip()
            for line in Path(args.asset_names_file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not names:
            raise ChannelError(f"no release asset names in {args.asset_names_file}")
        report.append(check_asset_names(names, version))

    served: dict[str, Any] | None = None
    if args.served_version:
        served_version: str | None = args.served_version
    else:
        served = read_served_manifest(repo, args.served_ref) if args.served_ref else None
        served_version = str(served["version"]) if isinstance(served, dict) and served.get("version") else None
    report.append(check_monotonic(served_version, version, allow_downgrade=args.allow_downgrade))

    if served is not None and served_version is not None and semver_key(version) == semver_key(served_version):
        advertised = {platform: entry.get("url") for platform, entry in (served.get("platforms") or {}).items()}
        report.append(
            f"re-publishing {version} over an existing {served_version} manifest "
            f"({len(advertised)} platform(s) served)"
        )

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    pubkey = args.pubkey or pubkey_at_ref(repo, args.tag)
    verified = verify_manifest(manifest, Path(args.assets_dir), pubkey, version)
    key_id, _ = parse_public_key(pubkey)
    report.append(
        f"verified {len(verified)} signed payload(s) against updater key {key_id.hex()}: "
        f"{', '.join(verified)}"
    )
    _report(report)
    print(version)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    version_parser = subparsers.add_parser("version", help="resolve the version this release advertises")
    version_parser.add_argument("--repo", default=str(Path.cwd()), help="clone the tag can be read from")
    version_parser.add_argument("--tag", required=True, help="release tag, for example v1.2.3")
    version_parser.set_defaults(handler=_cmd_version)

    verify_parser = subparsers.add_parser("verify", help="refuse a publish that would damage the channel")
    verify_parser.add_argument("--repo", default=str(Path.cwd()), help="clone the tag and served manifest are read from")
    verify_parser.add_argument("--tag", required=True, help="release tag, for example v1.2.3")
    verify_parser.add_argument("--manifest", required=True, help="the generated latest.json")
    verify_parser.add_argument("--assets-dir", required=True, help="directory holding the released payloads")
    verify_parser.add_argument(
        "--served-ref",
        default="HEAD:updates/latest.json",
        help="git ref of the manifest currently served (empty to skip the monotonic guard)",
    )
    verify_parser.add_argument("--asset-names-file", help="one release asset name per line, for the name cross-check")
    verify_parser.add_argument(
        "--served-version",
        help="the version currently served, when the caller already read it (skips --served-ref)",
    )
    verify_parser.add_argument("--pubkey", help="updater public key (default: the key in the tag's tree)")
    verify_parser.add_argument(
        "--allow-downgrade",
        action="store_true",
        help="permit a version lower than the one served (a deliberate rollback)",
    )
    verify_parser.set_defaults(handler=_cmd_verify)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except ChannelError as exc:
        print(f"verify-updater-channel: refused: {exc}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(f"verify-updater-channel: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
