"""Contract tests for the macOS signing/notarization pipeline.

These assert the properties that are easy to lose in a refactor and expensive to discover in a
release: the hardening flags, the notarization+staple steps, the entitlements keys the app
genuinely needs, the "no identity -> loud failure" behaviour, and the absence of personal data
or credentials in either file.
"""

from __future__ import annotations

import plistlib
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "macos-sign-and-notarize.sh"
APP_ENTITLEMENTS = REPO_ROOT / "src-tauri" / "entitlements.plist"
ENGINE_ENTITLEMENTS = REPO_ROOT / "src-tauri" / "entitlements-engine.plist"

REQUIRED_HARDENING = ("--options runtime", "--timestamp", "--force")

# Keys without which a hardened-runtime Tauri app cannot start its own webview or load the
# bundled Python engine. Removing one is a regression, not a preference.
REQUIRED_KEYS = (
    "com.apple.security.cs.allow-jit",
    "com.apple.security.cs.allow-unsigned-executable-memory",
    "com.apple.security.cs.disable-library-validation",
)


def _script_text() -> str:
    assert SCRIPT.is_file(), f"missing {SCRIPT}"
    return SCRIPT.read_text(encoding="utf-8")


def test_script_is_syntactically_valid_bash() -> None:
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_script_signs_with_hardened_runtime_and_timestamp() -> None:
    text = _script_text()
    for flag in REQUIRED_HARDENING:
        assert flag in text, f"signing must use {flag}"


def test_script_notarizes_waits_and_staples() -> None:
    text = _script_text()
    assert "notarytool submit" in text
    assert "--wait" in text, "notarization must wait for the verdict, not fire and forget"
    assert "stapler staple" in text
    assert "stapler validate" in text


def test_script_verifies_the_bundle_after_signing() -> None:
    assert "codesign --verify --deep --strict" in _script_text()


def test_script_detects_nested_binaries_by_content_not_filename() -> None:
    """Signing order is the failure mode that reads as 'app is broken' instead of 'unsigned'."""
    text = _script_text()
    assert "Mach-O" in text, "nested binaries must be found by inspecting the file, not by suffix"
    assert "Resources/binaries/engine" in text, "the engine sidecar needs its own entitlements"


def test_script_refuses_to_pretend_it_can_sign_without_an_identity() -> None:
    """Run it with no identity: it must fail loudly and list what is available."""
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--app", "/tmp/does-not-exist.app", "--identity", ""],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin"},
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "does not exist" in combined or "no signing identity" in combined.lower()


def test_script_never_echoes_a_credential() -> None:
    text = _script_text()
    # The Apple ID password may only ever travel via the keychain profile.
    assert "APPLE_PASSWORD" not in text
    assert not re.search(r"--password\s", text), "notarytool must read the password from the keychain"
    assert "store-credentials" not in text or "notarytool store-credentials" in text


@pytest.mark.parametrize(
    "plist_path",
    [APP_ENTITLEMENTS, ENGINE_ENTITLEMENTS],
    ids=["app", "engine"],
)
def test_entitlements_parse_and_declare_required_keys(plist_path: Path) -> None:
    assert plist_path.is_file(), f"missing {plist_path}"
    with plist_path.open("rb") as handle:
        parsed = plistlib.load(handle)
    assert isinstance(parsed, dict)
    for key in REQUIRED_KEYS:
        assert parsed.get(key) is True, f"{plist_path.name} must enable {key}"


def test_docs_and_entitlements_contain_no_personal_data() -> None:
    """Signed into a public repository, these files must stay free of personal identifiers."""
    targets = [SCRIPT, APP_ENTITLEMENTS, ENGINE_ENTITLEMENTS, REPO_ROOT / "docs" / "SIGNING.md"]
    forbidden = (
        re.compile(r"[\w.%+-]+@(gmail|icloud|yahoo|hotmail)\.com", re.IGNORECASE),
        re.compile(r"/Users/[A-Za-z0-9._-]+"),
        re.compile(r"\+1[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}"),
    )
    for path in targets:
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            match = pattern.search(text)
            assert match is None, f"{path.name} contains personal data: {match.group(0)!r}"
