"""Persistent per-platform rate-limit cooldown state machine.

Hardening for the 10-01 Meta/IG rate-limit halt. Before this module the only
pause after a rate limit lived in a plain in-memory dict on the
:class:`~xpst.services.upload_service.UploadService` instance: a daemon
restart — which is exactly what happens after a halt, an update, or a crash —
forgot every active cooldown and the first upload after boot fired straight
back into the same wall.

The state machine
-----------------
One record per platform: a strike count and a ``until`` instant.

- **escalation** — each recorded hit doubles the pause along the ladder
  ``60s → 120s → 240s → ... → 24h`` (capped).
- **Retry-After honoured** — when the platform tells us how long to wait,
  the pause is at least that long (Meta's throttle errors and any 429 with a
  ``Retry-After`` are the case this exists for).
- **persistence** — the store survives restarts via an atomic JSON write
  under the config dir (``rate_cooldowns.json``).
- **shared Meta pool** — Instagram, Facebook, Threads and Messenger calls
  share Meta's app rate pool, so a throttle seen on one cools its siblings
  too. Cooling a sibling is conservative by design: the effect is "post
  later", never "post twice".
- **recovery** — a clean publish decays the strike count (half-life ~24h of
  clean publishes, 48h without any hit) so a platform that behaved for days
  returns to the base pause instead of escalating forever.

Dry-run note: :meth:`remaining_s` is pure and never sleeps, so scheduling can
be proven without a live API post (see
``tests/test_rate_limit_hardening_cooldown.py``).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from xpst.utils.atomic import write_text_atomic
from xpst.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

#: Platform families that share one upstream rate pool. A throttle seen on
#: any member cools every member for the same window.
_FAMILY_BY_PLATFORM = {
    "instagram": "meta",
    "facebook": "meta",
    "threads": "meta",
    "messenger": "meta",
}
_FAMILY_MEMBERS: dict[str, list[str]] = {}
for _platform, _family in _FAMILY_BY_PLATFORM.items():
    _FAMILY_MEMBERS.setdefault(_family, []).append(_platform)

#: Escalation ladder in seconds: 60s doubling to a 24h cap.
DEFAULT_ESCALATION_S: tuple[int, ...] = (60, 120, 240, 480, 960, 1920, 3840, 7680, 15360, 34560, 86400)

#: Strikes decay when the platform posts cleanly: a strike counts as stale
#: once this many *clean publishes* or this many seconds have passed since
#: the hit that raised it.
_STRIKE_DECAY_PUBLISHES = 24
_STRIKE_DECAY_S = 48 * 3600


@dataclass
class CooldownRecord:
    """Persisted per-platform cooldown state (the on-disk schema)."""

    strikes: int = 0
    until_iso: str = ""
    last_hit_iso: str = ""
    last_reason: str = ""
    clean_publishes_since_hit: int = 0


class CooldownStore:
    """Durable per-platform rate-limit cooldown state machine.

    Constructed once per process; safe to share between the engine's upload
    paths. Reads are pure (no sleep, no I/O beyond the one-time load); writes
    are atomic and owner-only.
    """

    def __init__(
        self,
        config_dir: str | Path | None = None,
        *,
        escalation_s: tuple[int, ...] = DEFAULT_ESCALATION_S,
        now: Callable[[], float] = time.time,
        persist: bool = True,
    ) -> None:
        if config_dir is None:
            from xpst.utils.platform import get_config_dir

            config_dir = get_config_dir()
        self._dir = Path(config_dir).expanduser()
        self._path = self._dir / "rate_cooldowns.json"
        self._escalation = escalation_s or DEFAULT_ESCALATION_S
        self._now = now
        self._persist = persist
        self._records: dict[str, CooldownRecord] = {}
        self._load()

    # ── queries (pure; dry-run safe) ──────────────────────────────────────

    def pause_for_strikes(self, strikes: int) -> int:
        """Ladder pause for a given (1-based) strike count."""
        idx = max(1, strikes) - 1
        return self._escalation[min(idx, len(self._escalation) - 1)]

    def family_for(self, platform: str) -> str:
        return _FAMILY_BY_PLATFORM.get(platform, platform)

    def remaining_s(self, platform: str) -> float:
        """Seconds until ``platform`` may post again (0 when it may post now).

        A Meta-family throttle cools every family member, so a sibling's
        cooldown counts here too.
        """
        family = self.family_for(platform)
        members = _FAMILY_MEMBERS.get(family, [platform])
        deadline = 0.0
        now = self._now()
        for member in members:
            record = self._records.get(member)
            if record is None or not record.until_iso:
                continue
            try:
                until = datetime.fromisoformat(record.until_iso).timestamp()
            except ValueError:
                continue
            if until > deadline:
                deadline = until
        return max(0.0, deadline - now)

    def is_cooling(self, platform: str) -> bool:
        return self.remaining_s(platform) > 0

    def snapshot(self) -> dict[str, dict]:
        """Machine-readable view of every non-idle record (health/doctor)."""
        out: dict[str, dict] = {}
        for platform, record in sorted(self._records.items()):
            remaining = self.remaining_s(platform)
            if record.strikes == 0 and remaining <= 0:
                continue
            out[platform] = {**asdict(record), "remaining_s": round(remaining, 1)}
        return out

    # ── transitions ───────────────────────────────────────────────────────

    def record_hit(
        self,
        platform: str,
        *,
        retry_after_s: float | None = None,
        reason: str = "",
    ) -> int:
        """Record a rate-limit hit; returns the pause applied (seconds).

        Strikes escalate the ladder; ``retry_after_s`` raises the pause to at
        least the value the platform itself asked for (capped at the ladder
        ceiling). The whole Meta family cools for the resulting window.
        """
        record = self._records.get(platform)
        if record is None:
            record = CooldownRecord()
            self._records[platform] = record

        # Drop stale strikes before escalating (see module docstring).
        now = self._now()
        if record.last_hit_iso:
            try:
                age = now - datetime.fromisoformat(record.last_hit_iso).timestamp()
            except ValueError:
                age = 0.0
            if age > _STRIKE_DECAY_S:
                record.strikes = 0
                record.clean_publishes_since_hit = 0
        strikes = record.strikes + 1
        pause = self.pause_for_strikes(strikes)
        cap = self._escalation[-1]
        if retry_after_s and retry_after_s > pause:
            pause = min(int(retry_after_s), cap)

        record.strikes = strikes
        record.until_iso = datetime.fromtimestamp(now + pause).isoformat()
        record.last_hit_iso = datetime.fromtimestamp(now).isoformat()
        record.last_reason = reason[:300]
        record.clean_publishes_since_hit = 0

        # Cool the whole upstream family: Meta counts IG/FB/Threads/Messenger
        # against one app pool, and cooling a sibling is always the safe side.
        family = self.family_for(platform)
        for member in _FAMILY_MEMBERS.get(family, []):
            if member == platform:
                continue
            sibling = self._records.setdefault(member, CooldownRecord())
            sibling.until_iso = record.until_iso
            sibling.last_reason = f"family cooldown via {platform}: {reason[:200]}"

        self._save()
        logger.warning(
            "Rate-limit cooldown: %s paused %ss (strike %d, reason: %s)",
            platform,
            pause,
            strikes,
            reason[:200] or "unknown",
        )
        return pause

    def note_success(self, platform: str) -> None:
        """A clean publish decayed the strike count.

        Strikes decay gradually rather than resetting to zero so a platform
        that flickers back into throttling the next day does not get a free
        base-pause pass.
        """
        record = self._records.get(platform)
        if record is None or record.strikes == 0:
            return
        record.clean_publishes_since_hit += 1
        if record.clean_publishes_since_hit >= _STRIKE_DECAY_PUBLISHES:
            record.strikes = max(0, record.strikes - 1)
            record.clean_publishes_since_hit = 0
            self._save()

    # ── persistence ───────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._persist:
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load %s (%s); starting with no cooldowns", self._path, exc)
            return
        if not isinstance(raw, dict):
            return
        known = {f for f in CooldownRecord.__dataclass_fields__}
        for platform, data in raw.items():
            if isinstance(data, dict):
                self._records[str(platform)] = CooldownRecord(
                    **{k: v for k, v in data.items() if k in known}
                )

    def _save(self) -> None:
        if not self._persist:
            return
        payload = {p: asdict(r) for p, r in self._records.items()}
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            write_text_atomic(self._path, json.dumps(payload, indent=2))
        except OSError as exc:
            # A failed persistence write degrades to in-process cooling only;
            # it must never block or fail an upload path.
            logger.warning("Could not persist rate cooldowns to %s: %s", self._path, exc)
