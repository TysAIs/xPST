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
