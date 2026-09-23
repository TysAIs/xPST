"""Thin JSON API routers for the web UI (Phase 1 foundation)."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)


def require_api_token(request: Request) -> None:
    """Route-level guard: mutating ``/api`` routes fail closed.

    The app-wide middleware in :mod:`xpst.dashboard.server` is the primary
    enforcement point (it also covers non-``/api`` mutations such as
    ``/bio/edit``). This dependency is defence in depth: it keeps the
    protection attached to the routes themselves, so a future app assembly that
    includes this router without the middleware still cannot execute a post,
    a connect flow or an onboarding write anonymously.

    Credentials come from the app state populated by ``_create_app`` (dashboard
    Basic auth + API tokens). On a bare router the only accepted token is the
    one supplied through ``XPST_API_TOKEN`` / ``XPST_UI_TOKEN`` — never a
    default.
    """
    from xpst.dashboard.auth import env_tokens, mutation_authorized

    accepted = getattr(request.app.state, "xpst_api_tokens", None)
    if accepted is None:
        accepted = env_tokens()
    username, password_hash = getattr(request.app.state, "xpst_dashboard_auth", ("", ""))
    if mutation_authorized(
        request,
        accepted=set(accepted),
        username=username or "",
        password_hash=password_hash or "",
    ):
        return
    raise HTTPException(
        status_code=401,
        detail=(
            "Mutating endpoints require the xPST API token. Run "
            "`xpst auth api-token --show` (or set XPST_API_TOKEN) and send it as "
            "`Authorization: Bearer <token>` or `X-API-Token: <token>`."
        ),
    )


# Live auth probes hit the network (measured ~5.6s on a real machine), so the
# Home screen must not pay that cost on every load. Entries are keyed by config
# dir; the TTL is XPST_AUTH_STATUS_TTL seconds (default 60; 0 disables caching).
# The response reports whether it was served from cache and how old it is, so a
# stale-while-revalidate answer can never masquerade as a fresh probe.
_AUTH_STATUS_CACHE: dict[str, tuple[float, dict[str, Any], dict[str, Any]]] = {}
_AUTH_STATUS_LOCK = threading.Lock()
_AUTH_STATUS_REFRESHING: set[str] = set()
DEFAULT_AUTH_STATUS_TTL_S = 60.0


def _auth_status_ttl() -> float:
    raw = os.environ.get("XPST_AUTH_STATUS_TTL")
    if raw is None:
        return DEFAULT_AUTH_STATUS_TTL_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_AUTH_STATUS_TTL_S


def _refresh_due_tokens_quietly(config: Any) -> None:
    """Renew expiring access tokens before a live probe (bounded, secret-free).

    Runs only when a token is actually due; failures are recorded (and keep the
    badge red) rather than raised — a refresh problem must not break a status
    read.
    """
    try:
        from xpst.token_refresh import (
            FAST_BASE_DELAY_SECONDS,
            FAST_DEADLINE_SECONDS,
            FAST_MAX_ATTEMPTS,
            refresh_due_tokens,
            save_refresh_report,
        )

        report = refresh_due_tokens(
            config,
            max_attempts=FAST_MAX_ATTEMPTS,
            base_delay=FAST_BASE_DELAY_SECONDS,
            deadline=FAST_DEADLINE_SECONDS,
        )
        if report:
            save_refresh_report(config, report)
    except Exception as exc:  # noqa: BLE001 - refresh is best-effort
        logger.debug("Background token refresh skipped: %s", exc)


def _probe_and_store(
    config_dir: str, config: Any, *, refresh_tokens: bool = False
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the live probe and store the result.

    ``refresh_tokens`` is only set on the background path: a cold request must
    not wait on a token refresh (bounded, but still seconds of retry budget)
    before the UI can paint.
    """
    from xpst.auth_status import collect_live_auth_status
    from xpst.provider_truth import canonical_status_report

    if refresh_tokens:
        # Renew expiring tokens first so the badge derived below reflects a
        # refreshed token instead of the one that was about to die.
        _refresh_due_tokens_quietly(config)
    auth = collect_live_auth_status(config)
    canonical = canonical_status_report(config, auth)
    with _AUTH_STATUS_LOCK:
        _AUTH_STATUS_CACHE[str(config_dir)] = (time.monotonic(), auth, canonical)
    return auth, canonical


# The outcome report verifies post ownership, which for YouTube is a real API
# round trip (channel uploads playlist). The Analytics page must not pay that
# on every load, so recorded-mode reports are memoized per config dir for
# XPST_ANALYTICS_OUTCOME_TTL seconds (default 60; 0 disables caching).
_OUTCOME_REPORT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_OUTCOME_REPORT_LOCK = threading.Lock()
DEFAULT_OUTCOME_TTL_S = 60.0


def _outcome_ttl() -> float:
    raw = os.environ.get("XPST_ANALYTICS_OUTCOME_TTL")
    if raw is None:
        return DEFAULT_OUTCOME_TTL_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_OUTCOME_TTL_S


def _cached_outcome_report(config_dir: str) -> dict[str, Any]:
    """Recorded-mode outcome report, memoized for a short TTL."""
    from xpst.analytics import AnalyticsCollector

    ttl = _outcome_ttl()
    key = str(config_dir)
    now = time.monotonic()
    if ttl > 0:
        with _OUTCOME_REPORT_LOCK:
            hit = _OUTCOME_REPORT_CACHE.get(key)
            if hit is not None and (now - hit[0]) < ttl:
                return hit[1]
    report = AnalyticsCollector(config_dir).outcome_report()
    with _OUTCOME_REPORT_LOCK:
        _OUTCOME_REPORT_CACHE[key] = (time.monotonic(), report)
    return report


