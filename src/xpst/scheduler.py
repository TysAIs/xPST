"""
Scheduler for XPST

Single source of truth for watch-mode scheduling. The CLI ``watch``
command delegates to this scheduler instead of duplicating the loop.

Handles:
- Periodic checking (watch mode)
- Catch-up logic (handle Mac sleep/wake cycles)
- Graceful shutdown
- Health monitoring
"""

import asyncio
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
            config: XPST configuration
        """
        self.engine = engine
        self.config = config
        self._running = False
        self._last_wake_check: datetime | None = None
        self._last_results: list = []

    @property
    def last_results(self) -> list:
        """Get results from the most recent check cycle."""
        return self._last_results

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

                # Wait for next check
                logger.debug(f"Next check in {check_interval}s")
                time.sleep(check_interval)

            except KeyboardInterrupt:
                logger.info("Scheduler stopped by user")
                break
            except Exception as e:
                logger.error(f"Error in scheduler loop: {e}")
                time.sleep(60)  # Wait before retry

    def stop(self) -> None:
        """Stop the scheduler"""
        self._running = False

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

    def _run_check(self, catch_up: bool = False) -> None:
        """Run a single check-and-post cycle via the engine.

        Args:
            catch_up: If True, fetches more videos to compensate for downtime.

        Raises:
            Exception: Re-raised after logging, to allow caller handling.
        """

        try:
            results = asyncio.run(self.engine.check_and_post(catch_up=catch_up))
            self._last_results = results

            # Update health
            self.engine.state.update_last_check_time()
            self.engine.state.save()

        except Exception as e:
            logger.error(f"Check failed: {e}")
            raise
