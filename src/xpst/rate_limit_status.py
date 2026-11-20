"""Read-only rate-limit cooldown visibility for operator-facing surfaces.

The durable cooldown state machine added in the 10-01 rate-limit hardening
(:mod:`xpst.utils.cooldown_store`) persists an ``until`` instant per platform
so a restart cannot forget that a platform is still cooling. Its
``snapshot()`` docstring calls itself the "health/doctor" view, but until this
module nothing rendered it: an operator could only discover an active cooldown
by opening ``rate_cooldowns.json`` by hand, and an agent had no way to ask
"why is nothing posting?" without guessing.

This module is that single view. The CLI (``xpst health``, ``xpst doctor``)
and the MCP tool ``xpst_rate_limit_status`` all call
:func:`cooldown_report`, so every surface answers with the same data instead
of each re-deriving the file format.

Read-only by construction: the report loads the state file once, never writes
it, never sleeps, and never calls a platform API. That keeps it dry-run safe
and cheap enough to sit inside a health check. A missing or unreadable file is
reported as idle/unknown rather than raised — an operator asking for health
must never get a traceback because a cooldown file is corrupt.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xpst.utils.cooldown_store import CooldownStore

if TYPE_CHECKING:
    from collections.abc import Callable

#: File name the cooldown store persists under the config dir.
COOLDOWN_FILENAME = "rate_cooldowns.json"

#: One sentence every surface can repeat verbatim: a cooldown delays a post,
#: it never loses one.
COOLDOWN_SEMANTICS = (
    "A cooldown defers posts to the affected platform; it does not fail them. "
    "xPST resumes automatically when the pause expires."
)


def format_duration(seconds: float) -> str:
    """Compact human duration (``3m 20s``) for an operator-facing line."""
    total = int(round(max(0.0, seconds)))
    if total >= 86400:
        days, rest = divmod(total, 86400)
        return f"{days}d {rest // 3600}h"
    if total >= 3600:
        hours, rest = divmod(total, 3600)
        return f"{hours}h {rest // 60}m"
    if total >= 60:
        minutes, rest = divmod(total, 60)
        return f"{minutes}m {rest}s"
    return f"{total}s"


def cooldown_store_path(config: Any = None) -> Path | None:
    """Where the cooldown state file lives for ``config`` (no I/O)."""
    candidate = getattr(config, "config_dir", None)
    config_dir: Path | None = None
    if isinstance(candidate, (str, Path)) and str(candidate):
        config_dir = Path(candidate).expanduser()
    else:  # mirror the uploader's fallback so both read the same file
        try:
            from xpst.utils.platform import get_config_dir

            config_dir = Path(get_config_dir()).expanduser()
        except Exception:  # noqa: BLE001 - reporting must not fail on import/env
            return None
    return config_dir / COOLDOWN_FILENAME


def cooldown_report(
    config: Any = None,
    *,
    now: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Machine-readable cooldown state for health/doctor/MCP.

    Returns a dict with:

    ``state``        ``"cooling"`` when any platform is paused, ``"idle"``
                     when the store has no active pause, ``"unknown"`` when no
                     config directory could be resolved.
    ``active``       platforms whose pause has not expired, sorted.
    ``cooldowns``    per-platform records (strikes, ``until_iso``,
                     ``last_hit_iso``, ``last_reason``, remaining seconds).
    ``source``       the state file that was read (``None`` when unresolved).
    ``file_present`` whether that file exists — an absent file is a normal
                     fresh install, not an error.
    ``note``         the fixed deferral-not-failure sentence.
    """
    path = cooldown_store_path(config)
    if path is None:
        return {
            "state": "unknown",
            "active": [],
            "cooldowns": {},
            "source": None,
            "file_present": False,
            "note": COOLDOWN_SEMANTICS,
        }

    file_present = path.is_file()
    # Constructing the store performs the single read this report needs. It
    # never writes (only transitions call _save) and never touches the network.
    kwargs: dict[str, Any] = {}
    if now is not None:
        kwargs["now"] = now
    store = CooldownStore(path.parent, **kwargs)
    snapshot = store.snapshot()

    cooldowns: dict[str, dict[str, Any]] = {}
    for platform, record in snapshot.items():
        remaining = float(record.get("remaining_s") or 0.0)
        cooldowns[platform] = {
            **record,
            "remaining_human": format_duration(remaining),
            "until_display": _display_time(record.get("until_iso")),
        }

    active = sorted(p for p, rec in cooldowns.items() if (rec.get("remaining_s") or 0) > 0)
    return {
        "state": "cooling" if active else "idle",
        "active": active,
        "cooldowns": cooldowns,
        "source": str(path),
        "file_present": file_present,
        "note": COOLDOWN_SEMANTICS,
    }


def _display_time(value: str | None) -> str | None:
    """Local ``HH:MM:SS`` rendering of a stored ISO instant (best effort)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value
