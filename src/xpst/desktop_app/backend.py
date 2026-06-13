"""AppController — Python ↔ QML bridge for xPST desktop app.

Exposes xPST state, config, analytics, and engine actions as
Q_PROPERTY / Q_INVOKABLE slots consumable from QML.  All complex
data is serialised as JSON strings so QML can parse with JSON.parse().
"""

import asyncio
import json
import logging
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from xpst.utils.platform import get_config_dir

# PySide6 is an optional ('desktop'/'pyside6') extra. Import it gracefully so
# that importing this module does not hard-fail when the extra is absent. The
# clear install message is surfaced only when the GUI is actually launched
# (see require_pyside6 / run_desktop_app in main.py).
try:
    from PySide6.QtCore import Property, QObject, QTimer, Signal, Slot
    _PYSIDE6_IMPORT_ERROR: ImportError | None = None
    HAS_PYSIDE6 = True
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    _PYSIDE6_IMPORT_ERROR = exc
    HAS_PYSIDE6 = False

    class QObject:  # type: ignore[no-redef]
        """Minimal stand-in for QObject so this module imports without PySide6."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    class _Signal:
        """Stand-in for a Qt Signal; supports .emit/.connect as no-ops."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def emit(self, *args: Any, **kwargs: Any) -> None:
            pass

        def connect(self, *args: Any, **kwargs: Any) -> None:
            pass

    def Signal(*args: Any, **kwargs: Any) -> _Signal:  # type: ignore[no-redef]
        return _Signal()

    class QTimer:  # type: ignore[no-redef]
        """Stand-in for QTimer (only constructed when the GUI runs)."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.timeout = _Signal()

        def start(self, *args: Any, **kwargs: Any) -> None:
            pass

    def Slot(*args: Any, **kwargs: Any):  # type: ignore[no-redef]
        """Stand-in for the @Slot(...) decorator: returns the function as-is."""

        def decorator(func):
            return func

        return decorator

    class Property:  # type: ignore[no-redef]
        """Stand-in for Qt's Property.

        Supports both call forms used in this module:
        ``Property(int, getter, notify=...)`` and the ``@Property(str, notify=...)``
        decorator form. Behaves as a plain (read-only) descriptor returning the
        getter's value, which is enough for module import; the real GUI requires
        the actual PySide6 package.
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._getter = None
            for a in args:
                if callable(a) and not isinstance(a, type):
                    self._getter = a
            self._setter_kwargs = kwargs

        def __call__(self, func):
            # Decorator form: @Property(str, notify=...)
            self._getter = func
            return self

        def setter(self, func):
            return self

        def __get__(self, obj, objtype=None):
            if obj is None:
                return self
            if self._getter is not None:
                return self._getter(obj)
            return None


def require_pyside6() -> None:
    """Raise a clear, actionable error if the optional PySide6 extra is missing."""
    if not HAS_PYSIDE6:
        raise ModuleNotFoundError(
            "The desktop app requires the optional PySide6 extra. "
            "Install it with: pip install 'xpst[pyside6]'"
        ) from _PYSIDE6_IMPORT_ERROR


from xpst.desktop_app import icon_glyphs

# ── Optional xPST dependencies (graceful fallback) ───────────────────
try:
    from xpst.config import XPSTConfig
except ImportError:
    XPSTConfig = None  # type: ignore[assignment,misc]

try:
    from xpst.state import StateManager
except ImportError:
    StateManager = None  # type: ignore[assignment,misc]

try:
    from xpst.engine import CrossPostEngine
except ImportError:
    CrossPostEngine = None  # type: ignore[assignment,misc]

try:
    from xpst.utils.video import FFmpegNotFoundError
except ImportError:
    FFmpegNotFoundError = None  # type: ignore[assignment,misc]

try:
    from xpst.dashboard.analytics import AnalyticsCollector
except ImportError:
    AnalyticsCollector = None  # type: ignore[assignment,misc]

try:
    from xpst.i18n import get_language as _get_lang
    from xpst.i18n import set_language as _set_lang
    from xpst.i18n import tr
except ImportError:
    tr = None  # type: ignore[assignment]
    _set_lang = None  # type: ignore[assignment]
    _get_lang = None  # type: ignore[assignment]

try:
    from xpst.i18n import get_available_languages as _get_available_langs
except ImportError:
    _get_available_langs = None  # type: ignore[assignment]

try:
    from xpst.utils.quota import QuotaManager
except ImportError:
    QuotaManager = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

PLATFORMS = ("youtube", "instagram", "x", "tiktok")
ACCOUNT_SECTIONS = (*PLATFORMS, "local")