def _refresh_in_background(config_dir: str, config: Any) -> bool:
    """Re-probe off the request path. Returns False when one is already running."""
    key = str(config_dir)
    with _AUTH_STATUS_LOCK:
        if key in _AUTH_STATUS_REFRESHING:
            return False
        _AUTH_STATUS_REFRESHING.add(key)

    def work() -> None:
        try:
            _probe_and_store(config_dir, config, refresh_tokens=True)
        except Exception as exc:  # noqa: BLE001 - a failed refresh keeps the stale value
            logger.debug("Background auth refresh failed: %s", exc)
        finally:
            with _AUTH_STATUS_LOCK:
                _AUTH_STATUS_REFRESHING.discard(key)

    threading.Thread(target=work, name="xpst-auth-refresh", daemon=True).start()
    return True


def warm_auth_status_cache(config_dir: str, config: Any) -> None:
    """Prime the cache at startup so the first UI load is not a cold probe."""
    if _auth_status_ttl() <= 0:
        return
    _refresh_in_background(config_dir, config)


def _has_cached_auth(config_dir: str) -> bool:
    with _AUTH_STATUS_LOCK:
        return str(config_dir) in _AUTH_STATUS_CACHE


def _badge_summary(auth: Mapping[str, Any] | None) -> dict[str, str]:
    """``{platform: badge}`` from a live-auth mapping (never invents a badge)."""
    summary: dict[str, str] = {}
    if not isinstance(auth, Mapping):
        return summary
    for name, entry in auth.items():
        if isinstance(entry, Mapping) and entry.get("badge"):
            summary[str(name)] = str(entry["badge"])
    return summary


def _auth_checked_at(auth: Mapping[str, Any] | None) -> float | None:
    """Newest live-check timestamp in a live-auth mapping (the badge's age)."""
    stamps: list[float] = []
    if isinstance(auth, Mapping):
        for entry in auth.values():
            if isinstance(entry, Mapping) and isinstance(entry.get("checked_at"), (int, float)):
                stamps.append(float(entry["checked_at"]))
    return max(stamps) if stamps else None


def _auth_checked_at_iso(auth: Mapping[str, Any] | None) -> str | None:
    from xpst.token_state import iso_timestamp

    return iso_timestamp(_auth_checked_at(auth))


def _auth_probe_in_flight(config_dir: str) -> bool:
    with _AUTH_STATUS_LOCK:
        return str(config_dir) in _AUTH_STATUS_REFRESHING


def _live_auth_and_canonical(
    config_dir: str, config: Any
) -> tuple[dict[str, Any], dict[str, Any], bool, float, bool]:
    """Return ``(auth, canonical, from_cache, age_seconds, stale)``.

    An expired entry is served immediately and refreshed behind the request, so
    the UI never blocks on the network; ``stale`` tells the caller the answer is
    an older probe rather than a fresh one.
    """
    key = str(config_dir)
    ttl = _auth_status_ttl()
    now = time.monotonic()
    with _AUTH_STATUS_LOCK:
        entry = _AUTH_STATUS_CACHE.get(key)

    if entry is not None and ttl > 0:
        age = now - entry[0]
        if age < ttl:
            return entry[1], entry[2], True, age, False
        _refresh_in_background(config_dir, config)
        return entry[1], entry[2], True, age, True

    auth, canonical = _probe_and_store(config_dir, config)
    return auth, canonical, False, 0.0, False


