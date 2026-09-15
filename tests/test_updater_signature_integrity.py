"""The update path cannot be silently compromised.

The Tauri updater is the only code path that can replace the user's binary, so
two properties must hold and be re-checked on every change:

1. A **public key is present** and is a real minisign public key — without it the
   Tauri client refuses to install anything (fail closed), and with a *wrong* key
   it would accept an attacker's manifest.
2. **No code path disables signature verification** — no
   ``dangerousAcceptInvalidCerts`` / ``dangerousAcceptInvalidHostnames``, no
   ``insecure`` flag, no unsigned-update escape hatch.

Reads the real config and Rust source; does not run a build.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TAURI_CONF = ROOT / "src-tauri" / "tauri.conf.json"
TAURI_SRC = ROOT / "src-tauri" / "src"
CARGO_TOML = ROOT / "src-tauri" / "Cargo.toml"

# Flags that would weaken or remove update signature verification.
_FORBIDDEN_PATTERNS = (
    r"dangerous[Aa]ccept[Ii]nvalid[Cc]erts",
    r"dangerous[Aa]ccept[Ii]nvalid[Hh]ostnames",
    r"dangerous_accept_invalid",
    r"accept_invalid_certs",
    r"accept_invalid_hostnames",
    r"verify_signature\s*[:=]\s*(false|False)",
    r"disable_signature",
    r"skip_verification",
    r"insecure\s*[:=]\s*(true|True)",
    r"--no-sign",  # Tauri CLI flag that skips Apple signing
)


@pytest.fixture(scope="module")
def tauri_config() -> dict:
    return json.loads(TAURI_CONF.read_text(encoding="utf-8"))


class TestUpdaterPublicKey:
    def test_updater_plugin_is_configured(self, tauri_config):
        updater = tauri_config.get("plugins", {}).get("updater")
        assert updater is not None, "plugins.updater missing — the updater would be unconfigurable"

    def test_public_key_is_present_and_non_empty(self, tauri_config):
        pubkey = tauri_config["plugins"]["updater"].get("pubkey")
        assert isinstance(pubkey, str) and pubkey.strip(), "updater pubkey is empty"
        assert len(pubkey) > 60, "updater pubkey is implausibly short"

    def test_public_key_is_a_minisign_public_key(self, tauri_config):
        """Tauri expects base64 of the two-line minisign text, used verbatim."""
        raw = base64.b64decode(tauri_config["plugins"]["updater"]["pubkey"]).decode("utf-8")
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        assert lines[0].startswith("untrusted comment: minisign public key: "), lines[0]
        key_id = lines[0].rsplit(":", 1)[-1].strip()
        assert re.fullmatch(r"[0-9A-F]{16}", key_id), f"unexpected minisign key id: {key_id}"
        # Second line is the base64 payload of the key itself.
        assert len(base64.b64decode(lines[1])) >= 40

    def test_endpoint_is_https(self, tauri_config):
        endpoints = tauri_config["plugins"]["updater"].get("endpoints") or []
        assert endpoints, "no updater endpoint configured"
        for endpoint in endpoints:
            assert endpoint.startswith("https://"), f"insecure update endpoint: {endpoint}"

    def test_key_id_is_consistent_with_the_documented_key(self, tauri_config):
        """The committed key id is what the docs/manifest claim it is."""
        raw = base64.b64decode(tauri_config["plugins"]["updater"]["pubkey"]).decode("utf-8")
        key_id = raw.splitlines()[0].rsplit(":", 1)[-1].strip()
        assert re.fullmatch(r"[0-9A-F]{16}", key_id)
        # A rotation must update this test deliberately, not slip through.
        assert key_id == "13F290B1316626E2"


class TestNoVerificationBypass:
    def test_no_forbidden_flags_in_tauri_sources(self):
        hits: list[str] = []
        for path in sorted(TAURI_SRC.rglob("*.rs")):
            text = path.read_text(encoding="utf-8")
            for pattern in _FORBIDDEN_PATTERNS:
                for match in re.finditer(pattern, text):
                    line = text[: match.start()].count("\n") + 1
                    hits.append(f"{path.relative_to(ROOT)}:{line} {match.group(0)}")
        assert hits == [], f"signature-verification bypass flags found: {hits}"

    def test_no_forbidden_flags_in_tauri_config(self):
        text = TAURI_CONF.read_text(encoding="utf-8")
        for pattern in _FORBIDDEN_PATTERNS:
            assert not re.search(pattern, text), f"forbidden flag in tauri.conf.json: {pattern}"

    def test_updater_dependency_is_the_signed_plugin(self):
        cargo = CARGO_TOML.read_text(encoding="utf-8")
        assert "tauri-plugin-updater" in cargo, "updater plugin dependency missing"

    def test_install_goes_through_download_and_install(self):
        """The only install call must be the plugin's signature-verified one."""
        text = (TAURI_SRC / "lib.rs").read_text(encoding="utf-8")
        assert "download_and_install(" in text, "no verified install path found"
        # No hand-rolled replacement of the binary (which would skip the check).
        for pattern in (r"fs::copy\([^)]*current_exe", r"std::fs::rename\([^)]*current_exe"):
            assert not re.search(pattern, text), f"hand-rolled binary replacement: {pattern}"

    def test_updater_check_is_opt_in_and_cannot_be_forced(self):
        """The check is env-gated; an attacker cannot make it run unrequested."""
        text = (TAURI_SRC / "lib.rs").read_text(encoding="utf-8")
        assert "XPST_UPDATER_CHECK" in text
        gate_lines = [ln for ln in text.splitlines() if "XPST_UPDATER_CHECK" in ln and "std::env::var" in ln]
        assert gate_lines, "no env gate around the updater check"
        # Must be an equality check against the explicit value "1".
        assert any('== Some("1")' in ln or '== "1"' in ln for ln in gate_lines), gate_lines

    def test_e2e_script_uses_a_throwaway_key_not_the_shipped_one(self):
        """The E2E harness must not use (or overwrite) the committed pubkey."""
        script = (ROOT / "scripts" / "updater-e2e.sh").read_text(encoding="utf-8")
        assert "xpst-updater-e2e.key" in script
        assert "13F290B1316626E2" not in script, "the E2E script must not reference the shipped key id"

    def test_no_signed_manifest_is_fabricated_with_an_empty_signature(self):
        """A manifest claiming a signature it does not have is an outage, not a bypass."""
        hits: list[str] = []
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")) + sorted((ROOT / "scripts").glob("*.sh")):
            text = path.read_text(encoding="utf-8")
            if "latest.json" not in text:
                continue
            for bad in ('"signature": ""', "signature: ''", '"signature":""'):
                if bad in text:
                    hits.append(f"{path.name}: {bad}")
        assert hits == [], f"manifest written with an empty signature: {hits}"

    def test_tauri_release_does_not_pass_no_sign(self):
        """`--no-sign` also skips Apple codesigning; the repo must not use it."""
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            assert "--no-sign" not in text, f"{path.name} passes --no-sign"
