"""The updater manifest must never take its version from the tag's spelling.

dryrun-tauri-4 (run 43601846557) failed ``publish-updater-manifest`` after every
build and the release upload had succeeded: the step passed
``--version "${GITHUB_REF_NAME#v}"``, so the generator was handed
``dryrun-tauri-4`` and correctly refused it as invalid SemVer. These tests hold
both halves of the fix: the resolver's rules, and the workflow actually using
the resolver instead of the ref name.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.release_version_for_ref import (
    release_version,
    resolve_manifest_version,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "tauri-release.yml"
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


def load_workflow() -> dict[str, Any]:
    text = WORKFLOW.read_text(encoding="utf-8")
    # Quote the trigger key so YAML's boolean coercion cannot turn `on` into True.
    text = re.sub(r"^on:", "'on':", text, count=1, flags=re.MULTILINE)
    return yaml.safe_load(text)


def manifest_step() -> dict[str, Any]:
    steps = load_workflow()["jobs"]["publish-updater-manifest"]["steps"]
    for step in steps:
        if "gen-updater-manifest.py" in str(step.get("run", "")):
            return step
    raise AssertionError("publish-updater-manifest has no step invoking gen-updater-manifest.py")


def test_tree_declares_one_release_version() -> None:
    version = release_version()
    assert SEMVER_RE.fullmatch(version), version


def test_dry_run_tag_uses_the_tree_version_not_the_tag() -> None:
    """The exact ref that broke run 43601846557."""
    version = resolve_manifest_version("dryrun-tauri-4")
    assert version == release_version()
    assert version != "dryrun-tauri-4"


def test_dry_run_tag_is_reported_for_every_spelling() -> None:
    assert resolve_manifest_version("dryrun-tauri-7") == release_version()
    assert resolve_manifest_version("refs/tags/dryrun-tauri-7") == release_version()


def test_matching_release_tag_is_accepted() -> None:
    assert resolve_manifest_version(f"v{release_version()}") == release_version()


def test_mis_tagged_release_fails_instead_of_advertising_a_wrong_version() -> None:
    with pytest.raises(ValueError, match="does not match the release version"):
        resolve_manifest_version("v9.9.9")


def test_non_semver_release_tag_is_rejected() -> None:
    with pytest.raises(ValueError, match="not a SemVer release tag|neither a version tag"):
        resolve_manifest_version("vnext")


def test_undefined_ref_shape_is_rejected() -> None:
    """A tag that is neither a release version nor a documented dry run."""
    with pytest.raises(ValueError, match="neither a version tag"):
        resolve_manifest_version("some-feature-branch")


def test_empty_ref_is_rejected() -> None:
    with pytest.raises(ValueError, match="no release ref"):
        resolve_manifest_version("  ")


def test_workflow_never_feeds_the_ref_name_to_the_generator() -> None:
    run = str(manifest_step()["run"])
    assert "${GITHUB_REF_NAME#v}" not in run
    assert "--version" in run
    version_line = next(line for line in run.splitlines() if "--version" in line)
    assert "steps.version.outputs.manifest_version" in version_line


def test_workflow_resolves_the_version_before_generating() -> None:
    steps = load_workflow()["jobs"]["publish-updater-manifest"]["steps"]
    names = [str(step.get("name", "")) for step in steps]
    resolver = next(index for index, name in enumerate(names) if name.startswith("Resolve the manifest version"))
    generator = next(index for index, name in enumerate(names) if "gen-updater-manifest.py" in str(steps[index].get("run", "")))
    assert resolver < generator
    run = str(steps[resolver]["run"])
    assert "scripts/release_version_for_ref.py" in run
    assert "manifest_version=" in run


def test_updater_job_still_attaches_the_manifest_to_the_tagged_release() -> None:
    steps = load_workflow()["jobs"]["publish-updater-manifest"]["steps"]
    attaches = [step for step in steps if "gh release upload" in str(step.get("run", ""))]
    assert len(attaches) == 1
    assert "latest.json" in str(attaches[0]["run"])


def test_updater_job_waits_for_the_release_it_attaches_to() -> None:
    """dryrun-tauri-8 (run 44465028672): both jobs started together, and this
    job's upload ran three seconds before publish-release created the release,
    so a four-of-four validated manifest was discarded with "release not found".
    The dependency — not a retry — is what makes the ordering correct."""
    job = load_workflow()["jobs"]["publish-updater-manifest"]
    needs = job.get("needs")
    needs_list = [needs] if isinstance(needs, str) else list(needs or [])
    assert "publish-release" in needs_list, needs_list
    attach = next(step for step in job["steps"] if "gh release upload" in str(step.get("run", "")))
    assert "gh release view" in str(attach["run"]), "the attach step does not wait for the release"
