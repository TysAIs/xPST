"""Structural tests for the updater manifest workflow.

The workflow this file guards failed instantly with zero steps because it used
the `runner` context in a workflow-level `env:` block, which GitHub rejects as
an invalid workflow file. These tests keep the file publishable: valid
structure, no out-of-scope context use, no step that turns an absent signing
secret into a failed job, and no reference to a script that does not exist.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/publish-updater.yml"
SERVED_PATH = "updates/latest.json"

# Contexts GitHub allows in a workflow-level or job-level `env:` block.
JOB_SCOPE_CONTEXTS = {"github", "inputs", "matrix", "needs", "secrets", "strategy", "vars", "env"}
EXPRESSION_RE = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
GITHUB_CONTEXT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)\.([A-Za-z_][A-Za-z0-9_.]*)")
SCRIPT_RE = re.compile(r"(?:python3?\s+|bash\s+|\./)?(scripts/[A-Za-z0-9_.-]+)")


def load_workflow() -> dict[str, Any]:
    text = WORKFLOW.read_text(encoding="utf-8")
    # Quote the trigger key so YAML's boolean coercion cannot turn `on` into True.
    text = re.sub(r"^on:", "'on':", text, count=1, flags=re.MULTILINE)
    return yaml.safe_load(text)


def job_scope_expressions() -> list[tuple[str, str]]:
    """Every expression outside a `steps:` list, with a label.

    `runner`, `steps` and `job` are not available at workflow or job scope; using
    one there makes the whole file invalid and GitHub runs nothing.
    """
    workflow = load_workflow()
    found: list[tuple[str, str]] = []

    def walk(label: str, node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(f"{label}.{key}", value)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(f"{label}[{index}]", value)
        elif isinstance(node, str):
            expressions = EXPRESSION_RE.findall(node)
            # A job-level `if:` is an expression without the `${{ }}` wrapper.
            if label.endswith(".if"):
                expressions.append(node)
            found.extend((label, expression) for expression in expressions)

    for key, value in workflow.items():
        if key in ("on", "jobs"):
            continue
        walk(key, value)
    for job_name, job in (workflow.get("jobs") or {}).items():
        for key, value in job.items():
            if key == "steps":
                continue
            walk(f"jobs.{job_name}.{key}", value)
    return found


def test_workflow_file_exists_and_is_valid_yaml() -> None:
    assert WORKFLOW.is_file()
    workflow = load_workflow()

    assert isinstance(workflow, dict)
    assert workflow["name"] == "Publish Tauri updater manifest"
    assert set(workflow["jobs"]) == {"publish"}


def test_triggers_cover_release_workflow_run_and_dispatch() -> None:
    workflow = load_workflow()
    triggers = workflow["on"]

    assert set(triggers) == {"release", "workflow_run", "workflow_dispatch"}
    assert triggers["release"]["types"] == ["published"]
    assert triggers["workflow_run"]["workflows"] == ["Tauri Shell Release"]
    assert triggers["workflow_run"]["types"] == ["completed"]
    assert triggers["workflow_dispatch"]["inputs"]["release_tag"]["required"] is True


def test_permissions_allow_the_manifest_commit_and_fallback_pr() -> None:
    workflow = load_workflow()

    assert workflow["permissions"]["contents"] == "write"
    assert workflow["permissions"]["pull-requests"] == "write"


def test_no_out_of_scope_context_outside_steps() -> None:
    expressions = job_scope_expressions()
    assert expressions, "expected at least one workflow- or job-scope expression to inspect"

    for label, expression in expressions:
        for context, _ in GITHUB_CONTEXT_RE.findall(expression):
            assert context in JOB_SCOPE_CONTEXTS, (
                f"{label} uses the '{context}' context, which GitHub rejects at that "
                "scope and turns into an invalid workflow file (run 34811052542)"
            )


def test_the_context_that_broke_the_workflow_is_gone() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "${{ runner." not in text
    # The documented failure is quoted in the header comment on purpose.
    assert "Invalid workflow file" not in text


def test_runner_paths_are_step_scoped() -> None:
    workflow = load_workflow()
    steps = workflow["jobs"]["publish"]["steps"]

    # The runs that need a scratch directory read it from the runner's own
    # environment instead of an expression that must survive workflow validation.
    assert "$RUNNER_TEMP" in WORKFLOW.read_text(encoding="utf-8")
    assert all("${{ runner." not in str(step.get("run", "")) for step in steps)


def test_checkout_is_pinned_to_the_served_branch() -> None:
    steps = load_workflow()["jobs"]["publish"]["steps"]
    checkout = next(step for step in steps if str(step.get("uses", "")).startswith("actions/checkout"))

    assert checkout["with"]["ref"] == "main"


def test_absent_signing_secret_degrades_instead_of_failing() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "secrets.TAURI_SIGNING_PRIVATE_KEY" in text
    assert "No TAURI_SIGNING_PRIVATE_KEY secret is configured" in text
    # The job must end green when there is nothing to publish.
    assert "exit 1" not in text
    signing_step = next(
        step
        for step in load_workflow()["jobs"]["publish"]["steps"]
        if step.get("id") == "signing"
    )
    assert "SIGNING_KEY" in signing_step["env"]


def test_publishes_the_path_pages_serves() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert SERVED_PATH in text
    assert "HEAD:refs/heads/main" in text
    assert "scripts/publish-updater.sh" in text


def test_every_referenced_script_exists() -> None:
    steps = load_workflow()["jobs"]["publish"]["steps"]
    referenced = {
        match
        for step in steps
        for match in SCRIPT_RE.findall(str(step.get("run", "")))
    }

    assert referenced, "expected the workflow to call the repository scripts"
    missing = sorted(name for name in referenced if not (ROOT / name).is_file())
    assert missing == []


def test_manifest_path_is_not_gitignored() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    rules = [line.strip() for line in ignored if line.strip() and not line.strip().startswith("#")]

    assert not any(rule.strip("/") == "updates" for rule in rules)


def test_selector_and_generator_agree_on_the_platform_set() -> None:
    selector = (ROOT / "scripts/select-updater-artifacts.py").read_text(encoding="utf-8")
    generator = (ROOT / "scripts/gen-updater-manifest.py").read_text(encoding="utf-8")

    for platform in ("darwin-aarch64", "darwin-x86_64", "windows-x86_64", "linux-x86_64"):
        assert platform in selector
        assert platform in generator
