"""Repository-level launch asset checks."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import yaml

from scripts.verify_desktop_package import _check_qt_lgpl_notice, verify_desktop_package

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
    assert workflow["jobs"]["github-release"]["needs"] == ["build-python", "build-windows", "build-linux", "build-macos"]

    # W3-2: the Linux desktop release lane must build, smoke, and attest a binary.
    linux_steps = "\n".join(str(step.get("run", "")) for step in workflow["jobs"]["build-linux"]["steps"])
    assert "pyinstaller --clean --noconfirm build_linux.spec" in linux_steps
    assert "scripts/verify_linux_binary.py" in linux_steps

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
        "python scripts/verify_desktop_package.py",
        "QT_QPA_PLATFORM=offscreen python scripts/verify_qml_pages.py",
        "python scripts/release_artifacts.py --dist dist --output-dir release/python --skip-checks",
    ]:
        assert required in python_steps
    python_uses = "\n".join(str(step.get("uses", "")) for step in workflow["jobs"]["build-python"]["steps"])
    assert "actions/attest@v4" in python_uses
    python_step_text = "\n".join(str(step) for step in workflow["jobs"]["build-python"]["steps"])
    assert "release/python/*" in python_step_text
    assert "!release/python/SHA256SUMS" in python_step_text
    assert "!release/python/SHA512SUMS" in python_step_text

    windows_steps = "\n".join(str(step.get("run", "")) for step in workflow["jobs"]["build-windows"]["steps"])
    assert "pyinstaller --clean --noconfirm build_windows.spec" in windows_steps
    assert "python scripts/verify_desktop_package.py" in windows_steps
    assert "scripts\\sign_windows.ps1 -Path dist\\xPST.exe" in windows_steps
    assert '$smokeArgs = @("--path", "dist\\xPST.exe", "--seconds", "12", "--json", "--clean-profile")' in windows_steps
    assert '${{ github.event_name }}" -eq "push"' in windows_steps
    assert '$smokeArgs += "--require-signed"' in windows_steps
    assert "python scripts/verify_windows_exe.py @smokeArgs" in windows_steps
    assert "python scripts/release_artifacts.py --dist dist --output-dir release/windows --skip-checks" in windows_steps
    windows_uses = "\n".join(str(step.get("uses", "")) for step in workflow["jobs"]["build-windows"]["steps"])
    assert "actions/attest@v4" in windows_uses
    windows_step_text = "\n".join(str(step) for step in workflow["jobs"]["build-windows"]["steps"])
    assert "release/windows/*" in windows_step_text
    assert "!release/windows/SHA256SUMS" in windows_step_text
    assert "!release/windows/SHA512SUMS" in windows_step_text

    macos_steps = "\n".join(str(step.get("run", "")) for step in workflow["jobs"]["build-macos"]["steps"])
    assert "bash scripts/verify_macos.sh" in macos_steps
    assert "macos_args+=(--public)" in macos_steps
    macos_step_text = "\n".join(str(step) for step in workflow["jobs"]["build-macos"]["steps"])
    assert "secrets.MACOS_CODESIGN_IDENTITY" in macos_step_text
    assert "secrets.APPLE_APP_PASSWORD" in macos_step_text
    macos_uses = "\n".join(str(step.get("uses", "")) for step in workflow["jobs"]["build-macos"]["steps"])
    assert "actions/attest@v4" in macos_uses
    assert "release/*" in macos_step_text
    assert "!release/SHA256SUMS" in macos_step_text
    assert "!release/SHA512SUMS" in macos_step_text
    verify_macos = (ROOT / "scripts" / "verify_macos.sh").read_text(encoding="utf-8")
    assert "MACOS_CODESIGN_IDENTITY" in verify_macos
    assert "bash scripts/sign_macos.sh dist/xPST.app" in verify_macos
    assert "--require-developer-id --require-notarized" in verify_macos

    linux_step_text = "\n".join(str(step) for step in workflow["jobs"]["build-linux"]["steps"])
    assert "release/linux/*" in linux_step_text
    assert "!release/linux/SHA256SUMS" in linux_step_text
    assert "!release/linux/SHA512SUMS" in linux_step_text

    release_steps = "\n".join(str(step.get("run", "")) for step in workflow["jobs"]["github-release"]["steps"])
    assert "cd release-artifacts" in release_steps
    assert "rm -f SHA256SUMS SHA512SUMS" in release_steps
    assert "sha256sum > SHA256SUMS" in release_steps
    assert "sha512sum > SHA512SUMS" in release_steps


def test_desktop_package_specs_include_runtime_assets_and_dynamic_imports():
    result = verify_desktop_package(ROOT)

    assert result["ok"] is True
    assert {"DashboardPage.qml", "ConnectPage.qml"} <= set(result["qml_pages"])


def test_desktop_package_gate_includes_qt_lgpl_notice():
    result = verify_desktop_package(ROOT)

    lgpl_checks = [check for check in result["checks"] if check["path"] == "NOTICES_QT_LGPL.md"]
    assert lgpl_checks, "verify gate must check the Qt/PySide6 LGPL notice"
    assert lgpl_checks[0]["ok"] is True


def test_qt_lgpl_notice_gate_fails_when_notice_missing(tmp_path):
    check = _check_qt_lgpl_notice(tmp_path)

    assert check["ok"] is False
    assert any("missing" in issue for issue in check["issues"])


def test_qt_lgpl_notice_gate_fails_without_relink_offer(tmp_path):
    (tmp_path / "NOTICES_QT_LGPL.md").write_text(
        "PySide6/Qt under the LGPL, but no offer here.", encoding="utf-8"
    )

    check = _check_qt_lgpl_notice(tmp_path)

    assert check["ok"] is False
    assert any("relink" in issue for issue in check["issues"])


def test_qt_lgpl_notice_gate_passes_with_complete_notice(tmp_path):
    (tmp_path / "NOTICES_QT_LGPL.md").write_text(
        "PySide6 / Qt are distributed under the LGPL. Written offer to relink "
        "against a modified Qt is provided.",
        encoding="utf-8",
    )

    check = _check_qt_lgpl_notice(tmp_path)

    assert check["ok"] is True
    assert check["issues"] == []


def test_qt_lgpl_notice_documents_lgpl_attribution_and_relink_offer():
    text = (ROOT / "NOTICES_QT_LGPL.md").read_text(encoding="utf-8")

    assert "LGPL" in text
    assert "PySide6" in text and "Qt" in text
    assert "relink" in text.lower()


def test_content_page_requires_post_preview_before_upload():
    text = (ROOT / "src" / "xpst" / "desktop_app" / "qml" / "pages" / "ContentPage.qml").read_text(encoding="utf-8")

    assert "controller.previewPost" in text
    assert "postPreviewDialog.open()" in text
    assert "batchPreviewDialog.open()" in text
    assert "function confirmPendingPost()" in text
    assert "function confirmBatchPost()" in text
    assert "controller.postVideo(pendingPost.videoPath, pendingPost.caption)" in text
    assert "controller.postVideo(item.videoPath, item.caption)" in text
    assert 'showToast("Batch posting' not in text


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
