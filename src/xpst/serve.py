"""Supervised long-running daemon for xPST (``xpst serve``).

This module provides a single supervised entry point that:

- acquires the shared engine pidfile (rejecting a live holder, safely
  stealing a stale leftover from a crashed process, releasing on shutdown),
- runs the configured scheduler loop by reusing the existing
  :class:`xpst.scheduler.Scheduler` and :class:`xpst.schedule_manager.ScheduleManager`
  classes (no scheduling logic is reimplemented here),
- optionally serves the FastAPI dashboard by reusing
  :func:`xpst.dashboard.server.start_dashboard`,
- logs launchctl/systemd-friendly startup and health lines,
- handles SIGTERM / SIGINT (graceful shutdown) and SIGHUP (continue) so a
  supervised process can be stopped cleanly and a pidfile is never left stale.

The daemon is intentionally threads-based: the scheduler and the dashboard run
in daemon threads while the main thread owns the pidfile and the signal
handlers.  This keeps signal response time independent of in-flight network
work (a check that is hanging on a remote API cannot delay ``SIGTERM`` past
the shutdown grace period).
"""

from __future__ import annotations

import asyncio
import os
import signal
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xpst.engine import CrossPostEngine
from xpst.schedule_manager import ScheduleManager
from xpst.scheduler import Scheduler
from xpst.utils.logger import get_logger
from xpst.utils.pidfile import PidfileLock, PidfileLockError

if TYPE_CHECKING:
    from xpst.config import XPSTConfig

logger = get_logger(__name__)

# Default source fed to the watch loop (matches `xpst run/watch`).
DEFAULT_WATCH_SOURCE = "tiktok"


