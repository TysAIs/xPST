"""
Retry policy with exponential backoff for MCP tool calls.

Provides a @retryable decorator that wraps async functions with
configurable retry logic: exponential backoff, max attempts, and
selective exception handling.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryExhaustedError(Exception):
    """Raised when all retry attempts are exhausted."""
    def __init__(self, last_exception: Exception, attempts: int):
        self.last_exception = last_exception
        self.attempts = attempts
        super().__init__(f"All {attempts} retry attempts exhausted: {last_exception}")


def retryable(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator that adds exponential backoff retry to async functions.

    Args:
        max_attempts: Maximum number of attempts (default 3).
        base_delay: Initial delay in seconds (default 1.0).
        max_delay: Maximum delay cap in seconds (default 30.0).
        retryable_exceptions: Exception types that trigger a retry.

    Returns:
        Decorated function with retry logic.
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except retryable_exceptions as exc:
                    last_exc = exc
                    if attempt >= max_attempts:
                        logger.error(
                            "Retry exhausted for %s after %d attempts: %s",
                            func.__name__, attempt, exc,
                        )
                        raise RetryExhaustedError(exc, attempt) from exc
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    logger.warning(
                        "Attempt %d/%d for %s failed: %s — retrying in %.1fs",
                        attempt, max_attempts, func.__name__, exc, delay,
                    )
                    await asyncio.sleep(delay)
            # Should never reach here, but satisfy type checker
            raise RetryExhaustedError(last_exc or RuntimeError("Unknown"), max_attempts)  # pragma: no cover

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as exc:
                    last_exc = exc
                    if attempt >= max_attempts:
                        logger.error(
                            "Retry exhausted for %s after %d attempts: %s",
                            func.__name__, attempt, exc,
                        )
                        raise RetryExhaustedError(exc, attempt) from exc
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    logger.warning(
                        "Attempt %d/%d for %s failed: %s — retrying in %.1fs",
                        attempt, max_attempts, func.__name__, exc, delay,
                    )
                    import time
                    time.sleep(delay)
            raise RetryExhaustedError(last_exc or RuntimeError("Unknown"), max_attempts)  # pragma: no cover

        import inspect
        if inspect.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator
