"""AppController — Python ↔ QML bridge for xPST desktop app.

Exposes xPST state, config, analytics, and engine actions as
Q_PROPERTY / Q_INVOKABLE slots consumable from QML.  All complex
data is serialised as JSON strings so QML can parse with JSON.parse().
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal, Slot, Property

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
    from xpst.dashboard.analytics import AnalyticsCollector
except ImportError:
    AnalyticsCollector = None  # type: ignore[assignment,misc]

try:
    from xpst.utils.quota import QuotaManager
except ImportError:
    QuotaManager = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

PLATFORMS = ("youtube", "instagram", "x", "tiktok")


class AppController(QObject):
    """Main controller bridging xPST Python backend to QML frontend."""

    # ── Signals ──────────────────────────────────────────────────────
    dataChanged = Signal()
    postComplete = Signal(str)   # JSON result string
    error = Signal(str)          # error message

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

        try:
            if AnalyticsCollector is not None:
                self._analytics = AnalyticsCollector()
        except Exception as exc:
            logger.warning("Failed to create AnalyticsCollector: %s", exc)

        # Engine is heavy and async; defer creation
        self._engine = None

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

    @Slot()
    def refreshData(self) -> None:
        """Reload all cached data from backends and emit dataChanged."""
        try:
            self._refresh_state()
            self._refresh_platform_health()
            self._refresh_recent_posts()
            self._refresh_config()
            self._refresh_analytics()
            self._refresh_quota()
            self.dataChanged.emit()
        except Exception as exc:
            logger.error("refreshData failed: %s", exc)
            self.error.emit(str(exc))

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

            # Best platform by post count
            if platform_counts:
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

                for platform, pinfo in vdata.get("posted_to", {}).items():
                    posts.append({
                        "title": video_id,
                        "caption": caption,
                        "platform": platform,
                        "status": "posted",
                        "timestamp": pinfo.get("timestamp") or downloaded_at,
                        "postId": pinfo.get("id") or video_id,
                        "url": pinfo.get("url", ""),
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
        """Collect analytics data for QML."""
        if self._analytics is None:
            self._analytics_data = json.dumps({"available": False})
            return

        try:
            data: dict[str, Any] = {"available": True}

            summary = self._analytics.get_summary_stats()
            data["summary"] = summary

            health_list = self._analytics.get_platform_health_all()
            data["platforms"] = health_list

            top = self._analytics.get_top_posts(limit=5)
            data["top_posts"] = top

            self._analytics_data = json.dumps(data, default=str)
        except Exception as exc:
            logger.warning("Analytics refresh error: %s", exc)
            self._analytics_data = json.dumps({"available": False, "error": str(exc)})

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

    @Slot()
    def runPost(self) -> None:
        """Trigger a cross-post cycle via CrossPostEngine."""
        if CrossPostEngine is None:
            self.error.emit("CrossPostEngine not available")
            return

        try:
            # Lazy-init engine
            if self._engine is None:
                if self._config is None or self._state is None:
                    self.error.emit("Config or State not initialised")
                    return
                self._engine = CrossPostEngine(self._config, self._state)

            # Run in a background thread to avoid blocking the UI
            import threading

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

        except Exception as exc:
            logger.error("runPost setup error: %s", exc)
            self.error.emit(str(exc))

    @Slot(str, result=str)
    def saveSettings(self, settings_json: str) -> str:
        """Persist settings from QML. Accepts JSON string, returns status JSON."""
        try:
            settings = json.loads(settings_json)
        except json.JSONDecodeError as exc:
            return json.dumps({"ok": False, "error": f"Invalid JSON: {exc}"})

        if self._config is None:
            return json.dumps({"ok": False, "error": "Config not loaded"})

        try:
            import yaml
            from pathlib import Path

            config_path = Path("~/.xpst/config.yaml").expanduser()
            config_path.parent.mkdir(parents=True, exist_ok=True)

            # Load existing YAML config
            existing: dict[str, Any] = {}
            if config_path.exists():
                with open(config_path) as f:
                    existing = yaml.safe_load(f) or {}

            # Merge QML settings into the config dict
            if "accounts" not in existing:
                existing["accounts"] = {}

            for plat in PLATFORMS:
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

            # Write back
            with open(config_path, "w") as f:
                yaml.dump(existing, f, default_flow_style=False, sort_keys=False)

            # Reload config
            self._config = XPSTConfig.load()
            self.refreshData()

            return json.dumps({"ok": True})
        except Exception as exc:
            logger.error("saveSettings error: %s", exc)
            return json.dumps({"ok": False, "error": str(exc)})

    @Slot(str, result=str)
    def connectPlatform(self, platform: str) -> str:
        """Test connectivity to a platform. Returns JSON status."""
        if self._engine is None:
            # Try lazy engine init
            if CrossPostEngine is not None and self._config and self._state:
                try:
                    self._engine = CrossPostEngine(self._config, self._state)
                except Exception as exc:
                    return json.dumps({"ok": False, "error": str(exc)})
            else:
                return json.dumps({"ok": False, "error": "Engine not available"})

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            health = loop.run_until_complete(self._engine.check_health())
            loop.close()

            plat_health = health.get("platforms", {}).get(platform, {})
            ok = plat_health.get("status") in ("ok", "healthy", "connected")

            return json.dumps({
                "ok": ok,
                "platform": platform,
                "details": plat_health,
            }, default=str)
        except Exception as exc:
            logger.error("connectPlatform(%s) error: %s", platform, exc)
            return json.dumps({"ok": False, "error": str(exc)})

    @Slot(str, str, result=str)
    def deletePost(self, post_id: str, platform: str) -> str:
        """Remove a post entry from state (does not delete from platform)."""
        if self._state is None:
            return json.dumps({"ok": False, "error": "StateManager not available"})

        try:
            posted = self._state._state.get("posted_videos", {})
            if post_id in posted:
                # Remove specific platform entry
                posted_to = posted[post_id].get("posted_to", {})
                if platform in posted_to:
                    del posted_to[platform]
                    self._state.save()
                    self.refreshData()
                    return json.dumps({"ok": True, "removed": f"{post_id}/{platform}"})

                # If no platforms left, remove the entire video entry
                if not posted_to:
                    del posted[post_id]
                    self._state.save()
                    self.refreshData()
                    return json.dumps({"ok": True, "removed": post_id})

                return json.dumps({"ok": False, "error": f"Not posted to {platform}"})

            return json.dumps({"ok": False, "error": "Post not found"})
        except Exception as exc:
            logger.error("deletePost error: %s", exc)
            return json.dumps({"ok": False, "error": str(exc)})

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
            "analyticsAvailable": self._analytics is not None,
            "quotaAvailable": self._quota is not None,
            "timestamp": datetime.now().isoformat(),
        }
        return json.dumps(status, default=str)

    @Slot(result=str)
    def getHealth(self) -> str:
        """Return platform health as JSON (same as platformHealth property)."""
        # Force a refresh then return
        self._refresh_platform_health()
        return self._platform_health


class ThemeProvider(QObject):
    """Theme colors and design tokens exposed to QML."""
    
    darkModeChanged = Signal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._dark_mode = True
    
    # Surface ladder
    @Property(str, constant=True)
    def canvas(self): return "#0a0a0f"
    
    @Property(str, constant=True)
    def surface(self): return "#12121a"
    
    @Property(str, constant=True)
    def surfaceAlt(self): return "#1a1a25"
    
    @Property(str, constant=True)
    def surfaceCard(self): return "#1e1e2a"
    
    # Text hierarchy
    @Property(str, constant=True)
    def textPrimary(self): return "#f0f0f5"
    
    @Property(str, constant=True)
    def textSecondary(self): return "#a0a0b0"
    
    @Property(str, constant=True)
    def textMuted(self): return "#6b6b80"
    
    # Accent
    @Property(str, constant=True)
    def accent(self): return "#6366f1"
    
    @Property(str, constant=True)
    def accentHover(self): return "#818cf8"
    
    @Property(str, constant=True)
    def accentMuted(self): return "#312e81"
    
    # Semantic
    @Property(str, constant=True)
    def success(self): return "#22c55e"
    
    @Property(str, constant=True)
    def warning(self): return "#f59e0b"
    
    @Property(str, constant=True)
    def error(self): return "#ef4444"
    
    # Platform colors
    @Property(str, constant=True)
    def youtube(self): return "#ff0000"
    
    @Property(str, constant=True)
    def instagram(self): return "#e1306c"
    
    @Property(str, constant=True)
    def xtwitter(self): return "#1d9bf0"
    
    @Property(str, constant=True)
    def tiktok(self): return "#00f2ea"
    
    # Spacing
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
    
    # Radius
    @Property(int, constant=True)
    def radiusSm(self): return 6
    @Property(int, constant=True)
    def radiusMd(self): return 8
    @Property(int, constant=True)
    def radiusLg(self): return 12
    @Property(int, constant=True)
    def radiusXl(self): return 16
    
    # Dark mode toggle
    @Property(bool, notify=darkModeChanged)
    def darkMode(self):
        return self._dark_mode
    
    @darkMode.setter
    def darkMode(self, value):
        if self._dark_mode != value:
            self._dark_mode = value
            self.darkModeChanged.emit()
