"""Frozen publish plans: preview → confirm → publish → readback, idempotently.

``docs/PHONE-FIRST-UX.md`` §2 draws a hard line between a *preview* (a local
computation) and a *publish* (a confirmed, frozen plan). Before this module the
HTTP surface had a preview service and a post endpoint, but nothing tied them
together: ``/api/post`` re-resolved its destination set from whatever the caller
sent at that moment, so a destination that connected (or dropped) between the
preview and the tap changed the plan silently, and a double tap ran the engine
twice.

This service owns that missing object:

* :meth:`PlanService.create` resolves the plan for a media set, caption and
  destination set and freezes it (media digests, envelopes, per-destination
  verdicts) under a ``plan_…`` id in ``<config_dir>/plans/``.
* :meth:`PlanService.confirm` revalidates the frozen inputs and refuses with
  ``confirm_stale`` when anything that decided the plan changed — the client
  must show the new plan and re-confirm.
* :meth:`PlanService.publish` executes the confirmed plan through the engine's
  existing :class:`~xpst.services.post_service.PostService` (no bespoke upload
  path). A replay returns the original result instead of posting again.
* :meth:`PlanService.readback` reports per destination the platform id, the URL
  when the platform exposed one, and an explicit verification state. It never
  upgrades a recorded id to "verified" without a readback that confirmed it.

Everything is local until ``publish`` runs a real post; a ``dry_run`` publish
makes no network calls and does not mark the plan published.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xpst.utils.atomic import write_text_atomic

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from xpst.config import XPSTConfig

#: Plan lifecycle states. ``published`` is terminal for a real run.
STATE_PREVIEWED = "previewed"
STATE_CONFIRMED = "confirmed"
STATE_PUBLISHED = "published"

#: Typed refusals the API renders verbatim.
ERROR_UNKNOWN_PLAN = "PLAN_UNKNOWN"
ERROR_NO_MEDIA = "PLAN_NO_MEDIA"
ERROR_NOT_CONFIRMED = "PLAN_NOT_CONFIRMED"
ERROR_STALE = "PLAN_CONFIRM_STALE"
ERROR_PREVIEW_FAILED = "PLAN_PREVIEW_FAILED"

#: Verification vocabulary — never a boolean, never "posted" for an id alone.
VERIFIED = "verified"
SUBMITTED_UNCONFIRMED = "submitted_unconfirmed"
NOT_ATTEMPTED = "not_attempted"
#: The destination did not publish because its window/cooldown said "not now".
#: M0: this must read as deferred with the window, never as failed.
DEFERRED = "deferred"
#: The destination was evaluated and refused the content (format, community).
REFUSED = "refused"
FAILED = "failed"

_LOCK = threading.Lock()


class PlanError(Exception):
    """A typed plan refusal (unknown plan, not confirmed, stale, no media).

    ``details`` carries the machine-readable part of the refusal: for a stale
    plan that is ``stale_reasons`` (one sentence per changed fact) plus the fresh
    ``preview`` the client should render before re-confirming. It is what makes
    ``confirm_stale`` actionable instead of a bare 409.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 409,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        payload.update(self.details)
        return payload


def plans_dir(config_dir: str | Path) -> Path:
    return Path(config_dir).expanduser() / "plans"


