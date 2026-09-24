"""Durable local compose drafts, with plan invalidation.

Two defects this module closes:

1. **A half-written post died with the page.** The compose screen kept the
   chosen media, caption, and destinations in component state only, so
   navigating away — or restarting the app — lost the work.
2. **A stale plan could be posted later as if it were current.** A preflight
   verdict computed before a token went missing, a destination was disabled, or
   a media file disappeared was still postable, because nothing recorded *what
   the plan had been computed against*.

Design rules:

* **Local and side-effect free.** Drafts live in ``<config_dir>/drafts.json``
  (mode ``0600``). Nothing here uploads, refreshes a token, records quota, or
  touches the network.
* **No secrets.** Only whitelisted content fields are persisted: media paths,
  caption, destination names, and the canonical preflight plan. Credential
  material is never copied into a draft.
* **Facts, not vibes.** A validation stamp stores the local facts a verdict
  depended on (media existence/size/mtime, per-destination enabled flag and
  local auth readiness) plus a fingerprint over them. Revalidation recomputes
  the facts and diffs them, so *stale* always arrives with a reason a human can
  read and act on.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xpst.services.post_preflight import local_auth_readiness

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from xpst.config import XPSTConfig

logger = logging.getLogger(__name__)

# ── Limits and vocabulary ────────────────────────────────────────────────

DRAFT_FILE_NAME = "drafts.json"
DRAFT_STORE_VERSION = 1

#: Hard cap on stored drafts. Oldest drafts are pruned; posted drafts first.
MAX_DRAFTS = 25

#: A caption longer than this is refused rather than written to disk (the CLI
#: already fails fast at 1 MB; a draft file must stay small and readable).
MAX_CAPTION_CHARS = 20_000

STATUS_DRAFT = "draft"
STATUS_PLANNED = "planned"
STATUS_POSTED = "posted"

# Reason codes are stable contract: clients may branch on them, humans read the
# ``message``. Never add a code without a message that says what to do next.
CODE_CONTENT_CHANGED = "CONTENT_CHANGED"
CODE_MEDIA_MISSING = "MEDIA_MISSING"
CODE_MEDIA_CHANGED = "MEDIA_CHANGED"
CODE_MEDIA_AVAILABLE = "MEDIA_AVAILABLE"
CODE_DESTINATION_DISABLED = "DESTINATION_DISABLED"
CODE_DESTINATION_ENABLED = "DESTINATION_ENABLED"
CODE_DESTINATION_NOT_READY = "DESTINATION_NOT_READY"
CODE_DESTINATION_READY = "DESTINATION_READY"
CODE_AUTH_CHANGED = "AUTH_CHANGED"

# ── Fact collection and fingerprints ─────────────────────────────────────


def media_fact_key(raw: str) -> str:
    """Absolute key a media path is tracked under (stable across restarts)."""
    return os.path.abspath(os.path.expanduser(str(raw)))


def normalize_platforms(platforms: Iterable[str] | None) -> list[str]:
    """Lower-case destination names, de-duplicated in request order."""
    seen: dict[str, None] = {}
    for item in platforms or []:
        name = str(item).strip().lower()
        if name:
            seen.setdefault(name, None)
    return list(seen)


def normalize_media_paths(paths: Iterable[str] | None) -> list[str]:
    """Non-empty media path strings, in request order."""
    return [str(item).strip() for item in (paths or []) if str(item).strip()]


def _media_facts(raw: str) -> dict[str, Any]:
    """Existence/size/mtime for one media path. Never raises."""
    path = Path(media_fact_key(raw))
    exists = False
    size: int | None = None
    mtime_ns: int | None = None
    try:
        if path.is_file():
            stat = path.stat()
            exists = True
            size = int(stat.st_size)
            mtime_ns = int(stat.st_mtime_ns)
    except OSError:  # pragma: no cover - unreadable file is "not usable"
        exists = False
    return {"exists": exists, "size": size, "mtime_ns": mtime_ns}


def _destination_facts(platform: str, config: Any) -> dict[str, Any]:
    """Local, network-free readiness facts for one destination."""
    account = getattr(config, platform, None)
    enabled = bool(account is not None and getattr(account, "enabled", False))
    auth_ready = False
    auth_status = "unknown"
    auth_mode = "unknown"
    try:
        auth = local_auth_readiness(platform, config)
        auth_ready = bool(auth.ready)
        auth_status = str(auth.status)
        auth_mode = str(auth.auth_mode)
    except Exception as exc:  # noqa: BLE001 - an unknown platform is "not ready"
        logger.debug("Auth readiness for %s unavailable: %s", platform, exc)
    return {
        "enabled": enabled,
        "auth_ready": auth_ready,
        "auth_status": auth_status,
        "auth_mode": auth_mode,
    }


def collect_facts(
    media_paths: Sequence[str],
    platforms: Sequence[str],
    config: Any,
) -> dict[str, Any]:
    """Snapshot the local facts a post plan depends on.

    Only local state is read: file metadata plus the same local auth readiness
    rule the canonical preflight uses. No network, no credential decryption.
    """
    media: dict[str, Any] = {}
    for raw in media_paths:
        key = media_fact_key(raw)
        if key in media:
            continue
        media[key] = _media_facts(raw)
    destinations: dict[str, Any] = {}
    for platform in normalize_platforms(platforms):
        destinations[platform] = _destination_facts(platform, config)
    return {"media": media, "destinations": destinations}


def fingerprint_facts(facts: Any) -> str:
    """Deterministic sha256 over a facts snapshot (order-insensitive)."""
    canonical = json.dumps(facts or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def content_hash(media_paths: Sequence[str], caption: str, platforms: Sequence[str]) -> str:
    """Deterministic hash of the *content* a plan was made for."""
    payload = {
        "media": [media_fact_key(item) for item in normalize_media_paths(media_paths)],
        "caption": str(caption or ""),
        "platforms": normalize_platforms(platforms),
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def stamp(media_paths: Sequence[str], caption: str, platforms: Sequence[str], config: Any) -> dict[str, Any]:
    """Build a validation stamp: what a verdict was validated against."""
    facts = collect_facts(media_paths, platforms, config)
    return {
        "captured_at": _now_iso(),
        "content_hash": content_hash(media_paths, caption, platforms),
        "fingerprint": fingerprint_facts(facts),
        "facts": facts,
    }


def explain_facts(previous: Any, current: Any) -> list[dict[str, Any]]:
    """Human-readable reasons the local facts moved between two snapshots.

    Ordering is deterministic (media paths, then destinations, alphabetically)
    so two surfaces reporting the same change produce the same list.
    """
    reasons: list[dict[str, Any]] = []
    previous = previous or {}
    current = current or {}

    prev_media = previous.get("media") or {}
    cur_media = current.get("media") or {}
    for path in sorted(set(prev_media) | set(cur_media)):
        before = prev_media.get(path) or {}
        after = cur_media.get(path) or {}
        if before == after:
            continue
        if after.get("exists") is False and before.get("exists") is not False:
            reasons.append(
                {
                    "code": CODE_MEDIA_MISSING,
                    "message": f"The media file {path} is no longer on disk.",
                    "subject": path,
                }
            )
        elif before.get("exists") is False and after.get("exists") is True:
            reasons.append(
                {
                    "code": CODE_MEDIA_AVAILABLE,
                    "message": f"The media file {path} is on disk again.",
                    "subject": path,
                }
            )
        elif after.get("exists") is False:
            # Never available in either snapshot: the preflight covers that, not
            # the staleness diff. Staying silent here avoids a duplicate reason.
            continue
        else:
            reasons.append(
                {
                    "code": CODE_MEDIA_CHANGED,
                    "message": f"The media file {path} changed since this plan was made.",
                    "subject": path,
                }
            )

    prev_dest = previous.get("destinations") or {}
    cur_dest = current.get("destinations") or {}
    for platform in sorted(set(prev_dest) | set(cur_dest)):
        before = prev_dest.get(platform) or {}
        after = cur_dest.get(platform) or {}
        if before == after:
            continue
        if before.get("enabled") is True and after.get("enabled") is False:
            reasons.append(
                {
                    "code": CODE_DESTINATION_DISABLED,
                    "message": f"{platform} was disabled since this plan was made.",
                    "subject": platform,
                }
            )
        elif before.get("enabled") is False and after.get("enabled") is True:
            reasons.append(
                {
                    "code": CODE_DESTINATION_ENABLED,
                    "message": f"{platform} was enabled since this plan was made.",
                    "subject": platform,
                }
            )
        if before.get("auth_ready") and not after.get("auth_ready"):
            reasons.append(
                {
                    "code": CODE_DESTINATION_NOT_READY,
                    "message": (
                        f"{platform} can no longer publish locally ({after.get('auth_status', 'unknown')}). "
                        "Sign in again before posting."
                    ),
                    "subject": platform,
                }
            )
        elif after.get("auth_ready") and not before.get("auth_ready"):
            reasons.append(
                {
                    "code": CODE_DESTINATION_READY,
                    "message": f"{platform} is ready now (it was not when this plan was made).",
                    "subject": platform,
                }
            )
        if before.get("auth_mode") != after.get("auth_mode"):
            reasons.append(
                {
                    "code": CODE_AUTH_CHANGED,
                    "message": (
                        f"The way xPST signs in to {platform} changed "
                        f"({before.get('auth_mode', 'unknown')} → {after.get('auth_mode', 'unknown')})."
                    ),
                    "subject": platform,
                }
            )
    return reasons


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def has_content(media_paths: Sequence[str], caption: str, platforms: Sequence[str]) -> bool:
    """True when a draft holds anything worth persisting."""
    return bool(normalize_media_paths(media_paths) or str(caption or "") or normalize_platforms(platforms))


# ── Persistence ──────────────────────────────────────────────────────────

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


class DraftStore:
    """File-backed draft storage: ``<config_dir>/drafts.json``, mode 0600.

    One process-local lock per file serialises writers in the API process, and
    every write is an atomic replace of a fully-flushed temporary file, so a
    crash can never leave a half-written draft behind.
    """

    def __init__(self, config_dir: str | Path = "~/.xpst") -> None:
        self.config_dir: Path = Path(str(config_dir)).expanduser()

    @property
    def path(self) -> Path:
        return self.config_dir / DRAFT_FILE_NAME

    # ── reading ──────────────────────────────────────────────────────────

    def load(self) -> list[dict[str, Any]]:
        """All stored drafts, newest first. A broken file reads as empty."""
        lock = _lock_for(self.path)
        with lock:
            return self._load_unlocked()

    def _load_unlocked(self) -> list[dict[str, Any]]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("Could not read %s: %s", self.path, exc)
            return []
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as exc:
            # Never destroy the evidence, never crash the UI: keep the corrupt
            # file aside and start clean.
            logger.warning("Draft store %s is not valid JSON: %s", self.path, exc)
            self._quarantine_corrupt()
            return []
        items = payload.get("drafts") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return []
        drafts = [item for item in items if isinstance(item, dict) and item.get("id")]
        drafts.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return drafts

    def _quarantine_corrupt(self) -> None:
        try:
            target = self.path.with_suffix(f".json.corrupt-{int(datetime.now(timezone.utc).timestamp())}")
            os.replace(self.path, target)
            os.chmod(target, 0o600)
        except OSError as exc:  # pragma: no cover - best effort
            logger.debug("Could not quarantine %s: %s", self.path, exc)

    def get(self, draft_id: str) -> dict[str, Any] | None:
        wanted = str(draft_id or "").strip()
        if not wanted:
            return None
        for draft in self.load():
            if draft.get("id") == wanted:
                return draft
        return None

    # ── writing ──────────────────────────────────────────────────────────

    def write_all(self, drafts: Sequence[dict[str, Any]]) -> None:
        """Atomically replace the store with ``drafts`` (pruned, newest first)."""
        lock = _lock_for(self.path)
        with lock:
            self._write_all_unlocked(drafts)

    def _write_all_unlocked(self, drafts: Sequence[dict[str, Any]]) -> None:
        ordered = sorted(
            (dict(item) for item in drafts if item.get("id")),
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )[:MAX_DRAFTS]
        payload = {"version": DRAFT_STORE_VERSION, "drafts": ordered}
        self.config_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".json.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.error("Could not write %s: %s", self.path, exc)
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:  # pragma: no cover - best effort
                    pass
            raise

    def upsert(self, draft: dict[str, Any]) -> dict[str, Any]:
        """Insert or replace one draft, returning the stored record."""
        record = dict(draft)
        record["updated_at"] = _now_iso()
        lock = _lock_for(self.path)
        with lock:
            existing = self._load_unlocked()
            kept = [item for item in existing if item.get("id") != record.get("id")]
            kept.append(record)
            self._write_all_unlocked(kept)
        return record

    def delete(self, draft_id: str) -> bool:
        """Remove one draft. Returns False when it was not stored."""
        wanted = str(draft_id or "").strip()
        if not wanted:
            return False
        lock = _lock_for(self.path)
        with lock:
            existing = self._load_unlocked()
            kept = [item for item in existing if item.get("id") != wanted]
            if len(kept) == len(existing):
                return False
            self._write_all_unlocked(kept)
        return True


# ── Service: validate, plan, gate ────────────────────────────────────────


def new_draft_id() -> str:
    """Opaque, collision-free draft id (never derived from content)."""
    return f"draft_{uuid.uuid4().hex[:16]}"


class DraftService:
    """Save, revalidate, and gate posting for compose drafts of one config dir."""

    def __init__(
        self,
        config: XPSTConfig | Any,
        config_dir: str | None = None,
        store: DraftStore | None = None,
    ) -> None:
        self.config = config
        self.config_dir = str(config_dir or getattr(config, "config_dir", "") or "~/.xpst")
        self.store = store or DraftStore(self.config_dir)

    # ── writing drafts ───────────────────────────────────────────────────

    def save(
        self,
        *,
        draft_id: str = "",
        media_paths: Sequence[str] | None = None,
        caption: str = "",
        platforms: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Create or update a draft and stamp it against the current facts.

        Returns ``{"draft": <record>, "verdict": <verdict>}``. The stamp records
        what this draft was validated against; a previously recorded plan is
        preserved and re-evaluated (so editing the caption makes the plan stale
        rather than silently discarding the evidence).
        """
        text = str(caption or "")
        if len(text) > MAX_CAPTION_CHARS:
            raise ValueError(f"Caption is {len(text)} characters; the draft limit is {MAX_CAPTION_CHARS}.")

        wanted = str(draft_id or "").strip()
        existing = self.store.get(wanted) if wanted else None
        recreated = bool(wanted) and existing is None
        record = existing or {
            "id": wanted or new_draft_id(),
            "created_at": _now_iso(),
            "version": DRAFT_STORE_VERSION,
            "plan": None,
            "posted_at": None,
            "video_id": "",
            "reconfirmations": 0,
        }

        media = normalize_media_paths(media_paths)
        targets = normalize_platforms(platforms)
        record.update(
            {
                "media_paths": media,
                "caption": text,
                "platforms": targets,
                "validation": stamp(media, text, targets, self.config),
                "status": STATUS_DRAFT if record.get("status") in (None, "", STATUS_POSTED) else record.get("status"),
            }
        )
        stored = self.store.upsert(record)
        return {"draft": stored, "verdict": self.verdict_for(stored), "recreated": recreated}

    def sync_from_request(
        self,
        draft_id: str,
        *,
        media_paths: Sequence[str] | None,
        caption: str,
        platforms: Sequence[str] | None,
    ) -> dict[str, Any]:
        """Bring a stored draft up to date with what is about to be posted.

        The post path calls this before gating so a caller cannot post one
        content while holding a plan stamped for another. Content that moved
        after the plan is *recorded as a change*, which is exactly what makes
        the plan stale.
        """
        return self.save(draft_id=draft_id, media_paths=media_paths, caption=caption, platforms=platforms)

    def delete(self, draft_id: str) -> bool:
        return self.store.delete(draft_id)

    def get(self, draft_id: str) -> dict[str, Any] | None:
        return self.store.get(draft_id)

    def mark_posted(self, draft_id: str, *, video_id: str = "") -> dict[str, Any] | None:
        """Close a draft out after a post actually ran."""
        record = self.store.get(draft_id)
        if record is None:
            return None
        record["status"] = STATUS_POSTED
        record["posted_at"] = _now_iso()
        record["video_id"] = str(video_id or "")
        return self.store.upsert(record)

    def record_confirmation(self, draft_id: str) -> dict[str, Any] | None:
        """Record that a human accepted a stale plan and re-stamped the draft.

        Re-confirmation is an acceptance of the *current* state, so the recorded
        plan and the draft's validation stamp are both re-stamped against it —
        the acceptance is then durable and auditable (``reconfirmations``), and
        the draft stops reporting itself stale until something changes again.
        """
        record = self.store.get(draft_id)
        if record is None:
            return None
        media = list(record.get("media_paths") or [])
        caption = str(record.get("caption") or "")
        platforms = list(record.get("platforms") or [])
        fresh = stamp(media, caption, platforms, self.config)
        record["reconfirmations"] = int(record.get("reconfirmations") or 0) + 1
        record["reconfirmed_at"] = fresh["captured_at"]
        record["validation"] = fresh
        plan = record.get("plan")
        if isinstance(plan, dict):
            plan["recorded_at"] = fresh["captured_at"]
            plan["content_hash"] = fresh["content_hash"]
            plan["fingerprint"] = fresh["fingerprint"]
            plan["facts"] = fresh["facts"]
            plan["reconfirmed"] = True
        return self.store.upsert(record)

    # ── planning ─────────────────────────────────────────────────────────

    def record_plan(
        self,
        draft_id: str,
        *,
        plan: Any = None,
        ready: bool | None = None,
        blockers: Sequence[str] | None = None,
    ) -> dict[str, Any] | None:
        """Attach a canonical preflight plan (and its stamp) to a draft."""
        record = self.store.get(draft_id)
        if record is None:
            return None
        media = record.get("media_paths") or []
        caption = str(record.get("caption") or "")
        platforms = record.get("platforms") or []
        validation = stamp(media, caption, platforms, self.config)
        record["plan"] = {
            "recorded_at": validation["captured_at"],
            "ready": bool(ready),
            "blockers": [str(item) for item in (blockers or [])],
            "content_hash": validation["content_hash"],
            "fingerprint": validation["fingerprint"],
            "facts": validation["facts"],
            "plan": plan,
        }
        record["validation"] = validation
        record["status"] = STATUS_PLANNED
        return self.store.upsert(record)

    # ── verdicts ─────────────────────────────────────────────────────────

    def verdict_for(self, record: dict[str, Any]) -> dict[str, Any]:
        """Revalidate one stored record against the facts on disk right now."""
        media = list(record.get("media_paths") or [])
        caption = str(record.get("caption") or "")
        platforms = list(record.get("platforms") or [])
        current = stamp(media, caption, platforms, self.config)
        plan = record.get("plan") if isinstance(record.get("plan"), dict) else None
        reference = plan or (record.get("validation") if isinstance(record.get("validation"), dict) else None)

        reasons: list[dict[str, Any]] = []
        validated_at: str | None = None
        planned = plan is not None
        content_changed = False
        if reference:
            validated_at = (
                str(reference.get("captured_at") or reference.get("recorded_at") or "") or None
            )
            reasons.extend(explain_facts(reference.get("facts"), current["facts"]))
            recorded_content = str(reference.get("content_hash") or "")
            content_changed = bool(recorded_content) and recorded_content != current["content_hash"]
            if content_changed:
                reasons.insert(
                    0,
                    {
                        "code": CODE_CONTENT_CHANGED,
                        "message": (
                            "The draft changed after this plan was made — run the check again before posting."
                            if planned
                            else "The draft changed since it was last validated."
                        ),
                        "subject": None,
                    },
                )

        stale = bool(reasons) and reference is not None
        return {
            "draft_id": str(record.get("id") or ""),
            "status": str(record.get("status") or STATUS_DRAFT),
            "planned": planned,
            "stale": stale,
            "reasons": reasons,
            "content_changed": content_changed,
            "validated_at": validated_at,
            "checked_at": current["captured_at"],
            "current_fingerprint": current["fingerprint"],
            "recorded_fingerprint": str((reference or {}).get("fingerprint") or "") or None,
            "plan_ready": bool(plan.get("ready")) if plan else None,
            "plan_blockers": list(plan.get("blockers") or []) if plan else [],
            "plan_recorded_at": (str(plan.get("recorded_at") or "") or None) if plan else None,
            "reconfirmations": int(record.get("reconfirmations") or 0),
        }

    def revalidate(self, draft_id: str) -> dict[str, Any]:
        """Fresh verdict for one stored draft (raises ``KeyError`` if unknown)."""
        record = self.store.get(draft_id)
        if record is None:
            raise KeyError(draft_id)
        return {"draft": record, "verdict": self.verdict_for(record)}

    def list(self, *, include_posted: bool = False) -> list[dict[str, Any]]:
        """Stored drafts with a fresh verdict each, newest first."""
        rows: list[dict[str, Any]] = []
        for record in self.store.load():
            if not include_posted and record.get("status") == STATUS_POSTED:
                continue
            rows.append({"draft": record, "verdict": self.verdict_for(record)})
        return rows

    # ── the post gate ────────────────────────────────────────────────────

    def post_gate(
        self,
        draft_id: str,
        *,
        media_paths: Sequence[str] | None = None,
        caption: str = "",
        platforms: Sequence[str] | None = None,
        confirm_stale: bool = False,
    ) -> dict[str, Any]:
        """Decide whether a post may run against this draft's plan.

        A plan whose destination, auth, media, or content state changed is
        refused unless the caller passes ``confirm_stale=True`` — the explicit
        re-confirmation a human gives after reading the reasons. Re-confirming
        re-stamps the draft so the acceptance is recorded, not implied.

        Returns ``{"allowed": bool, "stale": bool, "reasons": [...], "draft":…,
        "verdict": …}``.
        """
        record = self.store.get(draft_id)
        if record is None:
            return {
                "allowed": False,
                "unknown_draft": True,
                "stale": False,
                "reasons": [],
                "draft": None,
                "verdict": None,
            }

        if media_paths is not None or caption or platforms is not None:
            record = self.sync_from_request(
                draft_id, media_paths=media_paths, caption=caption, platforms=platforms
            )["draft"]

        verdict = self.verdict_for(record)
        if verdict["stale"] and not confirm_stale:
            return {"allowed": False, "unknown_draft": False, "stale": True, "reasons": verdict["reasons"], "draft": record, "verdict": verdict}
        if verdict["stale"] and confirm_stale:
            record = self.record_confirmation(draft_id) or record
            verdict = self.verdict_for(record)
            verdict["reconfirmed"] = True
        return {"allowed": True, "unknown_draft": False, "stale": bool(verdict["stale"]), "reasons": [], "draft": record, "verdict": verdict}


__all__ = [
    "CODE_AUTH_CHANGED",
    "CODE_CONTENT_CHANGED",
    "CODE_DESTINATION_DISABLED",
    "CODE_DESTINATION_ENABLED",
    "CODE_DESTINATION_NOT_READY",
    "CODE_DESTINATION_READY",
    "CODE_MEDIA_AVAILABLE",
    "CODE_MEDIA_CHANGED",
    "CODE_MEDIA_MISSING",
    "DRAFT_FILE_NAME",
    "DRAFT_STORE_VERSION",
    "MAX_CAPTION_CHARS",
    "MAX_DRAFTS",
    "STATUS_DRAFT",
    "STATUS_PLANNED",
    "STATUS_POSTED",
    "DraftService",
    "DraftStore",
    "collect_facts",
    "content_hash",
    "explain_facts",
    "fingerprint_facts",
    "has_content",
    "media_fact_key",
    "new_draft_id",
    "normalize_media_paths",
    "normalize_platforms",
    "stamp",
]
