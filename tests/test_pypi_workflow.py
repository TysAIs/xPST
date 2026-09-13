"""Repository checks for the PyPI Trusted Publishing path."""

from __future__ import annotations

from pathlib import Path

import tomllib
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-pypi.yml"


def _load_workflow() -> tuple[dict, dict]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML's YAML 1.1 resolver treats the GitHub Actions key "on" as True.
    triggers = data.get("on", data.get(True))
    assert isinstance(triggers, dict)
    return data, triggers


def test_pypi_workflow_uses_oidc_and_only_publishes_release_or_tag():
    workflow, triggers = _load_workflow()

    assert set(triggers) == {"release", "push", "workflow_dispatch"}
    assert triggers["release"] == {"types": ["published"]}
    assert triggers["push"] == {"tags": ["v*.*.*"]}
    assert workflow["permissions"]["id-token"] == "write"

    publish = workflow["jobs"]["publish"]
    assert publish["environment"] == "pypi"
    assert publish["permissions"]["id-token"] == "write"
    assert "github.event_name == 'release'" in publish["if"]
    assert "github.event_name == 'push'" in publish["if"]
    assert "refs/tags/v" in publish["if"]
    assert "workflow_dispatch" not in publish["if"]

    publish_actions = [step.get("uses", "") for step in publish["steps"]]
    assert "pypa/gh-action-pypi-publish@release/v1" in publish_actions
    assert all("password:" not in str(step) for step in publish["steps"])


def test_pypi_metadata_is_public_safe_and_has_required_license_classifiers():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert metadata["description"]
    assert metadata["license"] == "MIT OR Apache-2.0"
    assert {
        "License :: OSI Approved :: MIT License",
        "License :: OSI Approved :: Apache Software License",
    } <= set(metadata["classifiers"])
    assert all("email" not in author for author in metadata["authors"])
    assert all("email" not in maintainer for maintainer in metadata.get("maintainers", []))