class ServeSupervisor:
    """Supervised daemon: scheduler loop + optional FastAPI dashboard.

    Args:
        config: Loaded XPSTConfig.
        no_dashboard: If True, do not start the FastAPI dashboard.
        host: Bind host for the dashboard (default loopback).
        port: Bind port for the dashboard (default ``monitoring.healthcheck_port``).
        check_interval: Scheduler check interval in seconds (default from config).
        source: Source platform for the new-video watch check.
        engine: Optional pre-built engine (used by tests to inject a fake).
    """

    def __init__(
        self,
        config: XPSTConfig,
        *,
        no_dashboard: bool = False,
        host: str = "127.0.0.1",
        port: int | None = None,
        check_interval: int | None = None,
        source: str = DEFAULT_WATCH_SOURCE,
        engine: CrossPostEngine | None = None,
    ) -> None:
        self.config = config
        self.no_dashboard = no_dashboard
        self.host = host
        self.port = int(port or config.monitoring.healthcheck_port)
        self.check_interval = max(1, int(check_interval or config.schedule.check_interval))
        self.source = source
        self.engine = engine or CrossPostEngine(config)
        self.pidfile = PidfileLock(config.config_dir)

        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._dashboard_thread: threading.Thread | None = None
        self._exited = False

    # ── lifecycle ─────────────────────────────────────────────────────

    def acquire(self) -> bool:
        """Acquire the shared pidfile.

        Returns:
            True if this process now holds the pidfile.  False (without
            raising) when another live instance holds it — the caller treats
            that as "already running" and exits successfully so cron /
            launchctl keep-alive invocations are idempotent.
        """
        try:
            self.pidfile.acquire()
        except PidfileLockError as exc:
            logger.info("xpst serve: another instance is running (%s)", exc)
            return False
        logger.info(
            "xpst serve: pidfile acquired %s (pid=%d)",
            self.pidfile.lock_path,
            os.getpid(),
        )
        return True

    def _install_signal_handlers(self) -> None:
        """Install SIGTERM/SIGINT -> graceful stop, SIGHUP -> continue."""

        def _handle_term(signum: int, _frame: Any) -> None:  # noqa: ARG001
            logger.info("xpst serve: received signal %d, shutting down", signum)
            self._stop.set()

        def _handle_hup(signum: int, _frame: Any) -> None:  # noqa: ARG001
            logger.info("xpst serve: received SIGHUP; continuing", signum)

        signal.signal(signal.SIGTERM, _handle_term)
        signal.signal(signal.SIGINT, _handle_term)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, _handle_hup)

    # ── scheduler work ────────────────────────────────────────────────

    def _process_due_posts(self) -> dict[str, int]:
        """Publish due scheduled posts via the existing ScheduleManager API.

        Returns:
            Per-status counts (due, posted, failed).
        """
        counts = {"due": 0, "posted": 0, "failed": 0}
        manager = ScheduleManager(self.config.config_dir)

        due = manager.get_due()
        if not due:
            return counts
        counts["due"] = len(due)

        for entry in due:
            entry_id = entry["id"]
            video_path = Path(entry["video_path"])
            caption = entry["caption"]
            platforms = entry.get("platforms") or None

            if not video_path.exists():
                manager.mark_complete(entry_id, success=False, error=f"File not found: {video_path}")
                counts["failed"] += 1
                logger.warning(
                    "xpst serve: scheduled post %s skipped — file missing: %s",
                    entry_id,
                    video_path,
                )
                continue

            try:
                # The worker runs in a non-async thread; drive the async
                # engine call the same way the `schedule run` command does.
                result = asyncio.run(self.engine.post_manual(video_path, caption, platforms))
                success = result.all_success
                error_msg = None
                if not success:
                    error_msg = "; ".join(f"{p}: {ur.error}" for p, ur in result.results.items() if not ur.success)
                manager.mark_complete(entry_id, success=success, error=error_msg)
                if success:
                    counts["posted"] += 1
                    logger.info("xpst serve: scheduled post %s published", entry_id)
                else:
                    counts["failed"] += 1
                    logger.warning(
                        "xpst serve: scheduled post %s partial/failed: %s",
                        entry_id,
                        error_msg,
                    )
            except Exception as exc:  # noqa: BLE001 - keep the daemon alive
                manager.mark_complete(entry_id, success=False, error=str(exc))
                counts["failed"] += 1
                logger.error(
                    "xpst serve: scheduled post %s raised %s",
                    entry_id,
                    exc,
                )
        return counts

    def _run_watch_check(self) -> None:
        """Run one new-video/catch-up check via the existing Scheduler logic."""
        scheduler = Scheduler(self.engine, self.config)
        try:
            catch_up = scheduler._needs_catch_up()  # noqa: SLF001 - existing API
            scheduler._run_check(catch_up=catch_up, source=self.source)  # noqa: SLF001
            self.engine.state.update_last_wake_check()
            self.engine.state.save()
        except Exception as exc:  # noqa: BLE001 - keep the daemon alive
            logger.error("xpst serve: watch check failed: %s", exc)

    def _cycle_once(self) -> dict[str, int]:
        """Run one full scheduler cycle (due posts + new-video watch check)."""
        due_counts = self._process_due_posts()
        self._run_watch_check()
        return due_counts

    def _worker_loop(self) -> None:
        """Daemon-thread body: run cycles at the configured interval."""
        while not self._stop.is_set():
            counts = {}
            try:
                counts = self._cycle_once()
            except Exception as exc:  # noqa: BLE001 - keep the daemon alive
                logger.error("xpst serve: scheduler cycle raised %s", exc)
            logger.info(
                "xpst serve: cycle complete (due=%d posted=%d failed=%d)",
                counts.get("due", 0),
                counts.get("posted", 0),
                counts.get("failed", 0),
            )
            if self._stop.wait(self.check_interval):
                break

    # ── dashboard ─────────────────────────────────────────────────────

    def _start_dashboard(self) -> None:
        """Start the FastAPI dashboard in a daemon thread.

        The existing ``start_dashboard`` blocks on uvicorn, so it runs in a
        daemon thread; uvicorn will not install its own signal handlers in a
        non-main thread, leaving signal ownership with the supervisor.
        """

        def _serve() -> None:
            try:
                from xpst.dashboard.server import start_dashboard

                start_dashboard(
                    port=self.port,
                    host=self.host,
                    config_dir=self.config.config_dir,
                )
            except Exception as exc:  # noqa: BLE001 - never kill the daemon
                logger.error("xpst serve: dashboard failed: %s", exc)

        self._dashboard_thread = threading.Thread(
            target=_serve,
            name="xpst-dashboard",
            daemon=True,
        )
        self._dashboard_thread.start()
        logger.info(
            "xpst serve: dashboard starting on http://%s:%d",
            self.host,
            self.port,
        )

    # ── main entry ────────────────────────────────────────────────────

    def run(self) -> int:
        """Run the supervised daemon until a termination signal arrives.

        Returns:
            0 on clean shutdown, 1 if the pidfile could not be interpreted /
            a fatal setup error occurred.
        """
        if not self.acquire():
            # Another live instance already supervises the engine — an
            # idempotent no-op for cron/launchctl keep-alive invocations.
            return 0

        logger.info(
            "xpst serve: starting (pid=%d, config_dir=%s, dashboard=%s, port=%s, check_interval=%ss, source=%s)",
            os.getpid(),
            self.config.config_dir,
            "disabled" if self.no_dashboard else "enabled",
            self.port if not self.no_dashboard else "-",
            self.check_interval,
            self.source,
        )

        self._install_signal_handlers()

        if not self.no_dashboard:
            self._start_dashboard()

        self._worker = threading.Thread(
            target=self._worker_loop,
            name="xpst-scheduler",
            daemon=True,
        )
        self._worker.start()

        # Main thread owns signals + pidfile. Block until a signal handler
        # flips the stop event.
        logger.info("xpst serve: running (Ctrl+C or SIGTERM to stop)")
        try:
            self._stop.wait()
        finally:
            self._shutdown()
        return 0

    def _shutdown(self) -> None:
        """Release the pidfile and log the shutdown line (idempotent)."""
        if self._exited:
            return
        self._exited = True
        try:
            self.pidfile.release()
        except Exception as exc:  # noqa: BLE001
            logger.error("xpst serve: failed to release pidfile: %s", exc)
        logger.info("xpst serve: stopped cleanly (pid=%d)", os.getpid())


def run_serve(
    config: XPSTConfig,
    *,
    no_dashboard: bool = False,
    host: str = "127.0.0.1",
    port: int | None = None,
    check_interval: int | None = None,
    source: str = DEFAULT_WATCH_SOURCE,
) -> int:
    """Run the xPST supervisor; returns the process exit code."""
    supervisor = ServeSupervisor(
        config,
        no_dashboard=no_dashboard,
        host=host,
        port=port,
        check_interval=check_interval,
        source=source,
    )
    try:
        return supervisor.run()
    except KeyboardInterrupt:
        logger.info("xpst serve: interrupted")
        return 0
    except Exception as exc:  # noqa: BLE001 - report and exit non-zero
        logger.error("xpst serve: fatal error: %s", exc)
        return 1


__all__ = ["ServeSupervisor", "DEFAULT_WATCH_SOURCE", "run_serve"]