class AppController(QObject):
    """Main controller bridging xPST Python backend to QML frontend."""

    # ── Signals ──────────────────────────────────────────────────────
    dataChanged = Signal()
    postComplete = Signal(str)   # JSON result string
    error = Signal(str)          # error message
    connectResult = Signal(str)  # JSON connect result
    settingsSaved = Signal(bool, str)  # success, message
    progressChanged = Signal(str, float)  # platform, percentage 0-100
    notification = Signal(str, bool)   # message, isError

    # ── Init ─────────────────────────────────────────────────────────

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._config: Any = None
        self._state: Any = None
        self._engine: Any = None
        self._analytics: Any = None
        self._quota: Any = None

        # Cached property values
        self._total_posts: int = 0
        self._total_reach: int = 0
        self._best_platform: str = "—"
        self._posts_this_week: int = 0
        self._platform_health: str = "{}"
        self._recent_posts: str = "[]"
        self._config_data: str = "{}"
        self._analytics_data: str = "{}"
        self._quota_data: str = "{}"

        # Auto-refresh timer (30s)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refreshData)
        self._refresh_timer.start(30_000)

        # Platform health auto-check timer (5 minutes)
        self._health_check_interval = self._get_health_check_interval()
        self._health_timer = QTimer(self)
        self._health_timer.timeout.connect(self._run_health_check)
        self._health_timer.start(self._health_check_interval)

        # Thumbnail cache dir (routed through get_config_dir so Windows uses
        # %APPDATA% rather than ~/.xpst — W3-4).
        self._thumb_dir = get_config_dir() / "thumbnails"
        self._thumb_dir.mkdir(parents=True, exist_ok=True)

        # Wire error signal to notification signal
        self.error.connect(lambda msg: self.notification.emit(msg, True))
        self.postComplete.connect(
            lambda json_str: self.notification.emit("Post completed successfully", False)
        )

        # Initialise backends (best-effort)
        self._init_backends()
        self.refreshData()

    # ── Backend initialisation ───────────────────────────────────────

    def _init_backends(self) -> None:
        """Create backend objects, swallowing errors for missing deps."""
        try:
            if XPSTConfig is not None:
                self._config = XPSTConfig.load()
        except Exception as exc:
            logger.warning("Failed to load XPSTConfig: %s", exc)

        try:
            if StateManager is not None:
                self._state = StateManager()
        except Exception as exc:
            logger.warning("Failed to create StateManager: %s", exc)

        try:
            if QuotaManager is not None:
                self._quota = QuotaManager()
        except Exception as exc:
            logger.warning("Failed to create QuotaManager: %s", exc)

        # Defer AnalyticsCollector - it's heavy and may make network calls
        self._analytics = None
        self._analytics_initialized = False

        # Engine is heavy and async; defer creation
        self._engine = None

    def _get_analytics(self):
        """Lazily initialize AnalyticsCollector on first access."""
        if not self._analytics_initialized:
            self._analytics_initialized = True
            if AnalyticsCollector is not None:
                try:
                    self._analytics = AnalyticsCollector()
                except Exception as exc:
                    logger.warning("Failed to create AnalyticsCollector: %s", exc)
        return self._analytics

    def _ensure_engine(self) -> bool:
        """Lazily initialize the CrossPostEngine. Returns True if ready."""
        if self._engine is not None:
            return True
        if CrossPostEngine is None:
            return False
        if self._config is None:
            return False
        try:
            self._engine = CrossPostEngine(self._config)
            return True
        except Exception as exc:
            # Surface a friendly, actionable first-run message when FFmpeg is
            # missing instead of letting a raw traceback escape (W3-3).
            if FFmpegNotFoundError is not None and isinstance(exc, FFmpegNotFoundError):
                logger.error("FFmpeg not available: %s", exc)
                # self.error is wired to notification in __init__, so a single
                # emit surfaces the friendly message once (no double-notify).
                self.error.emit(str(exc))
            else:
                logger.error("Failed to create CrossPostEngine: %s", exc)
            return False

    # ── Q_PROPERTY definitions ───────────────────────────────────────

    # totalPosts(int)
    def _get_total_posts(self) -> int:
        return self._total_posts

    totalPosts = Property(int, _get_total_posts, notify=dataChanged)

    # totalReach(int)
    def _get_total_reach(self) -> int:
        return self._total_reach

    totalReach = Property(int, _get_total_reach, notify=dataChanged)

    # bestPlatform(str)
    def _get_best_platform(self) -> str:
        return self._best_platform

    bestPlatform = Property(str, _get_best_platform, notify=dataChanged)

    # postsThisWeek(int)
    def _get_posts_this_week(self) -> int:
        return self._posts_this_week

    postsThisWeek = Property(int, _get_posts_this_week, notify=dataChanged)

    # platformHealth(JSON str)
    def _get_platform_health(self) -> str:
        return self._platform_health

    platformHealth = Property(str, _get_platform_health, notify=dataChanged)

    # recentPosts(JSON str)
    def _get_recent_posts(self) -> str:
        return self._recent_posts

    recentPosts = Property(str, _get_recent_posts, notify=dataChanged)

    # configData(JSON str)
    def _get_config_data(self) -> str:
        return self._config_data

    configData = Property(str, _get_config_data, notify=dataChanged)

    # analyticsData(JSON str)
    def _get_analytics_data(self) -> str:
        return self._analytics_data

    analyticsData = Property(str, _get_analytics_data, notify=dataChanged)

    # quotaData(JSON str)
    def _get_quota_data(self) -> str:
        return self._quota_data

    quotaData = Property(str, _get_quota_data, notify=dataChanged)

    # ── Data refresh ─────────────────────────────────────────────────

    @Slot(result=str)
    def refreshData(self) -> str:
        """Reload all cached data from backends and emit dataChanged."""
        try:
            self._refresh_state()
            self._refresh_platform_health()
            self._refresh_recent_posts()
            self._refresh_config()
            self._refresh_analytics()
            self._refresh_quota()
            self.dataChanged.emit()
            return json.dumps({"ok": True})
        except Exception as exc:
            logger.error("refreshData failed: %s", exc)
            self.error.emit(str(exc))
            return json.dumps({"ok": False, "error": str(exc)})

    def _refresh_state(self) -> None:
        """Update summary counters from StateManager."""
        if self._state is None:
            return

        try:
            state = self._state._state
            posted = state.get("posted_videos", {})
            self._total_posts = len(posted)

            # Count unique platform posts for reach
            total_platform_posts = 0
            platform_counts: dict[str, int] = {}
            for _vid, vdata in posted.items():
                for platform in vdata.get("posted_to", {}):
                    total_platform_posts += 1
                    platform_counts[platform] = platform_counts.get(platform, 0) + 1

            self._total_reach = total_platform_posts

            # Best platform: prefer analytics collector engagement data, fallback to post count
            if self._analytics is not None:
                try:
                    analytics_summary = self._analytics.get_summary_stats()
                    analytics_best = analytics_summary.get("best_platform", "")
                    if analytics_best and analytics_best != "—":
                        self._best_platform = analytics_best
                    elif platform_counts:
                        self._best_platform = max(platform_counts, key=platform_counts.get)  # type: ignore[arg-type]
                    else:
                        self._best_platform = "—"
                except Exception:
                    if platform_counts:
                        self._best_platform = max(platform_counts, key=platform_counts.get)  # type: ignore[arg-type]
                    else:
                        self._best_platform = "—"
            elif platform_counts:
                self._best_platform = max(platform_counts, key=platform_counts.get)  # type: ignore[arg-type]
            else:
                self._best_platform = "—"

            # Posts this week
            cutoff = datetime.now() - timedelta(days=7)
            week_count = 0
            for _vid, vdata in posted.items():
                for _plat, pinfo in vdata.get("posted_to", {}).items():
                    ts_str = pinfo.get("timestamp") or vdata.get("downloaded_at")
                    if ts_str:
                        try:
                            ts = datetime.fromisoformat(ts_str)
                            if ts >= cutoff:
                                week_count += 1
                        except (ValueError, TypeError):
                            pass
            self._posts_this_week = week_count
        except Exception as exc:
            logger.warning("State refresh error: %s", exc)

    def _refresh_platform_health(self) -> None:
        """Build platform health dict from state + quota."""
        health: dict[str, Any] = {}

        for plat in PLATFORMS:
            info: dict[str, Any] = {
                "name": plat,
                "label": plat.capitalize(),
                "status": "unknown",
                "failures": 0,
                "circuit_breaker_open": False,
                "last_success": None,
                "can_upload": True,
                "quota_remaining": None,
            }

            # StateManager health
            if self._state is not None:
                try:
                    ph = self._state.get_platform_health(plat)
                    info["status"] = ph.get("status", "unknown")
                    info["failures"] = ph.get("failures", 0)
                    info["circuit_breaker_open"] = ph.get("circuit_breaker_open", False)
                    info["last_success"] = ph.get("last_success")
                except Exception:
                    pass

            # QuotaManager
            if self._quota is not None:
                try:
                    info["can_upload"] = self._quota.can_upload(plat)
                    remaining = self._quota.get_remaining(plat)
                    info["quota_remaining"] = remaining.get("daily")
                except Exception:
                    pass

            # Config enabled check
            if self._config is not None:
                try:
                    acct = getattr(self._config, plat, None)
                    if acct is not None:
                        info["enabled"] = getattr(acct, "enabled", True)
                    else:
                        info["enabled"] = True
                except Exception:
                    info["enabled"] = True

            health[plat] = info

        self._platform_health = json.dumps(health, default=str)

    def _refresh_recent_posts(self) -> None:
        """Build recent posts list from StateManager."""
        if self._state is None:
            self._recent_posts = "[]"
            return

        try:
            posted = self._state._state.get("posted_videos", {})
            posts: list[dict[str, Any]] = []

            for video_id, vdata in posted.items():
                caption = (vdata.get("caption") or "")[:120]
                downloaded_at = vdata.get("downloaded_at") or ""
                thumbnail_path = vdata.get("thumbnail") or ""

                for platform, pinfo in vdata.get("posted_to", {}).items():
                    posts.append({
                        "title": video_id,
                        "caption": caption,
                        "platform": platform,
                        "status": "posted",
                        "timestamp": pinfo.get("timestamp") or downloaded_at,
                        "postId": pinfo.get("id") or video_id,
                        "url": pinfo.get("url", ""),
                        "thumbnail": thumbnail_path,
                    })

            # Sort newest first, limit to 50
            posts.sort(key=lambda p: p.get("timestamp", ""), reverse=True)
            self._recent_posts = json.dumps(posts[:50], default=str)
        except Exception as exc:
            logger.warning("Recent posts refresh error: %s", exc)
            self._recent_posts = "[]"

    def _refresh_config(self) -> None:
        """Serialize current config to JSON for QML settings panel."""
        if self._config is None:
            self._config_data = json.dumps({"error": "Config not loaded"})
            return

        try:
            cfg: dict[str, Any] = {}
            for plat in PLATFORMS:
                acct = getattr(self._config, plat, None)
                if acct is not None:
                    cfg[plat] = {
                        "enabled": getattr(acct, "enabled", True),
                        "username": getattr(acct, "username", ""),
                    }

            # Rate limits
            if hasattr(self._config, "rate_limits"):
                rl = self._config.rate_limits
                cfg["rate_limits"] = {
                    "youtube": getattr(rl, "youtube", 5),
                    "instagram": getattr(rl, "instagram", 5),
                    "x": getattr(rl, "x", 5),
                    "tiktok": getattr(rl, "tiktok", 5),
                }

            # Monitoring
            if hasattr(self._config, "monitoring"):
                mon = self._config.monitoring
                cfg["monitoring"] = {
                    "log_level": getattr(mon, "log_level", "INFO"),
                }

            self._config_data = json.dumps(cfg, default=str)
        except Exception as exc:
            logger.warning("Config refresh error: %s", exc)
            self._config_data = json.dumps({"error": str(exc)})

    def _refresh_analytics(self) -> None:
        """Build the QML analytics payload from PERSISTED data only.

        Never touches the network: the periodic refresh timer runs on the
        GUI thread, and the old implementation live-fetched there — freezing
        the UI and hammering platform APIs every 30s (G20). Live refresh is
        an explicit user action via refreshAnalyticsLive().
        """
        analytics = self._get_analytics()
        if analytics is None:
            self._analytics_data = json.dumps({"available": False})
            return

        try:
            payload = analytics.get_analytics_payload(live=False)
            payload["health"] = analytics.get_platform_health_all()
            self._analytics_data = json.dumps(payload, default=str)
        except Exception as exc:
            logger.warning("Analytics refresh error: %s", exc)
            self._analytics_data = json.dumps({"available": False, "error": str(exc)})

    @Slot()
    def refreshAnalyticsLive(self) -> None:
        """Refresh analytics from the platform APIs on a worker thread (G20),
        then publish the new payload via dataChanged."""
        analytics = self._get_analytics()
        if analytics is None or getattr(self, "_analytics_live_inflight", False):
            return
        self._analytics_live_inflight = True

        def _work() -> None:
            try:
                payload = analytics.get_analytics_payload(live=True)
                payload["health"] = analytics.get_platform_health_all()
                self._analytics_data = json.dumps(payload, default=str)
            except Exception as exc:
                logger.warning("Live analytics refresh failed: %s", exc)
            finally:
                self._analytics_live_inflight = False
                # Cross-thread signal emission is queued by Qt — safe here.
                self.dataChanged.emit()

        threading.Thread(target=_work, name="xpst-analytics-live", daemon=True).start()

    def _refresh_quota(self) -> None:
        """Serialize quota status for QML."""
        if self._quota is None:
            self._quota_data = json.dumps({"available": False})
            return

        try:
            status = self._quota.get_status()
            self._quota_data = json.dumps(status, default=str)
        except Exception as exc:
            logger.warning("Quota refresh error: %s", exc)
            self._quota_data = json.dumps({"available": False, "error": str(exc)})

    # ── Q_INVOKABLE slots ────────────────────────────────────────────

    @Slot(str, str, str, result=str)
    def previewPost(self, video_path: str, caption: str, platforms_json: str = "") -> str:
        """Return a no-network preflight preview for a manual post."""
        try:
            config = self._config or (XPSTConfig.load() if XPSTConfig is not None else None)
            if config is None:
                return json.dumps({"ok": False, "ready": False, "blocking": ["Config not loaded"], "warnings": []})

            from xpst.platforms.base import PlatformRegistry

            requested_platforms: list[str] = []
            if platforms_json:
                try:
                    parsed = json.loads(platforms_json)
                    if isinstance(parsed, list):
                        requested_platforms = [str(item).lower() for item in parsed if str(item).strip()]
                except json.JSONDecodeError:
                    requested_platforms = [
                        item.strip().lower()
                        for item in platforms_json.split(",")
                        if item.strip()
                    ]

            PlatformRegistry.auto_discover()
            manifests = {
                manifest.name: manifest
                for manifest in PlatformRegistry.list_manifests(config)
            }
            enabled_platforms = [
                name
                for name in ("youtube", "instagram", "x")
                if getattr(getattr(config, name), "enabled", False)
            ]
            targets = requested_platforms or enabled_platforms

            path = Path(video_path).expanduser()
            blocking: list[str] = []
            warnings: list[str] = []
            if not video_path.strip():
                blocking.append("Choose a video file before posting.")
            elif not path.exists():
                blocking.append(f"Video file not found: {video_path}")
            elif not path.is_file():
                blocking.append(f"Video path is not a file: {video_path}")

            supported_suffixes = {".mp4", ".mov", ".m4v", ".webm"}
            if path.suffix.lower() and path.suffix.lower() not in supported_suffixes:
                warnings.append(f"{path.suffix.lower()} may need conversion before upload.")

            if not caption.strip():
                warnings.append("Caption is empty.")

            if not targets:
                blocking.append("Enable at least one destination platform.")

            platform_previews: list[dict[str, Any]] = []
            for platform_name in targets:
                if platform_name not in manifests:
                    blocking.append(f"{platform_name} is not an installed destination provider.")
                    continue

                account = getattr(config, platform_name, None)
                enabled = bool(getattr(account, "enabled", False))
                manifest = manifests[platform_name]
                extra = manifest.extra
                platform_warnings: list[str] = []
                platform_blocking: list[str] = []

                if not enabled:
                    platform_blocking.append(f"{manifest.display_name} is disabled.")

                credential_path = AppController._credential_path_for_platform(config, platform_name)
                if credential_path and not Path(credential_path).expanduser().exists():
                    platform_blocking.append(f"{manifest.display_name} is not connected.")

                max_caption = extra.get("max_caption_length")
                if isinstance(max_caption, int) and len(caption) > max_caption:
                    platform_blocking.append(
                        f"{manifest.display_name} caption is {len(caption) - max_caption} character(s) too long."
                    )

                quota = getattr(self, "_quota", None)
                if quota is not None:
                    try:
                        if not quota.can_upload(platform_name):
                            platform_blocking.append(f"{manifest.display_name} daily quota is exhausted.")
                    except Exception:
                        platform_warnings.append(f"{manifest.display_name} quota could not be checked.")

                platform_previews.append(
                    {
                        "name": platform_name,
                        "display_name": manifest.display_name,
                        "auth_mode": manifest.auth_mode.value,
                        "official": manifest.is_official_api,
                        "max_caption_length": max_caption,
                        "caption_length": len(caption),
                        "blocking": platform_blocking,
                        "warnings": platform_warnings,
                        "ready": not platform_blocking,
                    }
                )
                blocking.extend(platform_blocking)
                warnings.extend(platform_warnings)

            file_size = path.stat().st_size if path.exists() and path.is_file() else 0
            return json.dumps(
                {
                    "ok": True,
                    "ready": not blocking,
                    "video": {
                        "path": str(path),
                        "filename": path.name,
                        "size_bytes": file_size,
                        "size_mb": round(file_size / (1024 * 1024), 2) if file_size else 0,
                    },
                    "caption": caption,
                    "caption_length": len(caption),
                    "platforms": platform_previews,
                    "blocking": blocking,
                    "warnings": warnings,
                },
                default=str,
            )
        except Exception as exc:
            logger.error("previewPost error: %s", exc)
            return json.dumps({"ok": False, "ready": False, "blocking": [str(exc)], "warnings": []})

    @staticmethod
    def _credential_path_for_platform(config: Any, platform_name: str) -> str:
        account = getattr(config, platform_name, None)
        if account is None:
            return ""
        if platform_name == "youtube":
            return str(getattr(account, "client_secrets", "") or "")
        if platform_name == "x":
            return str(getattr(account, "cookies_file", "") or "")
        if platform_name == "instagram":
            return str(getattr(account, "session_file", "") or "")
        return ""

    @Slot(str, str)
    def postVideo(self, video_path: str, caption: str) -> None:
        """Post a video file to all enabled platforms.

        Runs the upload pipeline in a background thread to avoid
        blocking the UI. Emits postComplete with JSON results or
        error signal on failure.

        Args:
            video_path: Absolute path to the video file.
            caption: Caption/title text for the post.
        """
        preview = json.loads(self.previewPost(video_path, caption, ""))
        if not preview.get("ready"):
            blocking = preview.get("blocking") or ["Post is not ready."]
            self.error.emit(str(blocking[0]))
            return

        if not self._ensure_engine():
            self.error.emit("CrossPostEngine not available")
            return

        path = Path(video_path).expanduser()

        def _run() -> None:
            try:
                # Emit 0% progress for all platforms at start
                for plat in ("youtube", "instagram", "x", "tiktok"):
                    self.progressChanged.emit(plat, 0.0)

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                result = loop.run_until_complete(
                    self._engine.post_manual(path, caption)
                )
                loop.close()

                # Emit per-platform progress from results
                total_platforms = len(result.results) or 1
                for idx, (plat, _upload_result) in enumerate(result.results.items()):
                    pct = ((idx + 1) / total_platforms) * 100.0
                    self.progressChanged.emit(plat, pct)

                result_dict = {
                    "video_id": result.video_id,
                    "caption": result.caption,
                    "all_success": result.all_success,
                    "partial_success": result.partial_success,
                    "platforms": {
                        plat: {
                            "success": ur.success,
                            "error": ur.error,
                            "post_url": ur.post_url,
                        }
                        for plat, ur in result.results.items()
                    },
                }

                # Mark all as complete
                for plat in result.results:
                    self.progressChanged.emit(plat, 100.0)

                result_json = json.dumps(result_dict, default=str)
                self.postComplete.emit(result_json)
                self.refreshData()
            except Exception as exc:
                logger.error("postVideo error: %s", exc)
                self.error.emit(str(exc))

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    @Slot(str, str)
    def deletePost(self, post_id: str, platform: str) -> None:
        """Delete a post from a platform and update state.

        Attempts to delete the post on the actual platform via the
        engine's delete_post() method. If the engine is not available,
        falls back to removing the state entry only. Runs async platform
        deletion in a background thread.

        Args:
            post_id: Video/post identifier.
            platform: Platform name (youtube, instagram, x, tiktok).
        """
        if self._state is None:
            self.error.emit("StateManager not available")
            return

        def _run() -> None:
            try:
                deleted_from_platform = False

                # Try to delete from the actual platform via engine
                if self._ensure_engine():
                    try:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        deleted_from_platform = loop.run_until_complete(
                            self._engine.delete_post(post_id, platform)
                        )
                        loop.close()
                    except Exception as exc:
                        logger.warning(
                            "Platform delete failed for %s/%s: %s",
                            post_id, platform, exc,
                        )

                # Remove from local state regardless
                posted = self._state._state.get("posted_videos", {})
                if post_id in posted:
                    posted_to = posted[post_id].get("posted_to", {})
                    if platform in posted_to:
                        del posted_to[platform]
                        logger.info(
                            "Removed %s/%s from state (platform_delete=%s)",
                            post_id, platform, deleted_from_platform,
                        )

                    # If no platforms left, remove the entire video entry
                    if not posted_to:
                        del posted[post_id]
                        logger.info("Removed video entry %s (no platforms left)", post_id)

                    self._state.save()

                self.refreshData()

                result = json.dumps({
                    "ok": True,
                    "removed": f"{post_id}/{platform}",
                    "platform_deleted": deleted_from_platform,
                }, default=str)
                self.postComplete.emit(result)
            except Exception as exc:
                logger.error("deletePost error: %s", exc)
                self.error.emit(str(exc))

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    @Slot(str, result=str)
    def saveSettings(self, settings_json: str) -> str:
        """Persist settings from QML. Accepts JSON string, returns status JSON.

        Parses the JSON, merges into the existing YAML config file,
        reloads XPSTConfig, and refreshes all cached data.

        Args:
            settings_json: JSON string with settings to merge.

        Returns:
            JSON string with {"ok": true} or {"ok": false, "error": "..."}.
        """
        try:
            settings = json.loads(settings_json)
        except json.JSONDecodeError as exc:
            return json.dumps({"ok": False, "error": f"Invalid JSON: {exc}"})

        if self._config is None:
            return json.dumps({"ok": False, "error": "Config not loaded"})

        try:
            from pathlib import Path

            import yaml

            config_path = Path(getattr(self._config, "config_dir", "~/.xpst")).expanduser() / "config.yaml"
            config_path.parent.mkdir(parents=True, exist_ok=True)

            # Load existing YAML config
            existing: dict[str, Any] = {}
            if config_path.exists():
                with open(config_path) as f:
                    existing = yaml.safe_load(f) or {}

            # Merge QML settings into the config dict
            if "accounts" not in existing:
                existing["accounts"] = {}

            for plat in ACCOUNT_SECTIONS:
                if plat in settings:
                    if plat not in existing["accounts"]:
                        existing["accounts"][plat] = {}
                    plat_settings = settings[plat]
                    if isinstance(plat_settings, dict):
                        for k, v in plat_settings.items():
                            existing["accounts"][plat][k] = v

            if "rate_limits" in settings:
                existing["rate_limits"] = settings["rate_limits"]

            if "monitoring" in settings:
                existing.setdefault("monitoring", {}).update(settings["monitoring"])

            if "video" in settings:
                existing.setdefault("video", {}).update(settings["video"])

            if "reliability" in settings:
                existing.setdefault("reliability", {}).update(settings["reliability"])

            if "notifications" in settings:
                existing.setdefault("notifications", {}).update(settings["notifications"])

            if "schedule" in settings:
                existing.setdefault("schedule", {}).update(settings["schedule"])

            # Write back
            with open(config_path, "w") as f:
                yaml.dump(existing, f, default_flow_style=False, sort_keys=False)

            # Reload config
            self._config = XPSTConfig.load(str(config_path))
            # Reset engine so it picks up new config on next use
            self._engine = None
            self.refreshData()

            if hasattr(self, "settingsSaved"):
                self.settingsSaved.emit(True, "Settings saved successfully")
            return json.dumps({"ok": True})
        except Exception as exc:
            logger.error("saveSettings error: %s", exc)
            if hasattr(self, "settingsSaved"):
                self.settingsSaved.emit(False, str(exc))
            return json.dumps({"ok": False, "error": str(exc)})

    @Slot(str, result=str)
    def saveOnboarding(self, onboarding_json: str) -> str:
        """Persist first-run source/destination choices and return readiness."""
        try:
            data = json.loads(onboarding_json)
        except json.JSONDecodeError as exc:
            return json.dumps({"ok": False, "error": f"Invalid JSON: {exc}"})

        if self._config is None:
            return json.dumps({"ok": False, "error": "Config not loaded"})

        try:
            if isinstance(data.get("source"), dict):
                source = data["source"]
                source_type = str(source.get("type", "")).lower()
                if source_type == "local":
                    self._config.local.path = str(source.get("path", "")).strip()
                elif source_type == "tiktok":
                    self._config.tiktok.username = str(source.get("username", "")).strip()

            if isinstance(data.get("local"), dict):
                self._config.local.path = str(data["local"].get("path", self._config.local.path)).strip()

            if isinstance(data.get("tiktok"), dict):
                self._config.tiktok.username = str(data["tiktok"].get("username", self._config.tiktok.username)).strip()

            if isinstance(data.get("destinations"), dict):
                for name in ("youtube", "instagram", "x"):
                    if name in data["destinations"]:
                        getattr(self._config, name).enabled = bool(data["destinations"][name])

            from xpst.readiness import repair_local_setup

            repair_result = repair_local_setup(self._config)
            self._config = XPSTConfig.load(str(Path(self._config.config_dir).expanduser() / "config.yaml"))
            self._engine = None
            self.refreshData()

            if hasattr(self, "settingsSaved"):
                self.settingsSaved.emit(True, "Onboarding saved")
            return json.dumps(
                {
                    "ok": True,
                    "actions": repair_result.get("actions", []),
                    "readiness": repair_result["readiness"],
                },
                default=str,
            )
        except Exception as exc:
            logger.error("saveOnboarding error: %s", exc)
            if hasattr(self, "settingsSaved"):
                self.settingsSaved.emit(False, str(exc))
            return json.dumps({"ok": False, "error": str(exc)})

    @Slot(str)
    def connectPlatform(self, platform: str) -> None:
        """Test connectivity / authenticate a platform.

        Runs in a background thread to avoid blocking the UI.
        Emits postComplete with JSON result containing ok/details.

        Args:
            platform: Platform name to connect/test.
        """
        def _run() -> None:
            try:
                # First try running 'xpst auth <platform>' via subprocess
                # This handles OAuth flows etc.
                try:
                    result = subprocess.run(
                        [sys.executable, "-m", "xpst", "auth", platform],
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    if result.returncode == 0:
                        self.connectResult.emit(json.dumps({
                            "ok": True,
                            "platform": platform,
                            "message": f"Authenticated with {platform}",
                            "output": result.stdout.strip(),
                        }))
                        return
                except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
                    logger.debug("Auth subprocess failed: %s, trying engine", exc)

                # Fallback: use engine check_health for connectivity test
                if self._ensure_engine():
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    health = loop.run_until_complete(self._engine.check_health())
                    loop.close()

                    plat_health = health.get("platforms", {}).get(platform, {})
                    ok = plat_health.get("authenticated", False)

                    self.connectResult.emit(json.dumps({
                        "ok": ok,
                        "platform": platform,
                        "details": plat_health,
                    }, default=str))
                else:
                    self.connectResult.emit(json.dumps({
                        "ok": False,
                        "platform": platform,
                        "error": "Engine not available and auth subprocess failed",
                    }))

            except Exception as exc:
                logger.error("connectPlatform(%s) error: %s", platform, exc)
                self.error.emit(str(exc))

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    @Slot(str, result=str)
    def getAnalytics(self, platform: str = "") -> str:
        """Get analytics data for a specific platform or all platforms.

        Uses AnalyticsCollector to gather real engagement metrics from
        platform APIs, falling back to state-based counts.

        Args:
            platform: Platform name (empty string = all platforms).

        Returns:
            JSON string with views/likes/comments/shares per platform.
        """
        analytics = self._get_analytics()
        if analytics is None:
            return json.dumps({
                "available": False,
                "error": "AnalyticsCollector not initialized",
            })

        try:
            data: dict[str, Any] = {"available": True}

            # Get engagement data (uses real APIs where possible)
            engagement = analytics.get_engagement_data()

            if platform and platform in engagement:
                data["platform"] = platform
                data["metrics"] = engagement[platform]
            elif platform:
                data["error"] = f"Unknown platform: {platform}"
                data["metrics"] = {}
            else:
                data["platforms"] = engagement

            # Add summary stats
            data["summary"] = analytics.get_summary_stats()

            return json.dumps(data, default=str)
        except Exception as exc:
            logger.error("getAnalytics error: %s", exc)
            return json.dumps({
                "available": False,
                "error": str(exc),
            })

    @Slot()
    def runPost(self) -> None:
        """Trigger a cross-post cycle via CrossPostEngine.

        Runs check_and_post() in a background thread. Emits
        postComplete with JSON results when done, or error on failure.
        """
        if not self._ensure_engine():
            self.error.emit("CrossPostEngine not available")
            return

        def _run() -> None:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                results = loop.run_until_complete(
                    self._engine.check_and_post()
                )
                loop.close()

                result_dicts = []
                for r in results:
                    result_dicts.append({
                        "video_id": r.video_id,
                        "all_success": r.all_success,
                        "partial_success": r.partial_success,
                        "platforms": {
                            plat: {"success": ur.success, "error": ur.error}
                            for plat, ur in r.results.items()
                        },
                    })

                result_json = json.dumps(result_dicts, default=str)
                # Signal must be emitted on the main thread
                self.postComplete.emit(result_json)
                self.refreshData()
            except Exception as exc:
                logger.error("runPost error: %s", exc)
                self.error.emit(str(exc))

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    @Slot(result=str)
    def getHealth(self) -> str:
        """Return platform health as JSON.

        If the engine is available, performs a live health check
        (connectivity test per platform) via check_health(). Otherwise
        returns cached state-based health data.

        Returns:
            JSON string with per-platform health status.
        """
        if self._ensure_engine():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                health = loop.run_until_complete(self._engine.check_health())
                loop.close()
                return json.dumps(health, default=str)
            except Exception as exc:
                logger.warning("Live health check failed, using cached: %s", exc)

        # Fallback: return cached platform health
        self._refresh_platform_health()
        return self._platform_health

    @Slot(result=str)
    def getStatus(self) -> str:
        """Return overall system status as JSON."""
        status: dict[str, Any] = {
            "totalPosts": self._total_posts,
            "totalReach": self._total_reach,
            "bestPlatform": self._best_platform,
            "postsThisWeek": self._posts_this_week,
            "configLoaded": self._config is not None,
            "stateLoaded": self._state is not None,
            "engineAvailable": CrossPostEngine is not None,
            "analyticsAvailable": self._get_analytics() is not None,
            "quotaAvailable": self._quota is not None,
            "timestamp": datetime.now().isoformat(),
        }
        return json.dumps(status, default=str)

    @Slot(str, result=str)
    def saveShortcuts(self, shortcuts_json: str) -> str:
        """Save custom keyboard shortcuts to config.

        Args:
            shortcuts_json: JSON string mapping action names to key bindings.

        Returns:
            JSON string with ok status.
        """
        try:
            shortcuts = json.loads(shortcuts_json)
            if not isinstance(shortcuts, dict):
                return json.dumps({"ok": False, "error": "Expected a JSON object"})
            if self._config is not None:
                self._config._shortcuts = shortcuts
                self._config.save()
            return json.dumps({"ok": True})
        except Exception as exc:
            logger.error("saveShortcuts failed: %s", exc)
            return json.dumps({"ok": False, "error": str(exc)})

    @Slot(result=str)
    def getShortcuts(self) -> str:
        """Return current keyboard shortcuts as JSON.

        Returns:
            JSON string mapping action names to key bindings.
        """
        if self._config is not None and hasattr(self._config, "_shortcuts"):
            return json.dumps(self._config._shortcuts)
        return json.dumps({
            "dashboard": "Ctrl+1",
            "content": "Ctrl+2",
            "analytics": "Ctrl+3",
            "connect": "Ctrl+4",
            "schedule": "Ctrl+5",
            "refresh": "Ctrl+R",
            "quit": "Ctrl+Q",
        })

    @Slot(result=str)
    def checkForUpdates(self) -> str:
        """Check app, package, helper, and provider metadata update status."""
        try:
            from xpst.updater import check_update_components

            status = check_update_components(include_network=False)
            updatable_count = sum(
                1
                for items in status.values()
                for item in items
                if item.get("updatable")
            )
            return json.dumps({
                "ok": True,
                "status": status,
                "updatable_count": updatable_count,
            }, default=str)
        except Exception as exc:
            logger.error("checkForUpdates error: %s", exc)
            return json.dumps({
                "ok": False,
                "error": str(exc),
            })

    @Slot(result=str)
    def getProviders(self) -> str:
        """Return supported source and destination providers as JSON."""
        try:
            from xpst.platforms.base import PlatformRegistry
            from xpst.sources.base import SourceRegistry

            config = self._config or (XPSTConfig.load() if XPSTConfig is not None else None)
            if config is None:
                return json.dumps({"ok": False, "error": "Config not loaded"})

            SourceRegistry.auto_discover()
            PlatformRegistry.auto_discover()
            sources = SourceRegistry.list_manifests(config)
            destinations = PlatformRegistry.list_manifests(config)
            return json.dumps(
                {
                    "ok": True,
                    "sources": [
                        manifest.to_dict()
                        for manifest in sorted(sources, key=lambda item: item.name)
                    ],
                    "destinations": [
                        manifest.to_dict()
                        for manifest in sorted(destinations, key=lambda item: item.name)
                    ],
                },
                default=str,
            )
        except Exception as exc:
            logger.error("getProviders error: %s", exc)
            return json.dumps({"ok": False, "error": str(exc)})

    @Slot(result=str)
    def getReadiness(self) -> str:
        """Return first-run readiness and next actions as JSON."""
        try:
            from xpst.readiness import build_readiness_report

            report = build_readiness_report(self._config)
            return json.dumps({"ok": True, "readiness": report.to_dict()}, default=str)
        except Exception as exc:
            logger.error("getReadiness error: %s", exc)
            return json.dumps({"ok": False, "error": str(exc)})

    @Slot(result=str)
    def repairReadiness(self) -> str:
        """Repair local setup folders and return updated readiness."""
        try:
            from xpst.readiness import repair_local_setup

            if self._config is None:
                return json.dumps({"ok": False, "error": "Config not loaded"})
            result = repair_local_setup(self._config)
            self._config = XPSTConfig.load()
            self._engine = None
            self.refreshData()
            return json.dumps(result, default=str)
        except Exception as exc:
            logger.error("repairReadiness error: %s", exc)
            return json.dumps({"ok": False, "error": str(exc)})

    @Slot(str, result=str)
    def getThumbnail(self, video_path: str) -> str:
        """Generate or retrieve a cached thumbnail for a video file/URL.

        Returns a file:// URL to the thumbnail image, or empty string on failure.
        """
        import hashlib
        import shutil

        if not video_path:
            return ""

        # Create a deterministic cache key from the path
        cache_key = hashlib.md5(video_path.encode()).hexdigest()
        thumb_path = self._thumb_dir / f"{cache_key}.jpg"

        # Return cached thumbnail if it exists
        if thumb_path.exists() and thumb_path.stat().st_size > 0:
            return thumb_path.as_uri()

        # Check if ffmpeg is available
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return ""

        # If it's a local file, try to extract a frame
        source = video_path
        if video_path.startswith("file://"):
            source = video_path[7:]

        try:
            if Path(source).is_file():
                subprocess.run(
                    [ffmpeg, "-i", source, "-ss", "00:00:01", "-vframes", "1",
                     "-vf", "scale=320:-1", "-y", str(thumb_path)],
                    capture_output=True, timeout=10
                )
                if thumb_path.exists() and thumb_path.stat().st_size > 0:
                    return thumb_path.as_uri()
        except Exception as exc:
            logger.debug("Thumbnail extraction failed for %s: %s", video_path, exc)

        return ""

    @Slot(str)
    def connectPlatformAsync(self, platform: str) -> None:
        """Async wrapper that emits connectResult signal."""
        import threading

        def _run():
            self.connectPlatform(platform)
            # connectPlatform already emits connectResult internally
            # This method exists for QML to call explicitly

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    @Slot(str, str, str)
    def updateCaption(self, post_id: str, platform: str, new_caption: str) -> None:
        """Update caption for a specific post/platform in the post model.

        Args:
            post_id: The post identifier.
            platform: The platform name.
            new_caption: The new caption text.
        """
        # This is called from QML; the actual model update happens in main.py
        # where we connect this signal.  Emit a notification on success.
        self._pending_caption_update = (post_id, platform, new_caption)
        self._captionUpdateReady.emit(post_id, platform, new_caption)

    _captionUpdateReady = Signal(str, str, str)

    @Slot(result=str)
    def getGitLog(self) -> str:
        """Return the last 10 git commits as a JSON string.

        Returns:
            JSON string with list of {hash, message} objects.
        """
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "-10"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(Path(__file__).resolve().parent.parent.parent.parent),
            )
            commits = []
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if not line.strip():
                        continue
                    parts = line.split(" ", 1)
                    commits.append({
                        "hash": parts[0] if parts else "",
                        "message": parts[1] if len(parts) > 1 else "",
                    })
            return json.dumps({"ok": True, "commits": commits}, default=str)
        except Exception as exc:
            logger.debug("getGitLog error: %s", exc)
            return json.dumps({"ok": False, "commits": [], "error": str(exc)})

    # ── Platform health auto-check (#15) ─────────────────────────────

    def _get_health_check_interval(self) -> int:
        """Return healthCheckInterval from config (default 5 min = 300000ms)."""
        try:
            if self._config is not None:
                mon = getattr(self._config, "monitoring", None)
                if mon is not None and hasattr(mon, "health_check_interval"):
                    return int(getattr(mon, "health_check_interval", 300)) * 1000
        except Exception:
            pass
        return 300_000  # 5 minutes

    def _run_health_check(self) -> None:
        """Run platform health check in a background thread."""
        def _check() -> None:
            try:
                self._refresh_platform_health()
                self.dataChanged.emit()
            except Exception as exc:
                logger.warning("Background health check failed: %s", exc)

        t = threading.Thread(target=_check, daemon=True)
        t.start()

    # ── i18n language switcher (#16) ─────────────────────────────────

    @Slot(str)
    def setLanguage(self, lang: str) -> None:
        """Switch the application language and reload translations.

        Args:
            lang: Language code (e.g. 'en', 'es', 'fr').
        """
        if _set_lang is not None:
            try:
                _set_lang(lang)
                logger.info("Language switched to '%s'", lang)
                self.dataChanged.emit()
            except Exception as exc:
                logger.error("setLanguage error: %s", exc)
                self.error.emit(f"Failed to set language: {exc}")

    @Slot(result=str)
    def getAvailableLanguages(self) -> str:
        """Return JSON array of available language codes.

        Returns:
            JSON string like '["en", "es", "fr"]'.
        """
        if _get_available_langs is not None:
            try:
                langs = _get_available_langs()
                return json.dumps(langs)
            except Exception as exc:
                logger.warning("getAvailableLanguages error: %s", exc)

        # Fallback: scan translations dir
        try:
            from pathlib import Path as _Path
            translations_dir = _Path("~/.xpst/translations").expanduser()
            bundled_dir = _Path(__file__).resolve().parent.parent / "i18n"

            langs: list[str] = []
            for d in (translations_dir, bundled_dir):
                if d.exists():
                    for f in d.glob("*.json"):
                        code = f.stem
                        if code not in langs:
                            langs.append(code)
            if not langs:
                langs = ["en"]
            return json.dumps(sorted(langs))
        except Exception as exc:
            logger.warning("Fallback getAvailableLanguages error: %s", exc)
            return json.dumps(["en"])

    # ── Config validation on startup (#27) ───────────────────────────

    @Slot(result=str)
    def validateConfig(self) -> str:
        """Validate the current configuration and return issues as JSON.

        Returns:
            JSON string: {"valid": bool, "warnings": [...], "errors": [...]}
        """
        warnings: list[str] = []
        errors: list[str] = []

        if self._config is None:
            errors.append("Configuration not loaded")
            return json.dumps({"valid": False, "warnings": warnings, "errors": errors})

        try:
            # Check encoding configs
            for name in ("youtube", "instagram", "x"):
                enc = getattr(self._config.video, f"encoding_{name}", None)
                if enc is not None:
                    if enc.resolution and enc.resolution not in (360, 480, 720, 1080, 1440, 1920, 2160):
                        errors.append(f"Invalid resolution for {name}: {enc.resolution}")
                    if enc.crf is not None and not (0 <= enc.crf <= 51):
                        errors.append(f"Invalid CRF for {name}: {enc.crf}")
                    if enc.fps and enc.fps not in (24, 25, 30, 60):
                        errors.append(f"Invalid FPS for {name}: {enc.fps}")

            # Check schedule config
            if self._config.schedule.check_interval < 60:
                errors.append("Check interval must be at least 60 seconds")
            if self._config.schedule.catchup_window < 3600:
                errors.append("Catchup window must be at least 1 hour")

            # Check credentials existence
            for plat, attr, label in [
                ("youtube", "client_secrets", "YouTube client secrets"),
                ("youtube", "token_file", "YouTube token file"),
                ("x", "cookies_file", "X cookies file"),
                ("instagram", "session_file", "Instagram session file"),
            ]:
                acct = getattr(self._config, plat, None)
                if acct is not None:
                    path_val = getattr(acct, attr, "")
                    if path_val:
                        expanded = Path(path_val).expanduser()
                        if not expanded.exists():
                            warnings.append(f"{label} not found: {expanded}")

            # Check rate limits
            if hasattr(self._config, "rate_limits"):
                rl = self._config.rate_limits
                for plat in ("youtube", "instagram", "x", "tiktok"):
                    limit = getattr(rl, plat, None)
                    if limit is not None and (limit < 0 or limit > 100):
                        warnings.append(f"Unusual rate limit for {plat}: {limit}")

            valid = len(errors) == 0
            return json.dumps({
                "valid": valid,
                "warnings": warnings,
                "errors": errors,
            })
        except Exception as exc:
            logger.error("validateConfig error: %s", exc)
            return json.dumps({
                "valid": False,
                "warnings": warnings,
                "errors": [str(exc)],
            })


