"""Path confinement for file paths that came from state, config or an API/MCP argument.

xPST takes file paths from three untrusted-ish directions:

* MCP tool arguments (``xpst_post.video_path``, ``xpst_schedule_add.video_path``)
  — an agent or an LLM reading untrusted content chooses these;
* persisted state / a schedule entry written by an earlier run;
* HTTP query/body parameters.

None of those may read or write outside the media roots the user actually
granted. :func:`confine_path` resolves the candidate (following symlinks) and
requires the result to live inside one of the allowed roots, so ``../../etc/passwd``,
absolute escapes and symlink escapes are all rejected.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from xpst.utils.logger import get_logger

logger = get_logger(__name__)


class PathConfinementError(ValueError):
    """Raised when a path escapes every allowed root."""


def _temp_root() -> Path:
    """The system temp directory, resolved for THIS call.

    ``tempfile.gettempdir()`` caches its answer for the process lifetime and
    only consults the POSIX ``TMPDIR`` variable, so it is the wrong probe here:

    * Windows sets ``TEMP``/``TMP`` (never ``TMPDIR``), so a ``TMPDIR``-only
      read confines every legitimate ``%TEMP%\\...`` path — the media cache,
      the staging dir, and pytest's ``tmp_path`` — out of its own temp root.
    * A cached value ignores a ``TMPDIR`` set later in the process, which
      callers (external drives, test isolation) legitimately use.

    Read the environment the way the platform and the caller actually set it,
    newest override first, and fall back to the stdlib resolution.
    """
    for name in ("TMPDIR", "TEMP", "TMP"):
        value = os.environ.get(name)
        if value and value.strip():
            return Path(value.strip())
    return Path(tempfile.gettempdir())


def default_media_roots(config_dir: str | os.PathLike[str] = "~/.xpst") -> tuple[Path, ...]:
    """Roots a user-granted media path may live in by default.

    The config dir (xPST's own workspace, which holds ``library/`` and the
    media cache), the user's home Movies/Videos/Desktop/Downloads/Pictures
    folders, and the system temp dir (:func:`_temp_root`, i.e. ``TMPDIR`` on
    POSIX and ``TEMP``/``TMP`` on Windows). Anything else requires an explicit
    override — either the ``XPST_MEDIA_ROOTS`` environment variable
    (``os.pathsep``-separated, for an external drive) or an ``extra_roots``
    argument at the call site.
    """
    home = Path.home()
    roots: list[Path] = [Path(config_dir).expanduser()]
    for name in ("Movies", "Videos", "Desktop", "Downloads", "Pictures"):
        roots.append(home / name)
    roots.append(_temp_root())  # nosec B108 - an allowed root, not a temp file
    extra = os.environ.get("XPST_MEDIA_ROOTS", "")
    for part in extra.split(os.pathsep):
        if part.strip():
            roots.append(Path(part.strip()).expanduser())
    return tuple(roots)


def confine_path(
    candidate: str | os.PathLike[str],
    roots: tuple[Path, ...] | list[Path],
    *,
    must_exist: bool = False,
) -> Path:
    """Resolve ``candidate`` and require it to live under one of ``roots``.

    Args:
        candidate: The untrusted path.
        roots: Allowed root directories.
        must_exist: When ``True``, also require the resolved path to exist.

    Returns:
        The resolved absolute :class:`Path`.

    Raises:
        PathConfinementError: if the path is empty, contains a NUL byte,
            resolves outside every root, or does not exist when required.
    """
    if candidate is None:
        raise PathConfinementError("Path is required")
    raw = os.fspath(candidate)
    if not isinstance(raw, str) or not raw.strip():
        raise PathConfinementError("Path is empty")
    if "\x00" in raw:
        raise PathConfinementError("Path contains a NUL byte")

    resolved = Path(raw).expanduser().resolve(strict=False)

    allowed: list[Path] = []
    for root in roots:
        try:
            allowed.append(Path(root).expanduser().resolve(strict=False))
        except OSError:  # pragma: no cover - defensive
            continue

    if not any(resolved == root or resolved.is_relative_to(root) for root in allowed):
        logger.warning("Refused path outside the allowed roots: %s", resolved)
        raise PathConfinementError("Path resolves outside every allowed root")

    if must_exist and not resolved.exists():
        raise PathConfinementError("Path does not exist")

    return resolved


def confine_media_path(
    candidate: str | os.PathLike[str],
    config_dir: str | os.PathLike[str] = "~/.xpst",
    *,
    extra_roots: tuple[Path, ...] | list[Path] = (),
    must_exist: bool = True,
) -> Path:
    """Convenience wrapper over :func:`confine_path` for media arguments."""
    roots = (*default_media_roots(config_dir), *extra_roots)
    return confine_path(candidate, roots, must_exist=must_exist)
