"""Repository checks for the PyPI publish path (token auth, release/tag gated)."""

from __future__ import annotations

import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-pypi.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
PYPI_DOC = ROOT / "docs" / "PYPI.md"


def _load_workflow() -> tuple[dict, dict]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML's YAML 1.1 resolver treats the GitHub Actions key "on" as True.
    triggers = data.get("on", data.get(True))
    assert isinstance(triggers, dict)
    return data, triggers


def _publish_step(steps: list[dict]) -> dict:
    for step in steps:
        if "pypa/gh-action-pypi-publish" in step.get("uses", ""):
            return step
    raise AssertionError("no pypa/gh-action-pypi-publish step found")


def test_pypi_workflow_publishes_with_token_and_only_on_release_or_tag():
    workflow, triggers = _load_workflow()

    assert set(triggers) == {"release", "push", "workflow_dispatch"}
    assert triggers["release"] == {"types": ["published"]}
    assert triggers["push"] == {"tags": ["v*.*.*"]}
    # Token auth: the run needs no GitHub OIDC identity token.
    assert "id-token" not in workflow["permissions"]

    publish = workflow["jobs"]["publish"]
    # No environment gate: an environment with protection rules would stall the
    # publish job on a human approval click.
    assert "environment" not in publish
    assert "id-token" not in publish["permissions"]
    assert "github.event_name == 'release'" in publish["if"]
    assert "github.event_name == 'push'" in publish["if"]
    assert "refs/tags/v" in publish["if"]
    assert "workflow_dispatch" not in publish["if"]

    publish_actions = [step.get("uses", "") for step in publish["steps"]]
    assert "pypa/gh-action-pypi-publish@release/v1" in publish_actions

    publish_inputs = [
        step.get("with", {}) for step in publish["steps"] if "pypa/gh-action-pypi-publish" in step.get("uses", "")
    ]
    assert publish_inputs, "the publish step must exist"
    assert publish_inputs[0]["password"] == "${{ secrets.PYPI_API_TOKEN }}"


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


def test_pypi_doc_documents_the_credential_path_the_workflow_implements():
    """docs/PYPI.md must describe the workflow that actually publishes.

    The doc drifted once already (it claimed trusted publishing while `main` used
    the token secret), so the credential, the environment/id-token requirements
    and the workflow_dispatch behaviour are read out of the workflow here instead
    of being restated as prose.
    """
    workflow, _ = _load_workflow()
    publish = workflow["jobs"]["publish"]
    password = _publish_step(publish["steps"])["with"]["password"].strip()
    match = re.fullmatch(r"\$\{\{ secrets\.([A-Za-z0-9_]+) \}\}", password)
    assert match, f"the publish step must read a repository secret, got {password!r}"
    secret_name = match.group(1)

    doc = PYPI_DOC.read_text(encoding="utf-8")
    token_path, _, oidc_path = doc.partition("## Alternative")
    # The primary (token) section must name the secret the step actually reads.
    assert secret_name in token_path
    # Token auth needs no OIDC identity token and no environment gate; the doc has
    # to say that, and must not repeat the old "no token is stored" claim.
    assert "id-token" not in workflow["permissions"]
    assert "id-token" not in publish["permissions"]
    assert "environment" not in publish
    assert "id-token" in token_path
    assert "environment" in token_path
    assert "no PyPI API token or password is stored" not in doc
    # A manual dispatch builds and validates only: the publish job is gated away
    # from workflow_dispatch events, and the doc must say the same.
    assert "workflow_dispatch" not in publish["if"]
    assert "workflow_dispatch" in doc
    # The trusted-publishing alternative must name the exact claims to register.
    assert oidc_path, "docs/PYPI.md must keep the OIDC alternative documented"
    # The exact claims, in the same literal form docs/PYPI.md registers them
    # (backticked values); the repository owner is product identity, which the
    # deny list allows in this form only.
    for claim in ("`TysAIs`", "`xPST`", "publish-pypi.yml", "pypi"):
        assert claim in oidc_path, f"OIDC section must name the {claim!r} claim"


def test_release_lane_publishes_with_the_same_secret_and_stays_non_blocking():
    """release.yml's duplicate PyPI attempt must not drift onto a different credential."""
    workflow, _ = _load_workflow()
    authoritative = _publish_step(workflow["jobs"]["publish"]["steps"])["with"]
    release = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    step = _publish_step(release["jobs"]["build-python"]["steps"])

    assert step["with"]["password"] == authoritative["password"]
    # Non-blocking on purpose: the GitHub release must not be held hostage.
    assert step["continue-on-error"] is True
