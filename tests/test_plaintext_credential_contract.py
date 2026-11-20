"""The documented credential files must match what the code actually writes.

Issue #295: SECURITY.md claimed credentials were "encrypted at rest" while
``xpst/utils/sessions.py`` mirrored every refreshed token/cookie/session into a
plaintext ``.json`` sibling that the runtime really reads. A user backing up
dotfiles shipped live tokens in the clear.

Two failure directions are pinned here:

* docs claim encryption the code does not deliver — the documented plaintext set
  is compared against the executable registry
  (:data:`xpst.utils.secure_io.PLAINTEXT_CREDENTIAL_FILES`), against every
  credentials-directory ``*.json`` the source names, and against the behaviour
  that makes the files plaintext in the first place;
* a *new* plaintext credential file appears without being documented — the
  source scan fails on any credentials-dir JSON that is neither in the registry
  nor in the explicit non-secret allowlist below.

Synthetic secret values only; no real credentials are read or written.
"""

from __future__ import annotations

import json
import re
import stat
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xpst.utils.secure_io import PLAINTEXT_CREDENTIAL_FILES, write_text_0600

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "xpst"
SECURITY_MD = REPO_ROOT / "SECURITY.md"
README_MD = REPO_ROOT / "README.md"
PRIVACY_MD = REPO_ROOT / "docs" / "PRIVACY.md"

#: ``<!-- xpst-plaintext-credentials: a.json b.json ... -->`` — the machine
#: readable half of the SECURITY.md statement.
_MARKER_RE = re.compile(r"<!--\s*xpst-plaintext-credentials:\s*([^>]*?)\s*-->")

#: Credentials-directory JSON files that are deliberately NOT in the registry.
#:
#: * ``youtube_client_secrets.json`` is the user-supplied OAuth *client*
#:   identity from Google Cloud Console — xPST never writes it, and it is not a
#:   per-user secret (SECURITY.md says so next to the registry).
#: * ``_keyring_index.json`` holds credential *key names* only, no values.
NON_SECRET_CREDENTIAL_DIR_JSON: frozenset[str] = frozenset(
    {"youtube_client_secrets.json", "_keyring_index.json"}
)

#: ``"credentials" / "x_cookies.json"``, ``creds_dir / "..."``, ``Path("credentials") / "..."``
_CRED_JSON_REF_RE = re.compile(
    r'(?:"credentials"|creds_dir|credentials_dir)\s*\)?\s*/\s*f?"([^"]*\.json)"'
)

#: Claims that were literally false and must not come back.
#:
#: Each string below appeared in the shipped docs while the plaintext mirrors
#: existed: they asserted that *everything* was encrypted / that no plaintext
#: secret was on disk. Any of them reappearing means the claim has diverged
#: from the code again.
_DISPROVEN_CLAIMS: tuple[tuple[str, str], ...] = (
    (
        "SECURITY.md",
        "No plain-text secrets on disk",
    ),
    (
        "SECURITY.md",
        "All authentication tokens, session cookies, and API keys are stored using a layered security approach",
    ),
    (
        "README.md",
        "The OAuth token is stored in your OS keychain",
    ),
)

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX permission bits; Windows uses ACLs instead of 0600",
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _documented_plaintext_files() -> set[str]:
    text = SECURITY_MD.read_text(encoding="utf-8")
    matches = _MARKER_RE.findall(text)
    assert len(matches) == 1, (
        "SECURITY.md must carry exactly one "
        f"`<!-- xpst-plaintext-credentials: ... -->` marker, found {len(matches)}"
    )
    return {name for name in matches[0].split() if name}


