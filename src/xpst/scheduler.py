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
import threading
import time
from datetime import datetime

from xpst.config import XPSTConfig
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
        # Scheduled analytics snapshot capture (optional, default OFF).
        # Tracks the wall-clock time of the last capture so the interval is
        # independent of the (usually shorter) check_interval loop.
        self._last_snapshot_capture: float | None = None

    @property
    def last_results(self) -> list:
        """Get results from the most recent check cycle."""
        return self._last_results

    def _maybe_capture_analytics(self) -> None:
        """Capture analytics snapshots when the configured interval has elapsed.

        Runs inside the watch loop only when
        ``schedule.analytics_snapshot_enabled`` is True. Uses
        ``AnalyticsCollector.collect_all()`` — which persists metric_snapshots
        through the ownership-gated record path — for every platform with
        discovered post ids. Failures are logged and never break the watch
        loop; a failed capture simply retries on the next interval.
        """
        schedule = self.config.schedule
        if not schedule.analytics_snapshot_enabled:
            return

        now = time.monotonic()
        last = self._last_snapshot_capture
        if last is not None and (now - last) < schedule.analytics_snapshot_interval:
            return
        self._last_snapshot_capture = now

        try:
            from xpst.analytics import AnalyticsCollector

            collector = AnalyticsCollector(config_dir=self.config.config_dir)
            data = asyncio.run(collector.collect_all())
            captured = sum(len(posts) for posts in data.values())
            logger.info("Scheduled analytics snapshot captured (%d posts)", captured)
        except Exception as e:
            logger.warning("Scheduled analytics capture failed: %s", e)

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
                self._last_wake_check = datetime.now()
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

            # Optional scheduled snapshot capture (default OFF; config
            # schedule.analytics_snapshot_enabled). Runs after each check
            # cycle, gated by its own independent interval.
            try:
                self._maybe_capture_analytics()
            except Exception as e:  # defensive — never break the watch loop
                logger.warning(f"Analytics snapshot capture error: {e}")

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

        elapsed = (datetime.now() - last_wake).total_seconds()
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
