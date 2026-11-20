"""Atomic file replacement that survives transient Windows file locks.

``os.replace`` is atomic on POSIX, but on Windows the destination can be briefly
locked by another process or a scanner, surfacing as
``PermissionError: [WinError 5]``. Every writer in xPST that publishes state via
a temp file + rename must retry that specific error a bounded number of times
instead of crashing or losing the write.
"""

from __future__ import annotations

import os
import time
from os import PathLike
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_REPLACE_BACKOFF_S: tuple[float, ...] = (0.05, 0.1, 0.2)


def replace_with_retry(
    src: str | PathLike[str],
    dst: str | PathLike[str],
    *,
    backoff: tuple[float, ...] = DEFAULT_REPLACE_BACKOFF_S,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """``os.replace`` with bounded retries for a transient permission lock.

    Raises the final ``PermissionError`` when the destination stays locked, so a
    caller that cannot publish still fails loudly instead of silently dropping
    the write.
    """
    for attempt in range(len(backoff) + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt >= len(backoff):
                raise
            sleep(backoff[attempt])


def write_text_atomic(
    path: str | PathLike[str],
    text: str,
    *,
    mode: int = 0o600,
) -> None:
    """Write ``text`` to ``path`` atomically, owner-only by default.

    Creates a sibling temp file with ``mode`` (0600 by default — every xPST
    file that carries tokens or account details must not be world-readable),
    fsyncs it, then publishes it via :func:`replace_with_retry`.  A failure
    part-way through leaves the previous file untouched and removes the temp
    file, so a crash, a full disk or a transient Windows lock can never
    truncate a user's config.

    Raises:
        OSError: When the write or the rename genuinely fails (caller decides
            whether that is fatal).
    """
    path = Path(path)
    tmp_path = path.parent / f".{path.name}.tmp.{os.getpid()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp_path, flags, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    try:
        os.chmod(path, mode)
    except OSError:
        pass
