"""Signature integrity must be enforced independently of release policy."""

from unittest.mock import patch

import pytest

from scripts.verify_macos_artifact import verify_macos_artifact


@pytest.fixture
def app(tmp_path):
    bundle = tmp_path / "xPST.app"
    macos = bundle / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (bundle / "Contents" / "Info.plist").write_text("<plist></plist>")
    (macos / "xPST").write_bytes(b"launcher")
    return bundle


def _verify(app, verify_error, display_error, *, display_ok=False, require_developer_id=False):
    def run(command):
        display = "--display" in command
        error = display_error if display else verify_error
        ok = display_ok if display else not verify_error
        return {
            "ok": ok,
            "command": command,
            "returncode": 0 if ok else 1,
            "stdout": "",
            "stderr": error,
        }

    with (
        patch("scripts.verify_macos_artifact.platform.system", return_value="Darwin"),
        patch("scripts.verify_macos_artifact.shutil.which", side_effect=lambda name: "/usr/bin/codesign" if name == "codesign" else None),
        patch("scripts.verify_macos_artifact._run", side_effect=run),
    ):
        return verify_macos_artifact(app, require_developer_id=require_developer_id)


@pytest.mark.parametrize("display", ["Signature=adhoc", "Authority=Developer ID Application: Example (TEAM)"])
def test_corrupted_existing_signature_blocks_local_artifact(app, display):
    result = _verify(
        app,
        f"{app}: a sealed resource is missing or invalid",
        display,
        display_ok=True,
    )

    assert result["ok"] is False
    assert "codesign_verify" in {check["id"] for check in result["blocking"]}


@pytest.mark.parametrize("require_developer_id", [False, True])
def test_unsigned_bundle_is_explicitly_distinct_from_valid_signature(app, require_developer_id):
    unsigned = f"{app}: code object is not signed at all"
    result = _verify(app, unsigned, unsigned, require_developer_id=require_developer_id)

    assert result["ok"] is (not require_developer_id)
    check = next(check for check in result["checks"] if check["id"] == "codesign_verify")
    assert check["signed"] is False
    assert check["unsigned"] is True
    assert check["result"]["ok"] is False


@pytest.mark.parametrize(
    "verify_error,display_error,display_ok",
    [
        ("invalid signature (code or signature have been modified)", "invalid signature", False),
        ("operation timed out", "operation timed out", False),
        ("code object is not signed at all\nIn subcomponent: nested.dylib", "Signature=adhoc", True),
        ("code object is not signed at all", "Signature=adhoc", True),
    ],
)
def test_unverified_or_nested_unsigned_code_is_not_an_unsigned_bundle(app, verify_error, display_error, display_ok):
    result = _verify(app, f"{app}: {verify_error}", display_error, display_ok=display_ok)

    assert result["ok"] is False
    assert "codesign_verify" in {check["id"] for check in result["blocking"]}


@pytest.mark.parametrize("display", ["Signature=adhoc", "Authority=Developer ID Application: Example (TEAM)"])
def test_valid_existing_signature_is_accepted_locally(app, display):
    result = _verify(app, "", display, display_ok=True)

    assert result["ok"] is True
    check = next(check for check in result["checks"] if check["id"] == "codesign_verify")
    assert check["signed"] is True
