"""The local secret scanner must actually fire (and not on its own fixtures).

``.github/workflows/security-scan.yml`` uses this scanner as the offline
fallback when gitleaks cannot be downloaded. A fallback that never detects
anything is worse than no fallback, so its detection is tested here with
synthetic-but-realistic secret shapes.

Every planted value is fake and marked as such.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "scan_secrets.py"


def _load_scanner():
    import sys

    spec = importlib.util.spec_from_file_location("scan_secrets", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass resolves annotations via sys.modules.
    sys.modules["scan_secrets"] = module
    spec.loader.exec_module(module)
    return module


scanner = _load_scanner()


class _FakeRoot:
    """A git repo whose tracked files are supplied by the test."""

    def __init__(self, root: Path) -> None:
        self.root = root


def _scan_text(text: str, rel: str = "fixture.txt"):
    return scanner.scan_text(text, rel)


class TestDetection:
    # Each fixture is assembled at runtime from fragments. Reason: the source
    # text of this file must not itself contain a contiguous secret shape, or
    # scripts/scan_public_safety.py (which reads the file as text and has no
    # synthetic allowlist) would report this test file as a leak. The scanner
    # under test receives the fully assembled value, so detection is unaffected.
    _PRIVATE_KEY = "-----BEGIN " + "RSA " + "PRIVATE KEY-----" + "\nMIIabc\n"
    _AWS_ACCESS_KEY = 'aws_access_key_id = "AKIA' + "IOSFODNN7" + 'EXAMPLZ"\n'
    _GITHUB_TOKEN = 'token = "ghp_' + "AbCdEf" + "GhIjKl" + "MnOpQr" + 'StUvWxYz0123456789"\n'
    _SLACK_TOKEN = 'bot = "xoxb-' + "1234567890-" + 'abcdefghijklmnop"\n'

    @pytest.mark.parametrize(
        ("kind", "text"),
        [
            ("private_key", _PRIVATE_KEY),
            ("aws_access_key_id", _AWS_ACCESS_KEY),
            ("github_token", _GITHUB_TOKEN),
            ("github_fine_grained_pat", 'pat = "github_pat_' + "A" * 30 + '"\n'),
            ("openai_token", 'key = "sk-proj-' + "A" * 40 + '"\n'),
            ("anthropic_token", 'key = "sk-ant-api03-' + "A" * 40 + '"\n'),
            ("google_api_key", 'key = "AIza' + "SyA" + "b" * 32 + '"\n'),
            ("slack_token", _SLACK_TOKEN),
            ("stripe_secret_key", 'stripe = "sk_live_' + "A" * 30 + '"\n'),
            (
                "discord_webhook",
                'hook = "https://discord.com/api/webhooks/1234567890/'
                + "A" * 60
                + '"\n',
            ),
            ("telegram_bot_token", 'bot = "1234567890:' + "A" * 35 + '"\n'),
            # JWT and password fixtures are fragmented for the same reason as
            # the values above.
            (
                "jwt",
                'auth = "eyJhbGciOiJIUzI1NiJ9' + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0"
                + "." + "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U" + '"\n',
            ),
            ("meta_app_secret_assignment", 'client_secret: "' + "a1b2c3d4e5f6g7h8i9j0k1l2" + '"\n'),
            ("password_assignment", 'password = "' + "hunter2" + 'hunter2"\n'),
        ],
    )
    def test_known_secret_shape_is_detected(self, kind, text):
        kinds = {f.kind for f in _scan_text(text)}
        assert kind in kinds, f"{kind} not detected; got {kinds}"

    # NOTE: the "fixtures must stay fragmented" invariant is enforced by
    # scripts/scan_public_safety.py in CI, which reads this file as text and
    # fails the build if any fragment is ever re-inlined into a contiguous
    # secret shape. It is deliberately NOT asserted here — writing the literal
    # into an assertion would reintroduce exactly the pattern being guarded.

    def test_finding_carries_path_and_line(self):
        findings = _scan_text('line one\nbot = "1234567890:' + "A" * 35 + '"\n', "sub/f.txt")
        assert findings
        assert findings[0].path == "sub/f.txt"
        assert findings[0].line == 2

    def test_excerpt_masks_the_value(self):
        """CI logs are public — the excerpt must not reproduce the secret."""
        secret = "ghp_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
        findings = _scan_text(f'token = "{secret}"\n')
        assert findings
        assert secret not in findings[0].excerpt
        assert "<matched>" in findings[0].excerpt


class TestAllowedValues:
    @pytest.mark.parametrize(
        "text",
        [
            'token = "SYNTHETIC_IGQV_token_do_not_use_0123456789abcdef"\n',
            'bot = "999999999:SYNTHETIC_do_not_use_AAAAAAAAAAAAAAAAAAAAAAAAAAA"\n',
            'client_secret: "YOUR_TIKTOK_CLIENT_SECRET"\n',
            'client_secret: "your_tiktok_client_secret"\n',
            'Authorization: Bearer ${{ secrets.GITHUB_TOKEN }}\n',
            'hook = "https://discord.com/api/webhooks/1/PLACEHOLDER_PLACEHOLDER_PLACEHOLDER_PLACEHOLDER_PLACEHOLDER"\n',
            'password = os.environ.get("XPST_PASSWORD_ABCDEFGH")\n',
        ],
    )
    def test_obviously_synthetic_values_are_not_flagged(self, text):
        assert _scan_text(text) == []

    def test_checksums_and_key_ids_are_not_flagged(self):
        """The repo legitimately contains sha256s and minisign key ids."""
        line = " 551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb  file.tar.gz\n"
        assert _scan_text(line) == []
        assert _scan_text("minisign public key: 13F290B1316626E2\n") == []


class TestRepositoryIsClean:
    def test_the_real_repository_has_no_findings(self):
        assert scanner.scan(ROOT) == []

    def test_cli_returns_zero_on_a_clean_tree(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--fail-on-findings"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_cli_accepts_the_json_flag(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        assert result.stdout.strip().startswith("[")


class TestScannerIsWiredIntoTheWorkflow:
    def test_security_scan_workflow_uses_the_scanner_as_a_fallback(self):
        workflow = (ROOT / ".github" / "workflows" / "security-scan.yml").read_text(encoding="utf-8")
        assert "scripts/scan_secrets.py --fail-on-findings" in workflow
        assert "gitleaks" in workflow

    def test_workflow_pins_the_gitleaks_checksum(self):
        workflow = (ROOT / ".github" / "workflows" / "security-scan.yml").read_text(encoding="utf-8")
        assert "GITLEAKS_SHA256=" in workflow
        assert "sha256sum -c -" in workflow