def create_api_router(
    config_dir: str = "~/.xpst",
    *,
    engine_factory: Any | None = None,
    uploaders: dict[str, Any] | None = None,
) -> APIRouter:
    """Build the /api router bound to a config directory.

    Args:
        config_dir: config directory the API reads from.
        engine_factory: optional test seam — callable returning an engine-like
            object used by ``POST /api/post``. Production passes ``None`` so the
            real :class:`~xpst.engine.CrossPostEngine` runs.
        uploaders: optional test seam — ``{platform: uploader}`` used by
            ``POST /api/connect/{platform}`` instead of building real platform
            uploaders (keeps a verification call off the network).
    """
    router = APIRouter(prefix="/api", tags=["ui"])

    def _load_ui_config() -> Any:
        """Load this config dir's config, falling back to defaults.

        Read-only by design: a GET must never create or modify a config file.
        A brand-new install (no config.yaml yet) is answered from defaults, and
        the first *write* endpoint materializes the file.
        """
        from xpst.config import XPSTConfig

        path = str(Path(config_dir).expanduser() / "config.yaml")
        try:
            return XPSTConfig.load(path)
        except Exception as exc:  # noqa: BLE001 - a missing/corrupt config must not 500 the UI
            logger.debug("Using default config for %s: %s", config_dir, exc)
            config = XPSTConfig()
            config.config_dir = str(Path(config_dir).expanduser())
            return config

    def _save_ui_config(config: Any) -> None:
        """Persist a config to this router's config directory."""
        target = Path(config_dir).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        config.config_dir = str(target)
        config.save(str(target / "config.yaml"))

    def _destination_providers(config: Any) -> list[dict[str, Any]]:
        """Canonical video-destination providers, in catalog order.

        Config-only truth (no live probe): an entry is ``ready`` only when the
        destination is enabled AND its local credentials exist.
        """
        from xpst.provider_truth import canonical_provider_catalog

        catalog = canonical_provider_catalog(config)
        providers = [
            provider
            for provider in catalog.get("providers", [])
            if "video_destination" in (provider.get("roles") or [])
        ]
        for provider in providers:
            role = (provider.get("role_status") or {}).get("video_destination") or {}
            provider["destination_state"] = role.get("state", "unconfigured")
            provider["destination_ready"] = role.get("state") == "ready"
            provider["destination_error"] = role.get("error")
        return providers

    def _guide_payload(platform: str) -> dict[str, Any]:
        """Click-by-click setup steps for one platform (never a secret)."""
        try:
            from xpst.wizard import PLATFORM_GUIDES

            guide = PLATFORM_GUIDES.get(platform)
            if guide is None:
                return {"platform": platform, "title": platform, "why": "", "steps": [], "docs_url": ""}
            return {
                "platform": platform,
                "title": guide.title,
                "why": guide.why,
                "steps": [step.text for step in guide.steps],
                "docs_url": guide.docs_url,
            }
        except Exception as exc:  # noqa: BLE001 - a missing guide must not break connect
            logger.debug("No guide for %s: %s", platform, exc)
            return {"platform": platform, "title": platform, "why": "", "steps": [], "docs_url": ""}

    def _onboarding_payload(config: Any) -> dict[str, Any]:
        """First-run state: source folder, destinations, readiness, next step."""
        from xpst.readiness import build_readiness_report

        destinations = _destination_providers(config)
        source_path = str(getattr(getattr(config, "local", None), "path", "") or "")
        source_exists = bool(source_path) and Path(source_path).expanduser().is_dir()
        ready_destinations = [item["name"] for item in destinations if item.get("destination_ready")]

        try:
            readiness = build_readiness_report(config).to_dict()
        except Exception as exc:  # noqa: BLE001 - report truthfully instead of 500
            logger.warning("Readiness report failed: %s", exc)
            readiness = {"ready": False, "summary": f"Readiness could not be computed: {str(exc)[:200]}", "checks": [], "blocking": [], "warnings": []}

        first_run_complete = bool(getattr(config, "first_run_complete", False))
        if ready_destinations:
            next_step = {
                "kind": "compose",
                "label": "Compose a post",
                "route": "#/compose",
                "detail": ", ".join(ready_destinations),
            }
        elif destinations:
            next_step = {
                "kind": "connect",
                "label": "Connect a destination",
                "route": "#/connect",
                "detail": "No destination is ready yet.",
            }
        else:
            next_step = {
                "kind": "configure",
                "label": "Review configuration",
                "route": "#/settings",
                "detail": "No video destination is available.",
            }

        steps = [
            {"id": "welcome", "title": "Welcome to xPST", "done": True},
            {"id": "source", "title": "Choose a content folder", "done": source_exists},
            {"id": "destination", "title": "Connect a platform", "done": bool(ready_destinations)},
            {"id": "ready", "title": "Compose your first post", "done": False},
        ]
        return {
            "first_run_complete": first_run_complete,
            "show_wizard": not first_run_complete,
            "source": {"path": source_path, "exists": source_exists},
            "destinations": destinations,
            "ready_destinations": ready_destinations,
            "readiness": readiness,
            "next_step": next_step,
            "steps": steps,
        }

    @router.get("/onboarding")
    def api_onboarding() -> dict[str, Any]:
        """First-run onboarding state (read-only; never creates a config file)."""
        return _onboarding_payload(_load_ui_config())

    @router.post("/onboarding", dependencies=[Depends(require_api_token)])
    def api_onboarding_save(payload: dict[str, Any]) -> dict[str, Any]:
        """Persist the first-run choices (content folder + enabled destinations).

        Only non-secret local choices are written. Credentials are never
        accepted here — connecting a real account stays in the interactive
        connect flow (``xpst connect`` / the Tauri deep-link).
        """
        from xpst.readiness import repair_local_setup

        config = _load_ui_config()
        data = payload or {}
        applied: list[str] = []

        local = data.get("local")
        if isinstance(local, dict) and "path" in local:
            config.local.path = str(local.get("path") or "").strip()
            applied.append("local.path")

        destinations = data.get("destinations")
        if isinstance(destinations, dict):
            known = {item["name"] for item in _destination_providers(config)}
            for name, enabled in destinations.items():
                key = str(name).strip().lower()
                if key not in known:
                    raise HTTPException(status_code=404, detail=f"Unknown destination platform: {name}")
                getattr(config, key).enabled = bool(enabled)
                applied.append(f"accounts.{key}.enabled")

        _save_ui_config(config)
        reloaded = _load_ui_config()
        try:
            repair = repair_local_setup(reloaded)
            actions = list(repair.get("actions", []))
        except Exception as exc:  # noqa: BLE001 - repair is best-effort
            logger.debug("Local setup repair skipped: %s", exc)
            actions = []

        payload_out = _onboarding_payload(reloaded)
        payload_out["ok"] = True
        payload_out["applied"] = applied
        payload_out["actions"] = actions
        return payload_out

    @router.post("/onboarding/complete", dependencies=[Depends(require_api_token)])
    def api_onboarding_complete() -> dict[str, Any]:
        """Persist ``first_run_complete`` so the wizard is offered once."""
        config = _load_ui_config()
        config.first_run_complete = True
        _save_ui_config(config)
        reloaded = _load_ui_config()
        payload = _onboarding_payload(reloaded)
        return {
            "ok": True,
            "first_run_complete": bool(reloaded.first_run_complete),
            "next_step": payload["next_step"],
        }

    @router.get("/media")
    def api_media(folder: str = "", limit: int = 100) -> dict[str, Any]:
        """List local media in a folder for the compose screen.

        The folder comes from the caller or from the configured content folder.
        No folder is guessed from the user's home directory, and nothing is
        scanned recursively.

        Only files that at least one destination can actually publish are
        returned in ``items``. A file whose modality no destination accepts
        (an image, today) is moved to ``skipped`` with the plain-language reason
        from the canonical media spec, so the UI never offers something the
        preflight would hard-reject and nothing is dropped without a trace.
        """
        from xpst.media.modality import (
            IMAGE_EXTENSIONS,
            VIDEO_EXTENSIONS,
            detect_modality,
        )
        from xpst.media.specs import destinations_for_modality, modality_unsupported_message

        config = _load_ui_config()
        configured = str(getattr(getattr(config, "local", None), "path", "") or "")
        target = (folder or configured or "").strip()
        cap = max(1, min(int(limit or 100), 500))

        if not target:
            return {
                "ok": True,
                "folder": "",
                "exists": False,
                "items": [],
                "count": 0,
                "skipped": [],
                "skipped_count": 0,
                "hint": "No content folder is configured yet. Choose one during setup.",
            }

        path = Path(target).expanduser()
        if not path.is_dir():
            return {
                "ok": False,
                "folder": str(path),
                "exists": False,
                "items": [],
                "count": 0,
                "skipped": [],
                "skipped_count": 0,
                "error": f"Folder not found: {path}",
            }

        extensions = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS
        files: list[Path] = []
        for extension in extensions:
            files.extend(path.glob(f"*{extension}"))
            files.extend(path.glob(f"*{extension.upper()}"))
        unique = sorted(set(files), key=lambda item: item.name.lower())[:cap]

        items: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for item in unique:
            try:
                stat = item.stat()
            except OSError:
                continue
            suffix = item.suffix.lower()
            modality = detect_modality(item)
            # The same capability answer the preflight uses: a modality no
            # destination accepts is not offered, and it is never offered "with
            # a warning" that the post path would then turn into a hard refusal.
            # No destination list is emitted on purpose — "where can I post
            # this" is a readiness question the canonical preflight answers
            # (auth, quota, destination-specific limits), and guessing it here
            # would recreate the same offer-then-refuse defect.
            postable = bool(destinations_for_modality(modality)) if modality else False
            entry = {
                "path": str(item),
                "name": item.name,
                "size_bytes": stat.st_size,
                "type": modality or suffix.lstrip(".") or "unknown",
                "modified": stat.st_mtime,
                "postable": postable,
            }
            if postable:
                items.append(entry)
            else:
                entry["reason"] = modality_unsupported_message(
                    modality or "unknown",
                    suffix=suffix,
                )
                skipped.append(entry)

        payload: dict[str, Any] = {
            "ok": True,
            "folder": str(path),
            "exists": True,
            "items": items,
            "count": len(items),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }
        if skipped:
            payload["hint"] = skipped[0]["reason"]
        return payload

    @router.get("/media/stream")
    def api_media_stream(request: Request, path: str = "") -> Response:
        """Stream one local media file to the composer's preview player.

        Contract the UI depends on:

        * ``Accept-Ranges: bytes`` and a real ``206 Partial Content`` answer,
          so a video is *playable and seekable* in the webview and the client
          pulls only the ranges it needs.
        * The body is produced by a bounded chunk iterator (see
          :mod:`xpst.dashboard.media_preview`), so selecting or scrubbing a 2 GB
          video never costs the engine a full-file read.

        Only real, previewable local files are served; a directory, a missing
        path, or a non-media extension is refused before any bytes are read.
        """
        from xpst.dashboard.media_preview import (
            iter_file_chunks,
            media_content_type,
            parse_byte_range,
            resolve_media_path,
        )

        try:
            resolved = resolve_media_path(path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        try:
            size = resolved.stat().st_size
        except OSError as exc:
            raise HTTPException(status_code=404, detail=f"Media file unreadable: {exc}") from None

        try:
            span = parse_byte_range(request.headers.get("range"), size)
        except ValueError:
            # 416 with the true size: a player asking past EOF must not be
            # handed the whole file as a consolation prize.
            return JSONResponse(
                status_code=416,
                content={"ok": False, "error": "Range not satisfiable", "size_bytes": size},
                headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes */{size}"},
            )

        start, end = span if span else (0, max(size - 1, 0))
        length = end - start + 1 if size else 0
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Cache-Control": "private, max-age=0",
        }
        status_code = 200
        if span is not None:
            status_code = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return StreamingResponse(
            iter_file_chunks(resolved, start=start, length=length),
            status_code=status_code,
            media_type=media_content_type(resolved),
            headers=headers,
        )

    @router.get("/media/thumb")
    def api_media_thumb(path: str = "", width: int = 640) -> FileResponse:
        """Serve a generated, cached thumbnail (JPEG) for one media file.

        ``ffmpeg`` extracts a single scaled frame; the result is cached under
        ``~/.xpst/cache/previews`` keyed by path+size+mtime, so re-selecting
        the same asset is a file read of a small JPEG, not another decode.

        404 means *no thumbnail could be generated* (ffmpeg missing or the
        decode failed) — the UI answers that by streaming the original file
        through ``/api/media/stream``, so a preview still appears.
        """
        from xpst.dashboard.media_preview import resolve_media_path, thumbnail_for

        try:
            resolved = resolve_media_path(path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        thumb = thumbnail_for(resolved, width=max(64, min(int(width or 640), 1280)))
        if thumb is None:
            raise HTTPException(
                status_code=404,
                detail="No thumbnail could be generated for this file; stream the original instead.",
            )
        return FileResponse(thumb, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    @router.post("/connect/{platform}", dependencies=[Depends(require_api_token)])
    def api_connect(platform: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Inspect, enable, and verify one destination platform.

        Truth contract: ``connected`` is True only when the canonical live
        probe reports the destination authenticated. A request that merely
        succeeded returns ``connected: false`` with the state and the exact
        blocker — the UI must never show a success badge for an unconnected
        account.

        Body:
            dry_run: True (default) reports the plan without writing config.
            enable: optional bool — set ``accounts.<platform>.enabled``.
            verify: bool (default True) — run the canonical live probe.
        """
        from xpst.auth_status import collect_live_auth_status
        from xpst.provider_truth import canonical_status_report

        config = _load_ui_config()
        key = str(platform).strip().lower()
        destinations = {item["name"]: item for item in _destination_providers(config)}
        if key not in destinations:
            raise HTTPException(status_code=404, detail=f"Unknown destination platform: {platform}")

        data = payload or {}
        dry_run = bool(data.get("dry_run", True))
        verify = bool(data.get("verify", True))
        enable = data.get("enable")

        config_changed = False
        if isinstance(enable, bool):
            account = getattr(config, key, None)
            if account is not None and bool(getattr(account, "enabled", False)) != enable:
                if dry_run:
                    config_changed = False
                else:
                    account.enabled = enable
                    _save_ui_config(config)
                    config = _load_ui_config()
                    config_changed = True

        entry: dict[str, Any] = {}
        # ``None`` = not probed (a disabled destination is never probed, and
        # "not probed" must not read as "checked and failed").
        live_checked: bool | None = None
        probe_error: str | None = None
        if verify and getattr(getattr(config, key, None), "enabled", False):
            try:
                if uploaders is not None:
                    canonical = canonical_status_report(config, collect_live_auth_status(config, uploaders))
                else:
                    canonical = canonical_status_report(config, collect_live_auth_status(config))
                providers = canonical.get("providers") or {}
                entry = dict(providers.get(key) or {})
                live_checked = bool(entry)
            except Exception as exc:  # noqa: BLE001 - an unverifiable account is not a 500
                probe_error = str(exc)[:200]
                live_checked = False
                logger.debug("Connect probe failed for %s: %s", key, exc)

        provider = destinations[key]
        role = (entry.get("role_status") or {}).get("video_destination") or {}
        state = str(role.get("state") or provider.get("destination_state") or "unconfigured")
        authenticated = bool(entry.get("authenticated")) if entry else False
        verified_ready = state == "ready"
        # ``live_checked`` is None for a skipped probe; None is falsy, so the
        # connected verdict stays conservative without claiming a check ran.
        connected = bool((authenticated or verified_ready) and live_checked)

        enabled_now = bool(getattr(getattr(config, key, None), "enabled", False))
        if connected:
            next_action = {"kind": "compose", "label": "Compose a post", "route": "#/compose"}
        elif not enabled_now:
            next_action = {"kind": "enable", "label": f"Enable {provider['display_name']}", "route": "#/connect"}
        else:
            next_action = {"kind": "authenticate", "label": "Finish sign-in", "route": "#/connect"}

        return {
            "ok": True,
            "platform": key,
            "display_name": provider.get("display_name", key),
            "dry_run": dry_run,
            "enabled": enabled_now,
            "enable_requested": enable if isinstance(enable, bool) else None,
            "config_changed": config_changed,
            "connected": connected,
            "authenticated": authenticated,
            "state": state,
            "verified": live_checked is True,
            "live_checked": live_checked,
            "error": probe_error or role.get("error") or provider.get("destination_error"),
            "auth_mode": entry.get("auth_mode") or provider.get("auth_mode"),
            "official_api": bool(provider.get("is_official_api")),
            "docs_url": provider.get("docs_url") or "",
            "guide": _guide_payload(key),
            "next_action": next_action,
        }

    @router.post("/post", dependencies=[Depends(require_api_token)])
    def api_post(payload: dict[str, Any]) -> JSONResponse:
        """Run (or plan) a manual post through the canonical engine path.

        ``dry_run: true`` returns the preflight plan and uploads nothing.
        Otherwise the real engine runs; every requested destination appears in
        the response and a destination that published nothing is reported as a
        failure (never as success). A request refused by preflight returns 409
        with the same truthful envelope.
        """
        from xpst.services.post_service import PostRequest, PostService

        data = payload or {}
        request = PostRequest.from_payload(data)
        dry_run = bool(data.get("dry_run", False))
        service = PostService(_load_ui_config(), config_dir, engine_factory=engine_factory)
        envelope = service.dry_run(request) if dry_run else service.execute(request)
        envelope["request"] = {
            "media_paths": request.media_paths,
            "caption": request.caption,
            "platforms": request.platforms,
            "dry_run": dry_run,
        }
        blocked = bool(envelope.get("blocked")) or (not dry_run and not envelope.get("ok") and envelope.get("blockers"))
        return JSONResponse(envelope, status_code=409 if blocked else 200)

    @router.get("/summary")
    def api_summary() -> dict[str, Any]:
        from xpst.dashboard.analytics import cached_summary_stats

        return cached_summary_stats(config_dir)

    @router.get("/analytics/outcomes")
    def api_analytics_outcomes(live: int = 0) -> dict[str, Any]:
        """Per-post/per-platform outcomes, labelled recorded vs live.

        ``live=0`` (default) reads the persisted snapshot store plus the
        verified ownership set — no metric network calls. ``live=1`` runs a
        real collection first (platform APIs, seconds, API quota) and labels
        the rows it fetched as ``live``; posts whose collection failed keep
        their recorded snapshot and stay labelled ``recorded``.

        Every number traces to a post in the verified ownership set. A
        platform with no metric-bearing owned post returns ``totals: null``
        so the UI renders "no data" instead of a zero.
        """
        from xpst.analytics import AnalyticsCollector

        if live:
            import asyncio

            collector = AnalyticsCollector(config_dir)
            return asyncio.run(collector.collect_outcome_report())
        return _cached_outcome_report(config_dir)

    @router.get("/videos")
    def api_videos() -> dict[str, Any]:
        from xpst.dashboard.analytics import AnalyticsCollector

        collector = AnalyticsCollector(config_dir)
        lineup = collector.get_video_lineup()
        try:
            totals = collector._store().platform_totals()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("platform_totals read failed: %s", exc)
            totals = {}
        return {"videos": lineup, "count": len(lineup), "platform_totals": totals}

    @router.get("/videos/{video_id}")
    def api_video_detail(video_id: str) -> dict[str, Any]:
        from xpst.dashboard.analytics import AnalyticsCollector

        collector = AnalyticsCollector(config_dir)
        posts = [e for e in collector.get_video_lineup() if e.get("video_id") == video_id]
        if not posts:
            raise HTTPException(status_code=404, detail=f"Unknown video: {video_id}")

        history: dict[str, dict[str, Any]] = {}
        for post in posts:
            platform = str(post.get("platform") or "")
            post_id = str(post.get("post_id") or "")
            if not platform or not post_id:
                continue
            try:
                store = collector._store()
                latest = store.latest_for_post(platform, post_id)
                series = store.history(platform, post_id, limit=100)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Snapshot read failed for %s/%s: %s", platform, post_id, exc)
                latest, series = [], []
            history[f"{platform}:{post_id}"] = {"latest": latest, "series": series}
        return {"video_id": video_id, "posts": posts, "metrics": history}

    @router.get("/health-status")
    def api_health_status() -> dict[str, Any]:
        """Engine health from state + live auth liveness per platform.

        Data: ``load_state`` health block (src/xpst/dashboard/analytics.py:70)
        and ``collect_live_auth_status`` (src/xpst/auth_status.py:290 —
        async core ``collect_live_auth_status_async`` :237). The live check
        has its own internal timeout and degrades to honest all-false
        entries instead of raising; any residual failure surfaces as
        ``auth_error`` with HTTP 200 (the UI renders a degraded state,
        it does not want a 5xx).
        """
        from xpst.dashboard.analytics import load_state

        state = load_state(config_dir)
        health = state.get("health", {})
        platforms = health.get("platforms", {})
        status = "healthy" if all(p.get("status") == "ok" for p in platforms.values()) else "degraded"

        auth: dict[str, Any] = {}
        auth_error: str | None = None
        auth_cached = False
        auth_age_seconds: float | None = None
        auth_stale = False
        try:
            from xpst.config import XPSTConfig

            config = XPSTConfig.load(str(Path(config_dir).expanduser() / "config.yaml"))
            if _auth_probe_in_flight(config_dir) and not _has_cached_auth(config_dir):
                # A probe is already running (startup warm-up) and nothing is
                # cached yet: answer immediately and say it is still being
                # checked, instead of blocking the first paint on the network.
                return {
                    "status": "pending",
                    "platforms": platforms,
                    "total_processed": health.get("total_processed", 0),
                    "auth": {},
                    "auth_error": None,
                    "auth_cached": False,
                    "auth_age_seconds": None,
                    "auth_stale": False,
                    "readiness_pending": True,
                    "canonical": {"providers": {}, "platforms": {}, "roles": []},
                    "providers": {},
                    "readiness": {"ready": False, "blockers": [], "pending": True},
                    "next_action": {
                        "kind": "checking",
                        "label": "Checking live accounts",
                        "role": "video_destination",
                    },
                    "can_create_post": False,
                }

            (
                auth,
                canonical,
                auth_cached,
                auth_age_seconds,
                auth_stale,
            ) = _live_auth_and_canonical(config_dir, config)
            role_states = [
                role.get("state")
                for provider in canonical["providers"].values()
                for role in provider.get("role_status", {}).values()
                if role.get("enabled")
            ]
            if any(state in {"unconfigured", "degraded", "blocked_external_review"} for state in role_states):
                status = "degraded"
        except Exception as exc:
            auth_error = str(exc)[:200]
            logger.debug("Live auth status unavailable: %s", exc)
            canonical = {"providers": {}, "platforms": {}, "roles": []}

        provider_values = list(canonical.get("providers", {}).values())
        role_values = [
            role
            for provider in provider_values
            for role in provider.get("role_status", {}).values()
            if role.get("enabled")
        ]
        blockers = [
            {
                "platform": role.get("platform"),
                "role": role.get("role"),
                "state": role.get("state"),
                "error": role.get("error"),
            }
            for role in role_values
            if role.get("state") != "ready"
        ]
        destinations = [
            role for role in role_values if role.get("role") == "video_destination"
        ]
        destination = next(
            (role for role in destinations if role.get("state") == "ready"),
            destinations[0] if destinations else None,
        )
        destination_ready = bool(destination and destination.get("state") == "ready")
        next_action = (
            {
                "kind": "create_post",
                "label": "Create a post",
                "role": "video_destination",
            }
            if destination_ready
            else {
                "kind": "connect" if destination and destination.get("state") == "unconfigured" else "review",
                "label": "Connect a video destination" if destination and destination.get("state") == "unconfigured" else "Review readiness",
                "role": destination.get("role") if destination else "video_destination",
            }
        )
        ready = not blockers
        return {
            "status": status,
            "platforms": platforms,
            "total_processed": health.get("total_processed", 0),
            "auth": auth,
            "auth_error": auth_error,
            "auth_cached": auth_cached,
            "auth_age_seconds": auth_age_seconds,
            "auth_stale": auth_stale,
            # Badge truth, straight from the probe above (xpst.token_state):
            # the UI must render `badges[platform]` and `auth_checked_at`,
            # never a green pill derived from credential presence.
            "badges": _badge_summary(auth),
            "auth_checked_at": _auth_checked_at(auth),
            "auth_checked_at_iso": _auth_checked_at_iso(auth),
            "canonical": canonical,
            "providers": canonical["providers"],
            "readiness_pending": False,
            "readiness": {"ready": ready, "blockers": blockers},
            "next_action": next_action,
            "can_create_post": destination_ready,
        }


    @router.get("/providers")
    def api_providers() -> dict[str, Any]:
        from xpst.provider_truth import canonical_provider_catalog

        return canonical_provider_catalog(_load_ui_config())

    @router.post("/refresh-tokens")
    def api_refresh_tokens(force: bool = False) -> dict[str, Any]:
        """Renew due/expiring tokens on demand (bounded retry, no secrets).

        Mirrors ``xpst refresh-tokens``: a no-op when nothing is due, so the
        UI's "Refresh accounts" action is cheap and safe to press. ``force=true``
        renews every platform that has a refresh path, whatever its expiry.
        Failures are reported honestly and the cached probe is invalidated so
        the next read re-checks liveness instead of serving the old answer.
        """
        from xpst.token_refresh import (
            FAST_BASE_DELAY_SECONDS,
            FAST_DEADLINE_SECONDS,
            FAST_MAX_ATTEMPTS,
            refresh_due_tokens,
            save_refresh_report,
        )

        config = _load_ui_config()
        report = refresh_due_tokens(
            config,
            force=force,
            max_attempts=FAST_MAX_ATTEMPTS,
            base_delay=FAST_BASE_DELAY_SECONDS,
            deadline=FAST_DEADLINE_SECONDS,
        )
        save_refresh_report(config, report)
        with _AUTH_STATUS_LOCK:
            _AUTH_STATUS_CACHE.pop(str(config_dir), None)
        return {
            "refreshed": report,
            "attempted": sorted(report),
            "count": len(report),
            "failed": [name for name, item in report.items() if not item.get("ok")],
        }

    @router.get("/schedules")
    def api_schedules() -> dict[str, Any]:
        """Return persisted schedule entries without starting the scheduler."""
        from xpst.schedule_manager import ScheduleManager

        schedules = ScheduleManager(config_dir).list()
        return {"schedules": schedules, "count": len(schedules)}

    @router.get("/activity")
    def api_activity() -> dict[str, Any]:
        """Return recorded failures with targeted, truthful recovery metadata."""
        from xpst.state_store import StateStore

        state = StateStore(config_dir).get()
        failures: list[dict[str, Any]] = []
        for video_id, video in (state.get("posted_videos") or {}).items():
            errors = video.get("errors") or {}
            for platform, result in errors.items():
                if not isinstance(result, dict):
                    continue
                retryable = result.get("retryable")
                failures.append({
                    "video_id": str(video_id),
                    "platform": str(platform),
                    "error": str(result.get("error") or "Unknown error"),
                    "retryable": retryable,
                    "post_id": None,
                    "post_url": None,
                    "source_url": video.get("source_url"),
                    "last_attempt": result.get("timestamp") or video.get("last_attempt"),
                    "action": "retry" if retryable is True else "review",
                })
        failures.sort(
            key=lambda item: (item.get("last_attempt") or "", item["video_id"], item["platform"]),
            reverse=True,
        )
        return {"failures": failures, "count": len(failures)}

    @router.get("/library")
    def api_library() -> dict[str, Any]:
        """Return local library items with only verified platform results."""
        from xpst.state_store import StateStore

        state = StateStore(config_dir).get()
        items: list[dict[str, Any]] = []
        for video_id, video in (state.get("posted_videos") or {}).items():
            posts = []
            for platform, result in (video.get("posted_to") or {}).items():
                if not isinstance(result, dict):
                    continue
                post_id = result.get("id") or result.get("post_id")
                post_url = result.get("url") or result.get("post_url")
                if post_id or post_url:
                    posts.append({"platform": str(platform), "post_id": post_id, "post_url": post_url})
            items.append({
                "video_id": str(video_id),
                "source_url": video.get("source_url"),
                "source_platform": video.get("source_platform"),
                "caption": video.get("caption", ""),
                "last_attempt": video.get("last_attempt"),
                "posts": posts,
            })
        items.sort(key=lambda item: item.get("last_attempt") or "", reverse=True)
        return {"items": items, "count": len(items)}

    @router.post("/preflight", dependencies=[Depends(require_api_token)])
    def api_preflight(payload: dict[str, Any]) -> dict[str, Any]:
        """Run the canonical, side-effect-free post preflight.

        Delegates to ``PostPreflightService`` — the same service the CLI and the
        MCP tool use — so no surface can disagree about whether a post is ready.
        Only the request-shape preconditions (missing media or targets) are
        decided here; every media, caption, destination and **content-type**
        verdict is canonical.
        """
        from xpst.config import XPSTConfig
        from xpst.content import ContentRequest, content_verdict
        from xpst.services.post_preflight import PostPlanRequest, PostPreflightService, plan_content_type

        media_path = str(payload.get("media_path") or "").strip()
        # `text` is the text-post spelling of `caption`; both name the same body.
        caption = str(payload.get("text") or payload.get("caption") or "")
        platforms = [
            str(item).lower()
            for item in (payload.get("platforms") or [])
            if str(item).strip()
        ]

        request_blockers: list[str] = []
        if not platforms:
            # An empty target list would produce an empty plan that reports
            # "ready", so the request-shape precondition is decided here.
            request_blockers.append("Choose at least one destination platform.")

        # One content verdict, from the contract module: the same request gets
        # the same answer here, in the CLI, and over MCP. The payload goes through
        # the one request parser, so `text` (a text post) is read the same way
        # here as over MCP and in the CLI.
        request = ContentRequest.from_payload(payload)
        verdict = content_verdict(request)

        plan: dict[str, Any] | None = None
        canonical_blockers: list[str] = []
        canonical_warnings: list[str] = []
        try:
            config = XPSTConfig.load(str(Path(config_dir).expanduser() / "config.yaml"))
        except Exception as exc:  # noqa: BLE001 - a missing config must not break preflight
            logger.debug("Preflight config load failed, using defaults: %s", exc)
            config = XPSTConfig()
        try:
            plan = PostPreflightService(config).plan(
                PostPlanRequest(
                    media_paths=[media_path] if media_path else [],
                    target_platforms=platforms,
                    base_caption=caption,
                    # A text post carries no file, so the media requirement must
                    # not be applied to it (it would block every text preflight);
                    # a request with neither a file nor a body keeps that
                    # requirement instead of being read as a refused text post.
                    content_type=plan_content_type(request),
                )
            ).to_dict()
            canonical_blockers = [issue["message"] for issue in plan["hard_blockers"]]
            canonical_warnings = [issue["message"] for issue in plan["warnings"]]
        except Exception as exc:  # noqa: BLE001 - report truthfully instead of 500
            logger.warning("Preflight could not run: %s", exc)
            canonical_blockers = [f"Preflight could not run: {str(exc)[:200]}"]

        media = Path(media_path).expanduser() if media_path else None
        blockers = request_blockers + canonical_blockers + verdict["blockers"]
        return {
            "ok": not blockers,
            "ready": not blockers,
            "media": {
                "path": media_path,
                "exists": bool(media and media.exists()),
                "is_file": bool(media and media.is_file()),
            },
            "caption": {
                "length": len(caption),
                "per_platform": {platform: caption for platform in platforms},
            },
            "platforms": platforms,
            "content_type": verdict["content_type"] or verdict["effective_content_type"],
            "effective_content_type": verdict["effective_content_type"],
            "route": verdict["route"],
            "content": verdict,
            "blockers": blockers,
            "warnings": canonical_warnings,
            "plan": plan,
            "network_calls": False,
        }

    @router.get("/capabilities")
    def api_capabilities() -> dict[str, Any]:
        """The canonical capability contract, from its one source.

        The CLI ``capabilities`` command and the MCP ``xpst_capabilities`` tool
        return the same document (``xpst.content.capability_document``), so a
        dashboard, a human and an agent cannot disagree about what xPST can
        publish. No network calls, no secrets.
        """
        from xpst.content import capability_document

        return capability_document()

    @router.get("/settings")
    def api_settings() -> dict[str, Any]:
        from xpst.cli import _mask_sensitive_values

        config = _load_ui_config()

        def _section(obj: Any) -> dict[str, Any]:
            return dict(obj.__dict__) if hasattr(obj, "__dict__") else {}

        return _mask_sensitive_values(
            {
                "accounts": {
                    platform: _section(getattr(config, platform))
                    for platform in ("tiktok", "youtube", "x", "instagram", "threads", "messenger", "local")
                    if hasattr(config, platform)
                },
                "video": _section(config.video),
                "monitoring": _section(config.monitoring),
                "schedule": _section(config.schedule),
                "bio": _section(config.bio),
            }
        )

    return router
