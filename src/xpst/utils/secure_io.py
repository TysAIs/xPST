"""Owner-only secure file writes for credential material.

Credential, token, session, and cookie files written under ``~/.xpst`` must
never be world- or group-readable. This module centralizes the write so every
call site applies the same ``0600`` (owner read/write only) policy.

The implementation mirrors ``CredentialStore._write_secret_file``: the file is
created via ``os.open`` with mode ``0o600`` so POSIX platforms get correct
permissions from creation, and ``os.chmod`` is applied afterwards for defence in
depth and to tighten any pre-existing looser file. ``chmod`` failures are
swallowed so Windows (where POSIX mode bits are largely a no-op) does not raise.
"""

import os
import stat
from pathlib import Path

_OWNER_RW = stat.S_IRUSR | stat.S_IWUSR  # 0o600

#: Credential files under ``~/.xpst/credentials`` that xPST writes in PLAINTEXT.
#:
#: These are not an oversight and not a cache. google-auth, instagrapi and twikit
#: parse their credentials from a JSON document, and the analytics collectors in
#: ``xpst/analytics.py`` build their own clients from these files with no
#: encrypted-store fallback — so each one holds the LIVE credential in the clear
#: and the runtime genuinely requires it to exist.
#:
#: The contract this table encodes:
#:   * every file here is written through :func:`write_text_0600` (never a bare
#:     ``open(..., "w")``, which lands at the process umask);
#:   * the credentials directory itself is 0700;
#:   * ``SECURITY.md`` lists exactly this set, above the
#:     ``<!-- xpst-plaintext-credentials: ... -->`` marker, and says plainly that
#:     encryption at rest does not cover them.
#:
#: ``tests/test_plaintext_credential_contract.py`` fails if the documentation,
#: this table, or the source diverge — and it fails if a *new* plaintext
#: credential file appears in the tree without being documented, so the
#: "credentials are encrypted at rest" claim cannot silently become a lie again.
PLAINTEXT_CREDENTIAL_FILES: dict[str, str] = {
    "youtube": "youtube_token.json",
    "x": "x_cookies.json",
    "instagram": "instagram_session.json",
}


def write_text_0600(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` with owner-only (0600) permissions.

    Any parent directories are created as needed. Existing files are truncated.

    Args:
        path: Destination file path (already expanded).
        text: Text content to write.
        encoding: Text encoding (default UTF-8).

    Cross-platform: on POSIX the file is created 0600 and re-chmod'd; on
    Windows the ``chmod`` is best-effort and never raises.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, _OWNER_RW)
    try:
        os.write(fd, text.encode(encoding))
    finally:
        os.close(fd)
    try:
        os.chmod(path, _OWNER_RW)
    except OSError:
        pass
