"""
Retry logic with exponential backoff for xPST

Provides configurable retry logic with:
- Exponential backoff with specific delays (1s, 2s, 4s default)
- Jitter to prevent thundering herd
- Configurable max retries
- Error categorization (retryable vs fatal)
- Per-exception retry policies

Example usage:
    from xpst.utils.retry import retry_async, RetryConfig

    config = RetryConfig(max_retries=3, backoff_base=2)

    @retry_async(config)
    async def upload_video():
        # Upload logic here
        pass
"""

import asyncio
import functools
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from xpst.utils.errors import categorize_error, is_fatal
from xpst.utils.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


@dataclass
class RetryConfig:
    """
    Configuration for retry behavior.

    Attributes:
        max_retries: Maximum number of retry attempts
        backoff_base: Base for exponential backoff (2^n seconds)
        backoff_max: Maximum backoff time in seconds
        jitter: Add random jitter to backoff (0.0 to 1.0)
        retryable_exceptions: Tuple of exception types to retry
        fixed_delays: Fixed delay sequence in seconds (overrides exponential if set)
    """
    max_retries: int = 3
    backoff_base: int = 2
    backoff_max: float = 60.0
    jitter: float = 0.1
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,)
    fixed_delays: list[float] | None = None

    def get_backoff(self, attempt: int) -> float:
        """
        Calculate backoff time for a given attempt.

        Uses fixed delays if configured, otherwise exponential backoff.

        Args:
            attempt: Current attempt number (0-based)

        Returns:
            Backoff time in seconds
        """
        # Use fixed delay sequence if provided
        if self.fixed_delays and attempt < len(self.fixed_delays):
            backoff = self.fixed_delays[attempt]
        else:
            # Exponential backoff: base^n
            backoff = min(self.backoff_base ** attempt, self.backoff_max)

        # Add jitter
        if self.jitter > 0:
            jitter_range = backoff * self.jitter
            backoff += random.uniform(-jitter_range, jitter_range)

        return max(0, backoff)


# Predefined configs for common scenarios
# Quick: 2 attempts, 1s base backoff (for non-critical operations)
QUICK_RETRY = RetryConfig(max_retries=2, backoff_base=1, backoff_max=5)

# Standard: 3 attempts with fixed 1s/2s/4s delays (matches spec).
# This is the default used by the engine for all platform uploads.
STANDARD_RETRY = RetryConfig(
    max_retries=3,
    backoff_base=2,
    backoff_max=30,
    fixed_delays=[1.0, 2.0, 4.0],
)

# Aggressive: 5 attempts with exponential backoff up to 60s.
# Used for critical operations where eventual success matters more than speed.
AGGRESSIVE_RETRY = RetryConfig(max_retries=5, backoff_base=2, backoff_max=60)