class TestDocumentedSetMatchesCode:
    def test_security_md_documents_exactly_the_registry(self) -> None:
        documented = _documented_plaintext_files()
        registered = set(PLAINTEXT_CREDENTIAL_FILES.values())
        assert documented == registered, (
            "SECURITY.md and PLAINTEXT_CREDENTIAL_FILES disagree about which "
            f"credential files are plaintext: docs-only={sorted(documented - registered)}, "
            f"code-only={sorted(registered - documented)}. Update both in the same change."
        )

    def test_every_registry_entry_lives_in_the_credentials_dir(self) -> None:
        for platform, filename in PLAINTEXT_CREDENTIAL_FILES.items():
            assert filename.endswith(".json"), f"{platform}: {filename} is not a JSON file"
            assert "/" not in filename, f"{platform}: {filename} must be a bare file name"

    def test_no_undocumented_plaintext_credential_file_in_source(self) -> None:
        """A new credentials-dir JSON cannot appear without being documented."""
        allowed = set(PLAINTEXT_CREDENTIAL_FILES.values()) | NON_SECRET_CREDENTIAL_DIR_JSON
        found: dict[str, set[str]] = {}
        for path in sorted(SRC_ROOT.rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in _CRED_JSON_REF_RE.finditer(text):
                name = match.group(1)
                if not name:  # f-string name computed at runtime; covered by the registry itself
                    continue
                if name not in allowed:
                    found.setdefault(name, set()).add(str(path.relative_to(REPO_ROOT)))
        assert found == {}, (
            "the source names credentials-directory JSON files that are neither in "
            f"PLAINTEXT_CREDENTIAL_FILES nor explicitly non-secret: {found}. "
            "Document it in SECURITY.md (and pin its 0600 mode) or stop writing it."
        )

    def test_cli_session_health_uses_the_registry(self) -> None:
        """The one per-platform file table in the CLI must read the registry.

        Otherwise the CLI could keep reporting a file the registry no longer
        lists (or miss a new one) without any test noticing.
        """
        from xpst.cli import _session_health

        config = MagicMock()
        config.config_dir = str(REPO_ROOT / "does-not-exist")
        reported = _session_health(config)
        assert set(reported) == set(PLAINTEXT_CREDENTIAL_FILES)
        for platform in PLAINTEXT_CREDENTIAL_FILES:
            assert reported[platform]["present"] is False


class TestDocsStateTheTruth:
    def test_security_md_names_each_file_as_plaintext_in_the_agent_section(self) -> None:
        text = SECURITY_MD.read_text(encoding="utf-8")
        marker = _MARKER_RE.search(text)
        assert marker is not None
        heading_start = text.rfind("### ", 0, marker.start())
        next_heading = text.find("\n### ", marker.end())
        section = text[heading_start:next_heading if next_heading != -1 else len(text)]
        assert "plaintext" in section.lower(), "the section must call these files plaintext"
        assert "not encrypted" in section.lower(), "the section must say they are not encrypted"
        assert "0600" in section, "the section must state the owner-only mode"
        for name in PLAINTEXT_CREDENTIAL_FILES.values():
            assert name in section, f"SECURITY.md's plaintext section omits {name}"

    def test_security_md_says_what_reads_them(self) -> None:
        """The files are operative — the doc must not read like they are cache."""
        text = SECURITY_MD.read_text(encoding="utf-8")
        for reader in (
            "xpst/analytics.py",
            "_get_client_direct",
            "SessionManager",
        ):
            assert reader in text, f"SECURITY.md does not name {reader} as a reader"
        # The consequence a user actually needs: dotfiles/backups ship tokens.
        lowered = text.lower()
        assert "dotfile" in lowered
        assert "backup" in lowered

    def test_security_md_storage_table_has_a_plaintext_column(self) -> None:
        text = SECURITY_MD.read_text(encoding="utf-8")
        assert "Plaintext file also written" in text, (
            "the What-Is-Stored table must keep a column naming the plaintext mirror"
        )

    def test_best_practices_name_the_plaintext_files(self) -> None:
        text = SECURITY_MD.read_text(encoding="utf-8")
        start = text.index("## Security Best Practices")
        end = text.find("\n## ", start + 5)
        section = text[start:end if end != -1 else len(text)]
        for name in PLAINTEXT_CREDENTIAL_FILES.values():
            assert name in section, f"best practices do not mention {name}"

    def test_readme_names_every_plaintext_credential_file(self) -> None:
        text = README_MD.read_text(encoding="utf-8")
        for name in PLAINTEXT_CREDENTIAL_FILES.values():
            assert name in text, f"README.md never names the plaintext file {name}"
        assert "SECURITY.md" in text
        # The canonical statement lives in "## Security Practices"; naming the
        # files only in passing elsewhere would let that section go back to
        # claiming everything is encrypted.
        start = text.index("## Security Practices")
        end = text.find("\n## ", start + 5)
        section = text[start:end if end != -1 else len(text)]
        for name in PLAINTEXT_CREDENTIAL_FILES.values():
            assert name in section, (
                f"README.md's Security Practices section omits {name}, so it can "
                "again imply that every credential is encrypted"
            )
        assert "plaintext" in section.lower()

    @pytest.mark.parametrize(("doc", "claim"), _DISPROVEN_CLAIMS)
    def test_disproven_blanket_claims_are_gone(self, doc: str, claim: str) -> None:
        path = REPO_ROOT / doc
        assert claim not in path.read_text(encoding="utf-8"), (
            f"{doc} repeats a claim that was false while the plaintext mirrors exist: {claim!r}"
        )

    def test_privacy_md_names_the_plaintext_files(self) -> None:
        """docs/PRIVACY.md is the "full model" README links to — it must match."""
        text = PRIVACY_MD.read_text(encoding="utf-8")
        for name in PLAINTEXT_CREDENTIAL_FILES.values():
            assert name in text, f"docs/PRIVACY.md omits the plaintext file {name}"
        assert "plaintext" in text.lower()
        assert "0600" in text


class TestModeIsPinned:
    @pytest.mark.parametrize("filename", sorted(PLAINTEXT_CREDENTIAL_FILES.values()))
    @_POSIX_ONLY
    def test_documented_credential_file_is_written_0600(self, tmp_path, filename) -> None:
        """Every documented plaintext credential file is written owner-only."""
        path = tmp_path / "credentials" / filename
        write_text_0600(path, json.dumps({"synthetic": "value"}))
        assert _mode(path) == 0o600, f"{filename} was written {oct(_mode(path))}, not 0o600"

    @pytest.mark.parametrize("filename", sorted(PLAINTEXT_CREDENTIAL_FILES.values()))
    @_POSIX_ONLY
    def test_loosened_credential_file_is_tightened_on_rewrite(self, tmp_path, filename) -> None:
        """A backup restore that landed 0644 is repaired on the next refresh."""
        path = tmp_path / "credentials" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        path.chmod(0o644)
        write_text_0600(path, json.dumps({"synthetic": "value"}))
        assert _mode(path) == 0o600


class TestPlaintextIsReal:
    """The doc says these files hold the live credential in the clear."""

    @pytest.mark.asyncio
    @_POSIX_ONLY
    async def test_x_cookie_refresh_mirrors_plaintext_0600(self, tmp_path) -> None:
        from xpst.utils.sessions import SessionManager

        cookies = {"auth_token": "SYNTHETIC_auth_token", "ct0": "SYNTHETIC_ct0"}
        manager = SessionManager(config_dir=str(tmp_path))
        manager.credentials.store_json("x_cookies", cookies)

        cookies_path = tmp_path / "credentials" / "x_cookies.json"
        fake_client = MagicMock()
        fake_client.user = AsyncMock(return_value=MagicMock(screen_name="synthetic"))
        fake_client.get_cookies.return_value = cookies
        fake_twikit = MagicMock()
        fake_twikit.Client.return_value = fake_client

        with patch.dict(sys.modules, {"twikit": fake_twikit}):
            client = await manager.get_x_client(str(cookies_path))

        assert client is fake_client
        assert cookies_path.exists(), (
            "the X cookie refresh no longer mirrors to the plaintext file that "
            "SECURITY.md documents — update the docs and the registry together"
        )
        assert _mode(cookies_path) == 0o600
        on_disk = json.loads(cookies_path.read_text(encoding="utf-8"))
        assert on_disk == cookies, "the documented plaintext file is not plaintext JSON"

    @pytest.mark.asyncio
    async def test_encrypted_store_still_authenticates_without_the_plaintext_file(
        self, tmp_path
    ) -> None:
        """Documented precedence: the store is read first, the file is the fallback.

        Existing installs keep working if the plaintext sibling is removed, which
        is why the encrypted store — not the file — is authoritative for auth.
        """
        from xpst.utils.sessions import SessionManager

        cookies = {"auth_token": "SYNTHETIC_auth_token", "ct0": "SYNTHETIC_ct0"}
        manager = SessionManager(config_dir=str(tmp_path))
        manager.credentials.store_json("x_cookies", cookies)

        cookies_path = tmp_path / "credentials" / "x_cookies.json"
        assert not cookies_path.exists()

        fake_client = MagicMock()
        fake_client.user = AsyncMock(return_value=MagicMock(screen_name="synthetic"))
        fake_client.get_cookies.return_value = cookies
        fake_twikit = MagicMock()
        fake_twikit.Client.return_value = fake_client

        with patch.dict(sys.modules, {"twikit": fake_twikit}):
            await manager.get_x_client(str(cookies_path))

        fake_client.set_cookies.assert_called_once_with(cookies)

    def test_analytics_has_no_encrypted_store_fallback(self, tmp_path) -> None:
        """SECURITY.md says analytics reads only the plaintext file.

        That is the reason the file cannot simply be deleted, so it is pinned:
        an encrypted store entry alone must not satisfy the analytics path.
        """
        from xpst.analytics import AnalyticsCollector

        collector = AnalyticsCollector(config_dir=str(tmp_path))
        collector._config = {}

        creds = tmp_path / "credentials"
        creds.mkdir(parents=True, exist_ok=True)
        assert collector._youtube_token_path() is None

        # Now put the credential ONLY in the encrypted store.
        from xpst.utils.credentials import CredentialStore

        CredentialStore(str(tmp_path)).store("youtube_token", json.dumps({"token": "synthetic"}))
        assert (creds / "youtube_token.enc").exists()
        assert collector._youtube_token_path() is None, (
            "analytics now reads the encrypted store — SECURITY.md must stop saying "
            "the plaintext file is required (and the file may then be dropped)"
        )

        # The plaintext file is what the collector resolves.
        (creds / "youtube_token.json").write_text(
            json.dumps({"token": "synthetic"}), encoding="utf-8"
        )
        assert collector._youtube_token_path() == creds / "youtube_token.json"
