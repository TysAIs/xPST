"""
Scheduler for xPST

Single source of truth for watch-mode scheduling. The CLI ``watch``
command delegates to this scheduler instead of duplicating the loop.

Handles:
- Periodic checking (watch mode)
- Catch-up logic (handle Mac sleep/wake cycles)
- Graceful shutdown
- Health monitoring
"""

import asyncio
import inspect
import threading
import time
from datetime import datetime, timezone

from xpst.config import (
    MAX_ANALYTICS_SNAPSHOT_INTERVAL,
    MIN_ANALYTICS_SNAPSHOT_INTERVAL,
    XPSTConfig,
)
from xpst.engine import CrossPostEngine
from xpst.utils.logger import get_logger

logger = get_logger(__name__)


class Scheduler:
    """
    Scheduler for cross-posting operations.

    Features:
    - Configurable check intervals
    - Sleep/wake detection with catch-up
    - Graceful shutdown
    - Health monitoring
    """

    def __init__(self, engine: CrossPostEngine, config: XPSTConfig):
        """
        Initialize scheduler.

        Args:
            engine: Cross-posting engine
            config: xPST configuration
        """
        self.engine = engine
        self.config = config
        self._running = False
        self._stop_event = threading.Event()
        self._last_wake_check: datetime | None = None
        self._last_results: list = []
        # Optional analytics capture has its own cadence so a frequent post
        # check does not consume analytics API quota.
        self._last_snapshot_capture: float | None = None

    @property
    def last_results(self) -> list:
        """Get results from the most recent check cycle."""
        return self._last_results

    def _maybe_capture_analytics(self) -> None:
        """Capture persisted analytics snapshots on an optional cadence.

        The feature is disabled by default. Capture failures are isolated from
        the posting watch loop, and the attempt timestamp is recorded before
        work starts so a failing provider cannot create a hot retry loop.
        """
        schedule = self.config.schedule
        if not schedule.analytics_snapshot_enabled:
            return

        # Config validation enforces these bounds; clamp here too because
        # tests and programmatic callers may construct dataclasses directly.
        try:
            interval = min(
                max(int(schedule.analytics_snapshot_interval), MIN_ANALYTICS_SNAPSHOT_INTERVAL),
                MAX_ANALYTICS_SNAPSHOT_INTERVAL,
            )
        except (TypeError, ValueError):
            logger.warning("Scheduled analytics capture disabled: invalid snapshot interval")
            return
        now = time.monotonic()
        if self._last_snapshot_capture is not None and now - self._last_snapshot_capture < interval:
            return
        self._last_snapshot_capture = now

        try:
            from xpst.analytics import AnalyticsCollector

            collector = AnalyticsCollector(config_dir=self.config.config_dir)
            result = collector.collect_all()
            if inspect.isawaitable(result):
                result = asyncio.run(result)
            captured = sum(len(posts) for posts in result.values()) if isinstance(result, dict) else 0
            logger.info("Scheduled analytics snapshot captured (%d posts)", captured)
        except Exception as exc:  # noqa: BLE001 - isolate optional work
            logger.warning("Scheduled analytics capture failed: %s", exc)

    def run(self, interval: int | None = None) -> None:
        """
        Run the scheduler in watch mode.

        Args:
            interval: Check interval in seconds (default: from config)
        """
        check_interval = interval or self.config.schedule.check_interval

        logger.info(f"Starting scheduler (interval: {check_interval}s)")

        self._running = True

        while self._running:
            try:
                # Check if we need catch-up
                if self._needs_catch_up():
                    logger.info("Mac was asleep. Running catch-up...")
                    self._run_check(catch_up=True)
                else:
                    self._run_check(catch_up=False)

                # Update wake check
                self._last_wake_check = datetime.now(timezone.utc)
                self.engine.state.update_last_wake_check()
                self.engine.state.save()

                # Wait for next check. Event.wait (not time.sleep) so that
                # stop() interrupts the wait immediately instead of after a
                # full interval.
                logger.debug(f"Next check in {check_interval}s")
                if self._stop_event.wait(check_interval):
                    logger.info("Scheduler stop requested during wait")
                    break

            except KeyboardInterrupt:
                logger.info("Scheduler stopped by user")
                break
            except Exception as e:
                logger.error(f"Error in scheduler loop: {e}")
                if self._stop_event.wait(60):  # Wait before retry
                    break

            # Optional analytics capture is independent of posting success.
            self._maybe_capture_analytics()

    def stop(self) -> None:
        """Stop the scheduler (interrupts a pending wait immediately)."""
        self._running = False
        self._stop_event.set()

    def _needs_catch_up(self) -> bool:
        """Check if a catch-up run is needed due to sleep/wake.

        Returns True if the elapsed time since the last wake check
        exceeds 2× the configured check interval. This heuristic
        detects Mac sleep/wake cycles where the timer was paused.

        Returns:
            True if catch-up should run, False otherwise.
        """

        last_wake = self.engine.state.get_last_wake_check()

        if not last_wake:
            return False

        if last_wake.tzinfo is None:
            # Legacy/fake state providers expose naive local wall-clock time;
            # compare it to the same clock. Persisted current state is aware
            # UTC and follows the branch below.
            current = datetime.now()
            if current.tzinfo is not None:
                current = current.replace(tzinfo=None)
            elapsed = (current - last_wake).total_seconds()
        else:
            last_wake = last_wake.astimezone(timezone.utc)
            current = datetime.now(timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            else:
                current = current.astimezone(timezone.utc)
            elapsed = (current - last_wake).total_seconds()
        threshold = self.config.schedule.check_interval * 2

        return elapsed > threshold

    def _run_check(self, catch_up: bool = False, source: str = "tiktok") -> None:
        """Run a single check-and-post cycle via the engine.

        Args:
            catch_up: If True, fetches more videos to compensate for downtime.
            source: Source to fetch from (e.g. 'tiktok', 'local').

        Raises:
            Exception: Re-raised after logging, to allow caller handling.
        """

        try:
            results = asyncio.run(self.engine.check_and_post(catch_up=catch_up, source=source))
            self._last_results = results

            # Update health
            self.engine.state.update_last_check_time()
            self.engine.state.save()

        except Exception as e:
            logger.error(f"Check failed: {e}")
            raise