def _plan_path(config_dir: str | Path, plan_id: str) -> Path:
    return plans_dir(config_dir) / f"{plan_id}.json"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(material: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()


def _media_material(media_paths: Sequence[str]) -> list[dict[str, Any]]:
    """Describe the frozen media: path, size and content digest.

    The digest is of the file *bytes*, so a file replaced between preview and
    publish invalidates the plan instead of silently publishing different media.
    """
    material: list[dict[str, Any]] = []
    for raw in media_paths:
        path = Path(str(raw)).expanduser()
        if not path.is_file():
            material.append({"path": str(path), "exists": False, "bytes": 0, "sha256": None})
            continue
        material.append(
            {
                "path": str(path),
                "exists": True,
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    return material


class PlanService:
    """Create, confirm, publish and read back one frozen plan.

    Args:
        config: loaded engine config (destination truth comes from it).
        config_dir: config directory; plans are stored under ``plans/``.
        preview_fn: test seam — callable building the preview payload. The
            production path uses the same preflight/route-plan services the
            ``/api/preflight`` endpoint uses.
        post_service_factory: test seam returning the PostService used for
            publish (production: the real engine path).
        verify_fn: optional live readback seam ``(platform, post_id) -> dict``.
    """

    def __init__(
        self,
        config: XPSTConfig,
        config_dir: str | Path = "~/.xpst",
        *,
        preview_fn: Callable[..., dict[str, Any]] | None = None,
        post_service_factory: Callable[[], Any] | None = None,
        verify_fn: Callable[[str, str], dict[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self.config_dir = str(Path(config_dir).expanduser())
        self._preview_fn = preview_fn
        self._post_service_factory = post_service_factory
        self._verify_fn = verify_fn

    # ── Preview ─────────────────────────────────────────────────────────
    def preview(
        self,
        *,
        media_paths: Sequence[str] = (),
        caption: str = "",
        platforms: Sequence[str] = (),
        overrides: Mapping[str, str] | None = None,
        content_type: str = "auto",
    ) -> dict[str, Any]:
        """Compute the local plan for these inputs. Never uploads, never writes."""
        if self._preview_fn is not None:
            return self._preview_fn(
                media_paths=list(media_paths),
                caption=caption,
                platforms=list(platforms),
                overrides=dict(overrides or {}),
                content_type=content_type,
            )
        from xpst.services.post_preflight import PostPlanRequest, PostPreflightService, plan_content_type
        from xpst.services.route_plan import build_route_plan, detect_content_type

        resolved_type = detect_content_type(list(media_paths), content_type=content_type)
        request = PostPlanRequest(
            media_paths=list(media_paths),
            target_platforms=list(platforms),
            base_caption=caption,
            per_platform_captions=dict(overrides or {}),
            content_type=plan_content_type(
                _content_request(media_paths, caption, platforms, overrides, content_type)
            ),
        )
        try:
            plan = PostPreflightService(self.config).plan(request).to_dict()
        except Exception as exc:  # noqa: BLE001 - a preview failure is a refusal, not a 500
            raise PlanError(
                ERROR_PREVIEW_FAILED,
                f"The preview could not be computed: {str(exc)[:200]}",
                status_code=422,
            ) from exc
        route = build_route_plan(
            self.config,
            media_paths=[str(item) for item in media_paths],
            caption=caption,
            per_platform_captions=dict(overrides or {}),
            content_type=resolved_type,
            requested=list(platforms) or None,
            include_transform=False,
        ).to_dict()
        blockers = [issue.get("message") for issue in plan.get("hard_blockers") or []]
        rows = route.get("destinations") or []
        return {
            "ok": not blockers,
            "ready": not blockers,
            "content_type": resolved_type,
            "media_paths": [str(item) for item in media_paths],
            "caption": caption,
            "overrides": dict(overrides or {}),
            "platforms": list(platforms),
            "route": rows,
            "blockers": [item for item in blockers if item],
            "errors": list(route.get("errors") or []),
            "request_blockers": list(route.get("request_blockers") or []),
            "plan": plan,
            "network_calls": False,
        }

    # ── Lifecycle ───────────────────────────────────────────────────────
    def create(
        self,
        *,
        media_ids: Sequence[str] = (),
        media_paths: Sequence[str] = (),
        caption: str = "",
        platforms: Sequence[str] = (),
        overrides: Mapping[str, str] | None = None,
        content_type: str = "auto",
        client_idempotency_key: str = "",
        media_probe: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """Freeze a plan and return it with its ``plan_id``.

        ``media_ids`` are staged uploads (resolved here); ``media_paths`` are the
        engine-host paths the existing contract uses. At least one is required.
        """
        from xpst.media.ingest import resolve_media_paths

        paths = [str(item) for item in media_paths if str(item).strip()]
        if media_ids:
            paths.extend(resolve_media_paths(self.config_dir, list(media_ids)))
        paths = list(dict.fromkeys(paths))
        if not paths:
            raise PlanError(
                ERROR_NO_MEDIA,
                "This plan has no media. Upload a file first (POST /api/media) and pass its media id.",
                status_code=400,
            )

        key = str(client_idempotency_key or "").strip()
        if key:
            existing = self._find_by_idempotency_key(key)
            if existing is not None:
                same = [item.get("path") for item in existing.get("media", [])] == paths
                if same and existing.get("caption") == caption and list(existing.get("platforms") or []) == list(
                    platforms
                ):
                    return {**existing, "replayed": True}

        preview = self.preview(
            media_paths=paths,
            caption=caption,
            platforms=platforms,
            overrides=overrides,
            content_type=content_type,
        )
        media = _media_material(paths)
        record = {
            "plan_id": f"plan_{uuid.uuid4().hex[:16]}",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "state": STATE_PREVIEWED,
            "client_idempotency_key": key or None,
            "media": media,
            "media_ids": list(media_ids),
            "media_probe": [dict(item) for item in media_probe],
            "caption": caption,
            "overrides": dict(overrides or {}),
            "platforms": [str(item).strip().lower() for item in platforms if str(item).strip()],
            "content_type": preview.get("content_type"),
            "preview": preview,
            "digest": self._plan_digest(media, caption, overrides, platforms, preview),
            "confirmed_at": None,
            "confirmations": [],
            "result": None,
            "dry_run_result": None,
            "replayed_result": False,
        }
        self._save(record)
        return record

    def get(self, plan_id: str) -> dict[str, Any]:
        record = self._load(plan_id)
        if record is None:
            raise PlanError(
                ERROR_UNKNOWN_PLAN,
                "No plan with that id. It may have been created on another engine or removed.",
                status_code=404,
            )
        return record

    def confirm(
        self,
        plan_id: str,
        *,
        confirm_stale: bool = False,
        client_digest: str = "",
    ) -> dict[str, Any]:
        """Freeze a plan for publishing, refusing a stale plan by default.

        A stale plan is one whose deciding inputs changed since it was created
        (media bytes, caption, destination set, or a destination's resolved
        state/envelope). With ``confirm_stale=True`` the caller explicitly
        accepts the new plan: it is re-frozen and confirmed as it stands now.
        """
        record = self.get(plan_id)
        if record.get("state") == STATE_PUBLISHED:
            return {**record, "already_published": True}
        if client_digest and client_digest != record.get("digest"):
            raise PlanError(
                ERROR_STALE,
                "The client confirmed a different plan version than the engine holds. Reload the preview.",
                status_code=409,
            )

        fresh = self.preview(
            media_paths=[item["path"] for item in record.get("media") or []],
            caption=record.get("caption") or "",
            platforms=record.get("platforms") or [],
            overrides=record.get("overrides") or {},
            content_type=record.get("content_type") or "auto",
        )
        media = _media_material([item["path"] for item in record.get("media") or []])
        digest = self._plan_digest(
            media, record.get("caption") or "", record.get("overrides") or {}, record.get("platforms") or [], fresh
        )
        reasons = self._stale_reasons(record, media, fresh, digest)
        if reasons and not confirm_stale:
            raise PlanError(
                ERROR_STALE,
                "This plan changed since the preview: " + "; ".join(reasons),
                status_code=409,
                details={
                    "stale_reasons": reasons,
                    # The plan as it stands now, so the client can show the new
                    # preview instead of asking the operator to guess.
                    "preview": fresh,
                    "digest": digest,
                },
            )

        record.update(
            {
                "state": STATE_CONFIRMED,
                "updated_at": _now_iso(),
                "media": media,
                "preview": fresh,
                "digest": digest,
                "confirmed_at": _now_iso(),
            }
        )
        confirmations = list(record.get("confirmations") or [])
        confirmations.append({"at": record["confirmed_at"], "digest": digest, "reconfirmed": bool(reasons)})
        record["confirmations"] = confirmations
        self._save(record)
        return {**record, "stale_reconfirmed": bool(reasons), "stale_reasons": reasons}

    def publish(
        self,
        plan_id: str,
        *,
        dry_run: bool = False,
        force: bool = False,
        force_now: bool = False,
    ) -> dict[str, Any]:
        """Execute a confirmed plan, or return the original result on replay.

        A real publish is idempotent **per destination**: a destination that
        already posted is never uploaded again, so the second call returns the
        stored result with ``replayed: true`` and does not touch the engine.
        The one exception is a destination that was *deferred* (cooldown or
        anti-bot window): the plan stays retryable, and a later call publishes
        exactly the deferred destinations and merges their rows into the stored
        result — that is the "retry after the window publishes exactly once"
        rule in docs/PHONE-FIRST-UX.md §7. ``dry_run`` runs the engine's plan
        path only and never marks the plan published.
        """
        with _LOCK:
            record = self.get(plan_id)
            if record.get("state") not in (STATE_CONFIRMED, STATE_PUBLISHED):
                raise PlanError(
                    ERROR_NOT_CONFIRMED,
                    "Confirm the plan before publishing it. A preview alone never posts.",
                    status_code=409,
                )
            if dry_run and record.get("dry_run_result") is not None and not force:
                cached = record["dry_run_result"]
                return {**record, "dry_run_result": cached, "replayed": True, "network_calls": False}

            pending = list(record.get("platforms") or [])
            if not dry_run and record.get("result"):
                # A plan that already ran retries only what is still waiting; a
                # destination that posted is never uploaded twice.
                pending = _pending_retry_platforms(record)
                if not pending:
                    return {**record, "result": record.get("result"), "replayed": True}
            elif not dry_run and record.get("state") == STATE_PUBLISHED:
                return {**record, "result": record.get("result"), "replayed": True}

            overrides = {
                key: value
                for key, value in (record.get("overrides") or {}).items()
                if str(key).strip().lower() in {item.lower() for item in pending}
            }
            request = _content_request(
                [item["path"] for item in record.get("media") or []],
                record.get("caption") or "",
                pending,
                overrides,
                record.get("content_type") or "auto",
            )
            service = self._post_service()
            result = service.dry_run(request) if dry_run else service.execute(request, force_now=force_now)

            record["updated_at"] = _now_iso()
            if dry_run:
                record["dry_run_result"] = result
                record["dry_run_at"] = record["updated_at"]
            else:
                merged = _merge_result(record.get("result"), result, pending)
                record["result"] = merged
                record["replayed_result"] = False
                attempts = list(record.get("attempts") or [])
                attempts.append(
                    {
                        "at": record["updated_at"],
                        "platforms": list(pending),
                        "deferred": list(merged.get("deferred") or []),
                        "uploaded": list(merged.get("uploaded_platforms") or []),
                    }
                )
                record["attempts"] = attempts
                # A deferred destination keeps the plan retryable; only a run
                # with nothing left waiting becomes terminal.
                if merged.get("deferred_count"):
                    record["state"] = STATE_CONFIRMED
                else:
                    record["state"] = STATE_PUBLISHED
                    record["published_at"] = record["updated_at"]
            self._save(record)
            out = {**record}
            if dry_run:
                out["dry_run_result"] = record["dry_run_result"]
            else:
                out["result"] = record["result"]
            out["replayed"] = False
            return out

    def readback(self, plan_id: str, *, verify: bool = False) -> dict[str, Any]:
        """Per-destination readback: id, URL, verification state — never a guess.

        The durable authority is the plan's stored result (written by the engine
        before it returns) plus the engine's state file. ``verify=True`` asks a
        platform readback adapter to confirm the post exists; when no adapter can
        answer, the row stays ``submitted, unconfirmed`` with the reason.
        """
        record = self.get(plan_id)
        result = record.get("result") or {}
        rows_by_platform = {
            str(row.get("platform")): row for row in (result.get("destinations") or []) if row.get("platform")
        }
        requested = list(record.get("platforms") or [])
        video_id = str(result.get("video_id") or "")
        state_posts = _state_posts(self.config_dir, video_id) if video_id else {}

        rows: list[dict[str, Any]] = []
        for platform in requested:
            row = rows_by_platform.get(platform) or {}
            recorded = state_posts.get(platform) or {}
            post_id = row.get("post_id") or recorded.get("id")
            url = row.get("post_url") or recorded.get("url")
            state = str(row.get("state") or ("posted" if post_id else "not_attempted"))
            verification = SUBMITTED_UNCONFIRMED if post_id else NOT_ATTEMPTED
            reason = (
                "The engine recorded this platform id; no independent readback was run."
                if post_id
                else "Nothing was published to this destination."
            )
            resume_after = row.get("resume_after")
            if state == DEFERRED:
                # Scheduling, not failure: the destination is cooling down (or
                # outside the anti-bot window) and its own window is reported.
                verification = DEFERRED
                window = f" until {resume_after}" if resume_after else ""
                reason = str(row.get("error") or "").strip() or f"Deferred{window}: nothing was uploaded yet."
            elif state == "skipped_unconnected":
                verification = NOT_ATTEMPTED
                reason = f"Skipped: not connected. {row.get('reason') or ''}".strip()
            elif state in ("unsupported_format", "refused_by_community"):
                verification = REFUSED
                reason = str(row.get("reason") or row.get("error") or "Refused before upload.")
            elif row and row.get("success") is False:
                verification = FAILED
                reason = str(row.get("error") or "The upload failed.")
            if verify and post_id:
                checked = self._verify(platform, str(post_id))
                verification = checked.get("verification", SUBMITTED_UNCONFIRMED)
                reason = checked.get("reason", reason)
                url = checked.get("url") or url
            rows.append(
                {
                    "platform": platform,
                    "platform_id": post_id,
                    "url": url,
                    "verification": verification,
                    "verification_reason": reason,
                    "state": state,
                    "error": row.get("error"),
                    "retryable": row.get("retryable"),
                    "resume_after": resume_after,
                    "connect_command": row.get("connect_command"),
                    "caption": row.get("caption") or record.get("caption") or "",
                    "verified_at": record.get("updated_at") if verification == VERIFIED else None,
                }
            )
        return {
            "plan_id": record.get("plan_id"),
            "state": record.get("state"),
            "video_id": video_id,
            "published_at": record.get("published_at"),
            "verified": verify,
            "rows": rows,
            "network_calls": bool(verify),
        }

    # ── Internals ───────────────────────────────────────────────────────
    def _post_service(self) -> Any:
        if self._post_service_factory is not None:
            return self._post_service_factory()
        from xpst.services.post_service import PostService

        return PostService(self.config, self.config_dir)

    def _verify(self, platform: str, post_id: str) -> dict[str, Any]:
        if self._verify_fn is not None:
            return self._verify_fn(platform, post_id)
        from xpst.services.readback import verify_post

        return verify_post(self.config, platform, post_id)

    def _plan_digest(
        self,
        media: Sequence[Mapping[str, Any]],
        caption: str,
        overrides: Mapping[str, str] | None,
        platforms: Sequence[str],
        preview: Mapping[str, Any],
    ) -> str:
        """Digest the facts that decide a plan.

        Account state and envelopes are included, so a destination that drops to
        "not connected" or a format that stops fitting changes the digest — that
        is what makes ``confirm_stale`` meaningful rather than a checksum of the
        request form.
        """
        verdicts = [
            {
                "platform": str(row.get("platform") or "").lower(),
                "state": row.get("state"),
                "will_post": row.get("will_post"),
                "reason": row.get("reason"),
                "envelope": row.get("envelope"),
                "blockers": [str(item.get("code") or "") for item in row.get("blockers") or []],
            }
            for row in preview.get("route") or []
        ]
        return _digest(
            {
                "media": [{"path": item.get("path"), "exists": item.get("exists"), "sha256": item.get("sha256")} for item in media],
                "caption": caption,
                "overrides": dict(sorted((overrides or {}).items())),
                "platforms": [str(item).lower() for item in platforms],
                "content_type": preview.get("content_type"),
                "verdicts": sorted(verdicts, key=lambda item: item["platform"]),
                "blockers": sorted(str(item) for item in preview.get("blockers") or []),
            }
        )

    def _stale_reasons(
        self,
        record: Mapping[str, Any],
        media: Sequence[Mapping[str, Any]],
        fresh: Mapping[str, Any],
        digest: str,
    ) -> list[str]:
        reasons: list[str] = []
        for before, after in zip(record.get("media") or [], media, strict=False):
            if before.get("sha256") != after.get("sha256"):
                reasons.append(f"media changed: {Path(str(after.get('path'))).name}")
        for row in fresh.get("route") or []:
            platform = str(row.get("platform") or "")
            previous = next(
                (item for item in (record.get("preview") or {}).get("route") or [] if item.get("platform") == platform),
                None,
            )
            if previous is None:
                reasons.append(f"{platform}: added since the preview")
                continue
            if previous.get("state") != row.get("state"):
                reasons.append(f"{platform}: {previous.get('state')} → {row.get('state')}")
            elif previous.get("envelope") != row.get("envelope"):
                reasons.append(f"{platform}: envelope changed")
        if digest == record.get("digest"):
            reasons = []
        return reasons

    def _find_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        for path in sorted(plans_dir(self.config_dir).glob("plan_*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if record.get("client_idempotency_key") == key:
                return record
        return None

    def _load(self, plan_id: str) -> dict[str, Any] | None:
        safe = "".join(ch for ch in str(plan_id or "") if ch.isalnum() or ch in "_-")
        if not safe:
            return None
        path = _plan_path(self.config_dir, safe)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _save(self, record: Mapping[str, Any]) -> None:
        path = _plan_path(self.config_dir, str(record["plan_id"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True, default=str) + "\n", mode=0o600)


def _content_request(
    media_paths: Sequence[str],
    caption: str,
    platforms: Sequence[str],
    overrides: Mapping[str, str] | None,
    content_type: str,
) -> Any:
    """Build the canonical ContentRequest the preflight and engine share."""
    from xpst.content import ContentRequest

    payload: dict[str, Any] = {
        "media_paths": [str(item) for item in media_paths],
        "caption": caption,
        "platforms": [str(item) for item in platforms],
        "overrides": dict(overrides or {}),
    }
    raw_type = str(content_type or "").strip().lower()
    if raw_type and raw_type != "auto":
        payload["content_type"] = raw_type
    return ContentRequest.from_payload(payload)


def _pending_retry_platforms(record: Mapping[str, Any]) -> list[str]:
    """Destinations whose last attempt was deferred (and are worth retrying).

    Only ``deferred`` rows are retried: they were never uploaded, so retrying
    them cannot duplicate a post. A posted destination is excluded (exactly
    once), and a genuinely failed one is left to the operator/M1 retry surface.
    """
    rows = {
        str(row.get("platform")): row
        for row in ((record.get("result") or {}).get("destinations") or [])
        if row.get("platform")
    }
    pending: list[str] = []
    for platform in record.get("platforms") or []:
        row = rows.get(str(platform))
        if row is not None and row.get("state") == "deferred":
            pending.append(str(platform))
    return pending


def _merge_result(
    previous: Mapping[str, Any] | None,
    fresh: Mapping[str, Any],
    platforms: Sequence[str],
) -> dict[str, Any]:
    """Merge a retry's rows into the stored attempt result, honestly.

    The rows are the authority: a row from an earlier attempt survives unless
    this attempt produced a newer row for the same destination. Derived counts
    and ``ok`` are recomputed from the merged rows so nothing can claim a
    success that no row supports.
    """
    merged = dict(previous or {})
    rows: dict[str, dict[str, Any]] = {}
    for row in (previous or {}).get("destinations") or []:
        if row.get("platform"):
            rows[str(row["platform"])] = dict(row)
    for row in fresh.get("destinations") or []:
        if row.get("platform"):
            rows[str(row["platform"])] = dict(row)
    destinations = list(rows.values())
    uploaded = [row for row in destinations if row.get("success") is True]
    deferred = [row for row in destinations if row.get("state") == "deferred"]
    failed = [
        row
        for row in destinations
        if row.get("success") is False and row.get("state") not in ("skipped_unconnected", "deferred")
    ]
    blocked = bool(merged.get("blockers"))
    merged.update(
        {
            "destinations": destinations,
            "uploaded_platforms": [row["platform"] for row in uploaded],
            "uploaded_count": len(uploaded),
            "deferred": [row["platform"] for row in deferred],
            "deferred_count": len(deferred),
            "failed_count": len(failed),
            "ok": bool(uploaded) and not failed and not deferred and not blocked,
        }
    )
    # The plan's primary video id is the first attempt's: that is the run the
    # operator's record and the state file already point at. A retry that posts
    # a deferred destination carries that destination's id in its own row.
    if previous and previous.get("video_id"):
        merged["video_id"] = previous["video_id"]
    else:
        merged["video_id"] = str(fresh.get("video_id") or "")
    merged.setdefault("caption", fresh.get("caption") or (previous or {}).get("caption") or "")
    merged.setdefault("captions", fresh.get("captions") or (previous or {}).get("captions") or {})
    merged.setdefault("requested", list(platforms))
    merged.setdefault("content_type", fresh.get("content_type"))
    return merged


def _state_posts(config_dir: str | Path, video_id: str) -> dict[str, dict[str, Any]]:
    """Read the engine's durable post record for one video id.

    The state file is the authority for "did this actually get recorded", so a
    readback survives a client that lost the publish response.
    """
    from xpst.state_store import StateStore  # local import: keeps this module import-light

    try:
        # StateStore takes the config DIRECTORY (it appends state.json itself).
        store = StateStore(str(Path(config_dir).expanduser()))
        state = store.load_fresh() or {}
    except Exception:  # noqa: BLE001 - a missing state file is an empty readback
        return {}
    videos = state.get("posted_videos") or {}
    entry = videos.get(video_id) or {}
    posted_to = entry.get("posted_to") or {}
    rows: dict[str, dict[str, Any]] = {}
    for platform, data in posted_to.items():
        if isinstance(data, dict):
            rows[str(platform)] = {
                "id": data.get("id") or data.get("post_id"),
                "url": data.get("url") or data.get("post_url"),
            }
    return rows


__all__ = [
    "DEFERRED",
    "ERROR_NOT_CONFIRMED",
    "ERROR_NO_MEDIA",
    "ERROR_PREVIEW_FAILED",
    "ERROR_STALE",
    "ERROR_UNKNOWN_PLAN",
    "FAILED",
    "NOT_ATTEMPTED",
    "PlanError",
    "PlanService",
    "REFUSED",
    "STATE_CONFIRMED",
    "STATE_PREVIEWED",
    "STATE_PUBLISHED",
    "SUBMITTED_UNCONFIRMED",
    "VERIFIED",
    "plans_dir",
]
