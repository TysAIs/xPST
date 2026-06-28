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
