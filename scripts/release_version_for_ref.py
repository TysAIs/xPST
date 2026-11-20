#!/usr/bin/env python3
"""Resolve the version the updater manifest must advertise for a release ref.

``latest.json`` describes artifacts that were built from this tree, so its
``version`` is the tree's release version - the one in
``src-tauri/tauri.conf.json`` that names the installers - and never the
spelling of the tag that happened to trigger the run. ``tauri-release.yml``
accepts two ref shapes:

* ``vX.Y.Z`` - a real release. It must agree with the tree; a disagreement is
  a mis-tagged release and fails here instead of advertising a version whose
  artifacts it does not contain.
* ``dryrun-tauri-*`` - the documented dry-run lane, which deliberately carries
  no version of its own. It exists to exercise the exact build, manifest and
  upload path into a DRAFT release, so it needs a real, truthful version: the
  tree's.

This differs on purpose from ``publish-updater.yml``, which reports "not a
SemVer release tag; nothing to publish" and stops: that lane feeds the public
Pages channel, where a non-version ref has no meaning. This lane builds drafts
and must still prove the manifest path end to end.

Historical failure this prevents (dryrun-tauri-4, run 43601846557):
the step passed ``--version "${GITHUB_REF_NAME#v}"``, so the generator was
handed ``dryrun-tauri-4`` and refused it - correctly - as invalid SemVer,
failing a job whose build and upload had already succeeded.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.verify_release_version import release_versions  # noqa: E402

# The version surface that names the built installers and updater payloads.
ARTIFACT_VERSION_SURFACE = "tauri"
RELEASE_TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)$")
DRY_RUN_TAG_RE = re.compile(r"^dryrun-[A-Za-z0-9._-]+$")


def normalize_ref(ref_name: str) -> str:
    """Return a bare tag name from a tag name or a full ``refs/tags/`` ref."""
    ref = ref_name.strip()
    if ref.startswith("refs/tags/"):
        ref = ref[len("refs/tags/") :]
    return ref


def release_version(root: Path = ROOT) -> str:
    """Return the one release version this tree declares.

    Every release-facing surface must agree before a manifest can claim a
    version: a manifest is a promise that all four platforms are the same
    release.
    """
    versions = release_versions(root)
    distinct = sorted(set(versions.values()))
    if len(distinct) != 1:
        details = ", ".join(f"{name}={version}" for name, version in sorted(versions.items()))
        raise ValueError(f"release version surfaces disagree: {details}")
    return versions[ARTIFACT_VERSION_SURFACE]


def resolve_manifest_version(ref_name: str, root: Path = ROOT) -> str:
    """Return the manifest version for ``ref_name``, or raise ``ValueError``."""
    ref = normalize_ref(ref_name)
    if not ref:
        raise ValueError("no release ref given (tag name or refs/tags/<tag> expected)")

    version = release_version(root)

    release_match = RELEASE_TAG_RE.fullmatch(ref)
    if release_match is not None:
        tag_version = release_match.group(1)
        if tag_version != version:
            raise ValueError(
                f"tag {ref!r} does not match the release version {version!r} in "
                f"{ARTIFACT_VERSION_SURFACE} config: a manifest built from this tree "
                "would advertise a version its artifacts do not have"
            )
        return version

    if DRY_RUN_TAG_RE.fullmatch(ref) is None:
        raise ValueError(
            f"ref {ref!r} is neither a version tag (vMAJOR.MINOR.PATCH) nor a "
            "documented dry-run tag (dryrun-*), so it has no defined manifest version"
        )
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve the version the updater manifest must advertise")
    parser.add_argument(
        "--ref",
        default="",
        help="tag name or refs/tags/<tag> that triggered the run (default: $GITHUB_REF_NAME)",
    )
    args = parser.parse_args(argv)
    ref = args.ref or os.environ.get("GITHUB_REF_NAME", "")
    try:
        version = resolve_manifest_version(ref)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"release-version-for-ref: error: {exc}", file=sys.stderr)
        return 2
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
