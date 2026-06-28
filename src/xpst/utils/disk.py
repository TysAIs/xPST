"""Disk space utilities for xPST.

Checks available disk space before downloading large video files to
prevent disk-full errors that could corrupt state files or leave
partial downloads.
"""

import shutil
from pathlib import Path

from xpst.utils.logger import get_logger

logger = get_logger(__name__)

# Minimum free space required (500 MB)
MIN_FREE_SPACE_MB = 500

# Warning threshold (1 GB)
WARN_FREE_SPACE_MB = 1024


class DiskSpaceError(Exception):
    """Raised when there is not enough disk space to proceed."""


def check_disk_space(path: str | Path, min_mb: int = MIN_FREE_SPACE_MB) -> bool:
    """Check if there is enough free disk space at the given path.

    Args:
        path: Directory or file path to check (checks parent if file).
        min_mb: Minimum required free space in megabytes.

    Returns:
        True if sufficient space is available.

    Raises:
        DiskSpaceError: If free space is below min_mb.
    """
    path = Path(path).expanduser()
    if path.is_file():
        path = path.parent

    try:
        usage = shutil.disk_usage(str(path))
        free_mb = usage.free / (1024 * 1024)

        if free_mb < min_mb:
            raise DiskSpaceError(
                f"Insufficient disk space: {free_mb:.0f} MB free, "
                f"{min_mb} MB required at {path}"
            )

        if free_mb < WARN_FREE_SPACE_MB:
            logger.warning(
                "Low disk space: %.0f MB free at %s", free_mb, path
            )

        return True

    except OSError as e:
        logger.warning("Could not check disk space at %s: %s", path, e)
        return True  # Don't block on check failure


def get_free_space_mb(path: str | Path) -> float:
    """Get free disk space in megabytes.

    Args:
        path: Directory or file path to check.

    Returns:
        Free space in MB, or -1 if check fails.
    """
    path = Path(path).expanduser()
    if path.is_file():
        path = path.parent

    try:
        usage = shutil.disk_usage(str(path))
        return usage.free / (1024 * 1024)
    except OSError:
        return -1