def _default_ui_font() -> str:
    """Return a sensible default UI font family for the current platform (W4-7).

    The app ships Inter so regular UI text never falls back to the bundled
    Lucide icon font in headless, offscreen, or packaged environments where Qt
    cannot see system sans fonts.
    """
    return icon_glyphs.UI_FONT_FAMILY


def _default_mono_font() -> str:
    """Return a sensible default monospace font family for the platform (W4-7)."""
    if sys.platform == "darwin":
        return "SF Mono"
    if sys.platform.startswith("win"):
        return "Cascadia Mono"
    return "DejaVu Sans Mono"


class ThemeProvider(QObject):
    """Theme colors and design tokens exposed to QML.

    All colour properties emit darkModeChanged when toggled, so QML
    bindings automatically update.
    """

    darkModeChanged = Signal()

    # ── Colour maps ──────────────────────────────────────────────────
    _DARK = {
        "canvas": "#1c1c1e",
        "surface": "#242426",
        "surfaceAlt": "#2c2c2e",
        "surfaceCard": "#2c2c2e",
        "textPrimary": "#f5f5f7",
        "textSecondary": "#c7c7cc",
        "textMuted": "#8e8e93",
        "accent": "#007aff",
        "accentHover": "#0a84ff",
        "accentMuted": "#0f355f",
        "success": "#30d158",
        "warning": "#ffd60a",
        "error": "#ff453a",
    }
    _LIGHT = {
        "canvas": "#f5f5f7",
        "surface": "#fbfbfd",
        "surfaceAlt": "#f0f0f2",
        "surfaceCard": "#ffffff",
        "textPrimary": "#1d1d1f",
        "textSecondary": "#5f6368",
        "textMuted": "#8a8f98",
        "accent": "#007aff",
        "accentHover": "#0a84ff",
        "accentMuted": "#dbeafe",
        "success": "#248a3d",
        "warning": "#b26a00",
        "error": "#d70015",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dark_mode = False

    def _col(self, key: str) -> str:
        return self._DARK[key] if self._dark_mode else self._LIGHT[key]

    # Surface ladder
    @Property(str, notify=darkModeChanged)
    def canvas(self): return self._col("canvas")

    @Property(str, notify=darkModeChanged)
    def surface(self): return self._col("surface")

    @Property(str, notify=darkModeChanged)
    def surfaceAlt(self): return self._col("surfaceAlt")

    @Property(str, notify=darkModeChanged)
    def surfaceCard(self): return self._col("surfaceCard")

    # Text hierarchy
    @Property(str, notify=darkModeChanged)
    def textPrimary(self): return self._col("textPrimary")

    @Property(str, notify=darkModeChanged)
    def textSecondary(self): return self._col("textSecondary")

    @Property(str, notify=darkModeChanged)
    def textMuted(self): return self._col("textMuted")

    # Accent
    @Property(str, notify=darkModeChanged)
    def accent(self): return self._col("accent")

    @Property(str, notify=darkModeChanged)
    def accentHover(self): return self._col("accentHover")

    @Property(str, notify=darkModeChanged)
    def accentMuted(self): return self._col("accentMuted")

    # Semantic
    @Property(str, notify=darkModeChanged)
    def success(self): return self._col("success")

    @Property(str, notify=darkModeChanged)
    def warning(self): return self._col("warning")

    @Property(str, notify=darkModeChanged)
    def error(self): return self._col("error")

    # Platform colors (same in both modes)
    @Property(str, constant=True)
    def youtube(self): return "#ff0033"

    @Property(str, constant=True)
    def instagram(self): return "#e1306c"

    @Property(str, constant=True)
    def xtwitter(self): return "#1d9bf0"

    @Property(str, constant=True)
    def tiktok(self): return "#00b8b8"

    # Spacing (constant in both modes)
    @Property(int, constant=True)
    def spacingXs(self): return 4
    @Property(int, constant=True)
    def spacingSm(self): return 8
    @Property(int, constant=True)
    def spacingMd(self): return 12
    @Property(int, constant=True)
    def spacingLg(self): return 16
    @Property(int, constant=True)
    def spacingXl(self): return 24
    @Property(int, constant=True)
    def spacingXxl(self): return 32

    @Property(int, constant=True)
    def pageMargin(self): return 28

    # Radius (constant in both modes). radiusLg and radiusXl were duplicate
    # literal 8s; they now resolve to one shared base so future tweaks stay in
    # one place (W4-8: de-dupe, not a re-skin — values are unchanged).
    _RADIUS_LG = 8

    @Property(int, constant=True)
    def radiusSm(self): return 4
    @Property(int, constant=True)
    def radiusMd(self): return 6
    @Property(int, constant=True)
    def radiusLg(self): return self._RADIUS_LG
    @Property(int, constant=True)
    def radiusXl(self): return self._RADIUS_LG

    # Control-height scale (W4-8). Before this, button/control heights were
    # scattered raw literals (26/28/30/32/34/36/40). These three named steps
    # cover the sensible existing sizes so usages can point at a token instead
    # of a magic number. Values are chosen to match the heights already in use
    # (sm≈small chips, md≈standard buttons, lg≈primary actions) — this is
    # de-duplication, not a redesign.
    @Property(int, constant=True)
    def controlHeightSm(self): return 28
    @Property(int, constant=True)
    def controlHeightMd(self): return 36
    @Property(int, constant=True)
    def controlHeightLg(self): return 44

    # Platform-aware default font (W4-7). The previous hardcoded "Segoe UI" /
    # "Cascadia Mono" exist only on Windows, so mac/Linux fell back to an
    # unintended family and drifted in metrics. Pick a sensible per-platform
    # default while keeping Windows on its original families.
    @Property(str, constant=True)
    def fontFamily(self): return _default_ui_font()

    @Property(str, constant=True)
    def monoFontFamily(self): return _default_mono_font()

    # Icon font (W4-4 / W4-5). The single source of truth for icon glyphs is
    # icon_glyphs.py, routed through the bundled Lucide font. These properties
    # make the existing `theme.icon*` QML references (previously resolving to
    # the dead Theme.qml singleton) resolve on the live ThemeProvider instead.
    @Property(str, constant=True)
    def iconFontFamily(self): return icon_glyphs.ICON_FONT_FAMILY

    @Property(str, constant=True)
    def iconYouTube(self): return icon_glyphs.glyph("youtube")
    @Property(str, constant=True)
    def iconInstagram(self): return icon_glyphs.glyph("instagram")
    @Property(str, constant=True)
    def iconX(self): return icon_glyphs.glyph("x")
    @Property(str, constant=True)
    def iconTikTok(self): return icon_glyphs.glyph("tiktok")

    @Property(str, constant=True)
    def iconLogo(self): return icon_glyphs.glyph("logo")
    @Property(str, constant=True)
    def iconBell(self): return icon_glyphs.glyph("bell")
    @Property(str, constant=True)
    def iconMoon(self): return icon_glyphs.glyph("moon")
    @Property(str, constant=True)
    def iconSun(self): return icon_glyphs.glyph("sun")
    @Property(str, constant=True)
    def iconStats(self): return icon_glyphs.glyph("stats")
    @Property(str, constant=True)
    def iconViews(self): return icon_glyphs.glyph("views")
    @Property(str, constant=True)
    def iconLikes(self): return icon_glyphs.glyph("likes")
    @Property(str, constant=True)
    def iconComments(self): return icon_glyphs.glyph("comments")
    @Property(str, constant=True)
    def iconShares(self): return icon_glyphs.glyph("shares")
    @Property(str, constant=True)
    def iconViewGrid(self): return icon_glyphs.glyph("view_grid")
    @Property(str, constant=True)
    def iconViewList(self): return icon_glyphs.glyph("view_list")
    @Property(str, constant=True)
    def iconChevronLeft(self): return icon_glyphs.glyph("chevron_left")
    @Property(str, constant=True)
    def iconChevronRight(self): return icon_glyphs.glyph("chevron_right")
    @Property(str, constant=True)
    def iconUsers(self): return icon_glyphs.glyph("users")
    @Property(str, constant=True)
    def iconTrophy(self): return icon_glyphs.glyph("trophy")
    @Property(str, constant=True)
    def iconCalendar(self): return icon_glyphs.glyph("calendar")
    @Property(str, constant=True)
    def iconVideo(self): return icon_glyphs.glyph("video")
    @Property(str, constant=True)
    def iconCheck(self): return icon_glyphs.glyph("check")
    @Property(str, constant=True)
    def iconError(self): return icon_glyphs.glyph("error")
    @Property(str, constant=True)
    def iconEdit(self): return icon_glyphs.glyph("edit")
    @Property(str, constant=True)
    def iconWeb(self): return icon_glyphs.glyph("web")
    @Property(str, constant=True)
    def iconRepo(self): return icon_glyphs.glyph("repo")
    @Property(str, constant=True)
    def iconDocs(self): return icon_glyphs.glyph("docs")
    @Property(str, constant=True)
    def iconIssue(self): return icon_glyphs.glyph("issue")
    @Property(str, constant=True)
    def iconChangelog(self): return icon_glyphs.glyph("changelog")

    @Property(str, constant=True)
    def iconPlus(self): return icon_glyphs.glyph("plus")

    @Property(str, constant=True)
    def iconDashboard(self): return icon_glyphs.glyph("dashboard")
    @Property(str, constant=True)
    def iconContent(self): return icon_glyphs.glyph("content")
    @Property(str, constant=True)
    def iconAnalytics(self): return icon_glyphs.glyph("analytics")
    @Property(str, constant=True)
    def iconConnect(self): return icon_glyphs.glyph("connect")
    @Property(str, constant=True)
    def iconSchedule(self): return icon_glyphs.glyph("schedule")
    @Property(str, constant=True)
    def iconSettings(self): return icon_glyphs.glyph("settings")
    @Property(str, constant=True)
    def iconAbout(self): return icon_glyphs.glyph("about")

    # Dark mode toggle
    @Property(bool, notify=darkModeChanged)
    def darkMode(self):
        return self._dark_mode

    @darkMode.setter
    def darkMode(self, value):
        if self._dark_mode != value:
            self._dark_mode = value
            self.darkModeChanged.emit()
