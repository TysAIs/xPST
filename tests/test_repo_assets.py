"""Repository-level launch asset checks."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from urllib.parse import unquote

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
LOCAL_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\((?!https?://)([^)]+)\)")
LOCAL_MARKDOWN_LINK = re.compile(
    r"(?<!!)(?<!\\)\[[^\]\n]+\]\((?!https?://|mailto:|#)([^)\s]+(?:\s+\"[^\"]*\")?)\)"
)
SKIPPED_MARKDOWN_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "build",
    "dist",
    # Tauri build outputs (gitignored): the PyInstaller onedir engine ships
    # third-party dist-info license markdown whose relative links are
    # unresolvable inside _internal/. Those files are artifacts, never reviewed
    # content — checking them makes this test pass on a clean checkout but fail
    # after `scripts/build-engine.sh`.
    "target",
    "binaries",
    # npm installs (gitignored): third-party package READMEs carry relative
    # links (CHANGELOG, LICENSE) that do not resolve in an installed tree.
    # Building the UI is a normal local step, so `pytest` must stay green
    # after `npm ci` in ui/.
    "node_modules",
}


def _local_markdown_targets(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    targets: list[str] = []

    for regex in (LOCAL_MARKDOWN_IMAGE, LOCAL_MARKDOWN_LINK):
        for match in regex.finditer(text):
            raw_target = match.group(1).strip()
            target = raw_target.split(" ", 1)[0].strip("<>")
            target = unquote(target).split("#", 1)[0]
            if target and not target.startswith(("/", "http:", "https:", "mailto:")):
                targets.append(target)

    return targets


def test_issue_templates_cover_launch_support_paths():
    templates = {
        "platform-breakage.yml": {"platform", "workflow", "what-happened", "version", "os"},
        "install-failure.yml": {"install-method", "command", "output", "os"},
        "provider-request.yml": {"provider", "role", "workflow"},
    }

    for filename, required_ids in templates.items():
        path = ROOT / ".github" / "ISSUE_TEMPLATE" / filename
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        field_ids = {item.get("id") for item in data.get("body", []) if isinstance(item, dict)}
        assert data["name"]
        assert data["labels"]
        assert required_ids <= field_ids


def test_issue_template_config_routes_security_reports_privately():
    config = yaml.safe_load((ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text(encoding="utf-8"))

    links = config.get("contact_links", [])
    assert any("security/policy" in link.get("url", "") for link in links)


def test_dockerignore_excludes_runtime_data_and_secrets():
    text = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    for pattern in [".git", ".venv", ".xpst", ".env", "*token*.json", "*cookies*.json", "release-smoke"]:
        assert pattern in text


def test_docker_assets_reference_existing_entrypoint_and_current_commands():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")
    ci_workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))

    assert "COPY docker-entrypoint.sh /docker-entrypoint.sh" in dockerfile
    for command in ["diagnostics", "providers", "readiness", "schedule", "plugins"]:
        assert command in entrypoint
    assert "docker" in ci_workflow["jobs"]
    docker_steps = "\n".join(str(step.get("run", "")) for step in ci_workflow["jobs"]["docker"]["steps"])
    assert "docker build -t xpst:ci ." in docker_steps
    assert "docker run --rm xpst:ci version --json" in docker_steps
    ci_steps = "\n".join(str(step.get("run", "")) for step in ci_workflow["jobs"]["test"]["steps"])
    assert "python scripts/release_preflight.py --json" in ci_steps
    assert "python scripts/scan_public_safety.py --json" in ci_steps


def test_contributing_uses_current_repository_and_no_mojibake():
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")

    assert "https://github.com/TysAIs/xPST" in text
    # Personal-name guard: assert no author-identifying tokens leak into assets
    for marker in ("".join(chr(c) for c in (84,121,108,101,114)),):
        assert marker not in text
    assert "ð" not in text
    assert "â" not in text

def test_readme_local_images_exist_and_pngs_are_valid():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    image_paths = [match.group(1).split("#", 1)[0] for match in LOCAL_MARKDOWN_IMAGE.finditer(readme)]

    # Zero local images is valid (screenshots removed pending demo-data set);
    # when present, each must exist and be a real PNG.
    if not image_paths:
        return
    for image_path in image_paths:
        path = ROOT / image_path
        assert path.exists(), image_path
        if path.suffix.lower() == ".png":
            assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"), image_path


def test_local_markdown_links_point_to_existing_files():
    broken_links: list[str] = []

    for markdown_file in sorted(ROOT.rglob("*.md")):
        if any(part in SKIPPED_MARKDOWN_DIRS for part in markdown_file.relative_to(ROOT).parts):
            continue

        for target in _local_markdown_targets(markdown_file):
            if not (markdown_file.parent / target).exists():
                broken_links.append(f"{markdown_file.relative_to(ROOT)} -> {target}")

    assert broken_links == []


def test_release_workflow_preserves_required_ship_gates():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))

    assert workflow["permissions"]["id-token"] == "write"
    assert workflow["permissions"]["attestations"] == "write"
    # One desktop app: the Python lane is the only builder left in this workflow,
    # and the release job depends on it alone. The desktop installers come from
    # tauri-release.yml.
    assert workflow["jobs"]["github-release"]["needs"] == ["build-python"]
    assert set(workflow["jobs"]) == {"build-python", "github-release"}

    python_steps = "\n".join(str(step.get("run", "")) for step in workflow["jobs"]["build-python"]["steps"])
    for required in [
        "python -m pytest",
        "ruff check src tests",
        "mypy src/xpst",
        "pip-audit",
        "python scripts/scan_public_safety.py --json",
        "python scripts/build_package.py",
        "python scripts/release_preflight.py --json",
        "python scripts/clean_install_smoke.py --dist dist --artifact both",
        "python scripts/release_artifacts.py --dist dist --output-dir release/python --skip-checks --lane python",
    ]:
        assert required in python_steps
    python_uses = "\n".join(str(step.get("uses", "")) for step in workflow["jobs"]["build-python"]["steps"])
    assert "actions/attest@v4" in python_uses
    python_step_text = "\n".join(str(step) for step in workflow["jobs"]["build-python"]["steps"])
    assert "release/python/*" in python_step_text


def test_no_legacy_pyside_desktop_build_remains():
    """The repo must build exactly ONE desktop app (the Tauri shell)."""
    for legacy in ["build_macos.spec", "build_windows.spec", "build_linux.spec", "build.sh"]:
        assert not (ROOT / legacy).exists(), f"{legacy} still exists"
    assert not list((ROOT / "src").rglob("*.qml")), "QML desktop pages still exist"
    assert (ROOT / "build_engine.spec").exists(), "the Tauri engine-sidecar spec must stay"

    # No workflow may invoke PyInstaller against a desktop spec other than the
    # sidecar's, and none may name the retired macOS bundle.
    for workflow_path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        steps = [
            step
            for job in workflow.get("jobs", {}).values()
            for step in job.get("steps", [])
        ]
        run_text = "\n".join(str(step.get("run", "")) for step in steps)
        for legacy in ("build_macos.spec", "build_windows.spec", "build_linux.spec"):
            assert legacy not in run_text, f"{workflow_path.name} still runs {legacy}"
        if "pyinstaller" in run_text.lower():
            assert "build_engine.spec" in run_text or "build-engine.sh" in run_text, (
                f"{workflow_path.name} runs PyInstaller without build_engine.spec"
            )

    tauri = (ROOT / ".github" / "workflows" / "tauri-release.yml").read_text(encoding="utf-8")
    assert "cargo tauri build" in tauri


def test_removed_legacy_desktop_modules_are_not_importable() -> None:
    """The deleted PySide6/QML desktop must not be importable.

    Deleting the files is not enough on its own: a stray module on ``sys.path``
    (an old editable install, a leftover build tree) would otherwise keep
    resolving ``xpst.desktop_app`` and the suite would silently exercise a
    module the repository no longer ships.
    """
    for module in ("xpst.desktop_app", "xpst.desktop"):
        assert importlib.util.find_spec(module) is None, (
            f"{module} still resolves — a deleted desktop module is back on sys.path"
        )
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)


def test_security_docs_match_encrypted_credential_fallback():
    credential_source = (ROOT / "src" / "xpst" / "utils" / "credentials.py").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    privacy = (ROOT / "docs" / "PRIVACY.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "Fallback files encrypted" in credential_source
    assert "cryptography" in credential_source
    assert "not encrypted by xPST" not in security
    assert "not encrypted by xPST" not in privacy
    assert "local JSON files protected" not in readme
    for text in [security, privacy, readme]:
        assert ".enc" in text


def test_license_metadata_is_consistent_across_public_files():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    notices = (ROOT / "NOTICES.md").read_text(encoding="utf-8")
    licensing_report = (ROOT / "LICENSING_REPORT.md").read_text(encoding="utf-8")

    assert 'license = "MIT OR Apache-2.0"' in pyproject
    assert "MIT License" in license_text
    assert "Apache License" in license_text
    assert "at your option" in license_text
    for text in [readme, notices, licensing_report]:
        assert "MIT" in text
        assert "Apache" in text