def retry_sync(
    config: RetryConfig | None = None,
    on_retry: Callable[[int, Exception], None] | None = None,
    platform: str | None = None,
) -> Callable:
    """
    Decorator for synchronous retry logic with error categorization.

    Args:
        config: Retry configuration
        on_retry: Callback called on each retry (attempt, exception)
        platform: Platform name for error categorization

    Returns:
        Decorated function
    """
    if config is None:
        config = STANDARD_RETRY

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        """Apply retry logic to a synchronous function."""
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            """Wrapper that retries func on retryable exceptions."""

            last_exception = None

            for attempt in range(config.max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except config.retryable_exceptions as e:
                    last_exception = e

                    # Check if error is fatal - don't retry
                    if is_fatal(e, platform):
                        logger.error(
                            f"Fatal error for {func.__name__}, not retrying: {e}"
                        )
                        raise

                    if attempt < config.max_retries:
                        backoff = config.get_backoff(attempt)
                        cat = categorize_error(e, platform)
                        logger.warning(
                            f"Retry {attempt + 1}/{config.max_retries} "
                            f"({cat.category.value}) after {backoff:.1f}s: {e}"
                        )

                        if on_retry:
                            on_retry(attempt, e)

                        time.sleep(backoff)
                    else:
                        logger.error(f"All {config.max_retries} retries exhausted: {e}")

            raise last_exception  # type: ignore

        return wrapper

    return decorator


def retry_async(
    config: RetryConfig | None = None,
    on_retry: Callable[[int, Exception], Any] | None = None,
    platform: str | None = None,
) -> Callable:
    """
    Decorator for asynchronous retry logic with error categorization.

    Args:
        config: Retry configuration
        on_retry: Callback called on each retry (attempt, exception)
        platform: Platform name for error categorization

    Returns:
        Decorated async function
    """
    if config is None:
        config = STANDARD_RETRY

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        """Apply retry logic to an asynchronous function."""
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            """Wrapper that retries func on retryable exceptions."""

            last_exception = None

            for attempt in range(config.max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except config.retryable_exceptions as e:
                    last_exception = e

                    # Check if error is fatal - don't retry
                    if is_fatal(e, platform):
                        logger.error(
                            f"Fatal error for {func.__name__}, not retrying: {e}"
                        )
                        raise

                    if attempt < config.max_retries:
                        backoff = config.get_backoff(attempt)
                        cat = categorize_error(e, platform)
                        logger.warning(
                            f"Retry {attempt + 1}/{config.max_retries} "
                            f"({cat.category.value}) after {backoff:.1f}s: {e}"
                        )

                        if on_retry:
                            result = on_retry(attempt, e)
                            if asyncio.iscoroutine(result):
                                await result

                        await asyncio.sleep(backoff)
                    else:
                        logger.error(f"All {config.max_retries} retries exhausted: {e}")

            raise last_exception  # type: ignore

        return wrapper

    return decorator


async def retry_operation(
    operation: Callable[..., Any],
    *args: Any,
    config: RetryConfig | None = None,
    on_retry: Callable[[int, Exception], Any] | None = None,
    platform: str | None = None,
    **kwargs: Any,
) -> Any:
    """
    Retry an async operation with error categorization.

    Fatal errors (401, 403, invalid format, etc.) are raised immediately.
    Retryable errors (429, 503, timeout, etc.) are retried with backoff.

    Args:
        operation: Async function to retry
        *args: Arguments to pass to operation
        config: Retry configuration
        on_retry: Callback called on each retry
        platform: Platform name for error categorization
        **kwargs: Keyword arguments to pass to operation

    Returns:
        Result of the operation

    Raises:
        Exception: If all retries fail or error is fatal
    """
    if config is None:
        config = STANDARD_RETRY

    last_exception = None

    for attempt in range(config.max_retries + 1):
        try:
            result = await operation(*args, **kwargs)

            # Check if the operation returned a failed UploadResult
            # (uploaders catch exceptions and return UploadResult(success=False))
            if (
                hasattr(result, "success")
                and not result.success
                and hasattr(result, "error")
                and result.error
            ):
                error_msg = str(result.error).lower()
                retryable_keywords = [
                    "timeout",
                    "connection",
                    "503",
                    "rate_limit",
                    "rate limit",
                    "too many requests",
                    "429",
                    "502",
                    "500",
                    "temporarily",
                    "try again",
                ]
                if any(kw in error_msg for kw in retryable_keywords):
                    raise Exception(result.error)

            return result
        except config.retryable_exceptions as e:
            last_exception = e

            # Check if error is fatal - don't retry
            if is_fatal(e, platform):
                cat = categorize_error(e, platform)
                logger.error(
                    f"Fatal error ({cat.category.value}), not retrying: {e}"
                )
                raise

            if attempt < config.max_retries:
                backoff = config.get_backoff(attempt)
                cat = categorize_error(e, platform)
                logger.warning(
                    f"Retry {attempt + 1}/{config.max_retries} "
                    f"({cat.category.value}) after {backoff:.1f}s: {e}"
                )

                if on_retry:
                    result = on_retry(attempt, e)
                    if asyncio.iscoroutine(result):
                        await result

                await asyncio.sleep(backoff)
            else:
                logger.error(f"All {config.max_retries} retries exhausted: {e}")

    raise last_exception  # type: ignore
