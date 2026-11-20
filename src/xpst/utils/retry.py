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
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from xpst.platforms.base import UploadResult
from xpst.reconcile import (
    AttemptLedger,
    PublishAttempt,
    Reconciler,
    is_unknown_outcome,
    reconcile_attempt,
)
from xpst.utils.errors import categorize_error, is_fatal
from xpst.utils.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

#: Substrings that mark an error as a provider throttle (mirrors
#: UploadService._is_rate_limit; kept here to avoid an import cycle).
_RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "too many requests",
    "throttl",
)


def _looks_like_rate_limit(lowered_error: str) -> bool:
    """True when an error string describes a provider rate limit."""
    return any(marker in lowered_error for marker in _RATE_LIMIT_MARKERS)


#: ``Retry-After: 120`` / ``retry_after=120`` / ``{'retry_after': 120}`` —
#: the shapes providers use when they tell us how long to wait.
_RETRY_AFTER_RE = r"retry[-_ ]after\D{0,3}(\d{1,6})"


def parse_declared_retry_after_s(error: str | None) -> float | None:
    """Seconds the provider itself asked us to wait, or ``None``.

    This is the *provider's* claim ("come back in N seconds"), which is
    different in kind from our own conservative escalation floor: a retry
    scheduled before a provider-declared window expires is provably futile,
    while a retry inside our own floor may still succeed.
    """
    if not error:
        return None
    match = re.search(_RETRY_AFTER_RE, error, re.IGNORECASE)
    return float(match.group(1)) if match else None


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

    def raw_delay(self, attempt: int) -> float:
        """Unjittered scheduled delay for ``attempt`` (planning/diagnostics)."""
        if self.fixed_delays and attempt < len(self.fixed_delays):
            return float(self.fixed_delays[attempt])
        return float(min(self.backoff_base**attempt, self.backoff_max))

    def remaining_budget_s(self, attempt: int) -> float:
        """Total sleeping this call still plans, including the next backoff.

        Used to decide whether a provider-declared ``Retry-After`` can still be
        met in-call: a retry scheduled inside a window the provider itself
        named cannot recover anything, so it is not worth the request. The sum
        is deliberately unjittered (the plan, not one lucky draw), and it is 0
        once the last attempt has been made.
        """
        return sum(
            self.raw_delay(later)
            for later in range(attempt, self.max_retries)
        )


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


def _provider_wait_outlasts_budget(
    config: RetryConfig, attempt: int, error: str | None
) -> bool:
    """Whether the provider's own ``Retry-After`` is beyond this call's budget.

    A retry scheduled before a provider-declared window expires re-asks a
    provider that already said "not yet", so the request is spent for nothing.
    Our own escalation floor is deliberately not consulted here (see
    :func:`retry_operation`).
    """
    declared = parse_declared_retry_after_s(error)
    if declared is None:
        return False
    budget = config.remaining_budget_s(attempt)
    if declared <= budget:
        return False
    logger.info(
        "Not retrying: the provider asked for %.0fs (Retry-After), this call "
        "only plans %.0fs more of backoff",
        declared,
        budget,
    )
    return True


async def _gate_unknown_outcome(
    result: UploadResult,
    *,
    platform: str | None,
    content_hash: str,
    caption: str,
    ledger: AttemptLedger | None,
    reconciler: Reconciler | None,
) -> UploadResult | None:
    """Reconcile an unknown-outcome result before any retry is allowed (G07+).

    Returns a final :class:`UploadResult` when the outcome is settled
    (``FOUND`` -> published from evidence, ``UNKNOWN`` -> blocked with a
    reason), or ``None`` when the read-back proved the post absent, which lets
    the caller retry safely.
    """
    attempt = PublishAttempt(
        platform=platform or result.platform or "unknown",
        content_hash=content_hash,
        caption=caption,
        post_id=result.post_id,
        post_url=result.post_url,
        error=result.error,
    )
    verdict = await reconcile_attempt(
        attempt, ledger=ledger, reconciler=reconciler
    )

    metadata = dict(result.metadata)

    if verdict.is_found:
        # The earlier attempt landed. Report it published with the real
        # identifier and NEVER re-upload it.
        metadata["reconciled"] = True
        metadata["original_error"] = result.error
        metadata["reconciliation"] = verdict.to_dict()
        return UploadResult(
            success=True,
            post_id=verdict.post_id or attempt.post_id,
            post_url=verdict.post_url or attempt.post_url,
            platform=attempt.platform,
            metadata=metadata,
        )

    if verdict.is_absent:
        # Definitively not on the account: the retry cannot duplicate it.
        metadata["reconciliation"] = verdict.to_dict()
        result.metadata = metadata
        return None

    # Still unknown: block instead of blind-retrying. Never a plain failure
    # for a post that may be live, and never a silent success either.
    metadata["reconciliation"] = verdict.to_dict()
    metadata["blocked_retry"] = True
    reason = (
        f"UNVERIFIED on {attempt.platform}: the previous attempt's outcome is "
        f"still unknown after reconcile — refusing to retry and duplicate the "
        f"post. {verdict.detail}"
    )
    if result.error:
        reason = f"{reason} | original error: {result.error}"
    return UploadResult(
        success=False,
        error=reason,
        platform=attempt.platform,
        metadata=metadata,
    )


async def retry_operation(
    operation: Callable[..., Any],
    *args: Any,
    config: RetryConfig | None = None,
    on_retry: Callable[[int, Exception], Any] | None = None,
    platform: str | None = None,
    ambiguous_safe: bool = True,
    reconciler: Reconciler | None = None,
    ledger: AttemptLedger | None = None,
    content_hash: str = "",
    caption: str = "",
    on_rate_limit: Callable[[str], None] | None = None,
    **kwargs: Any,
) -> Any:
    """
    Retry an async operation with error categorization.

    Fatal errors (401, 403, invalid format, etc.) are raised immediately.
    Retryable errors (429, timeout, etc.) are retried with backoff.

    ``on_rate_limit`` is called with the error text whenever a failed result
    or exception looks like a rate limit, at the moment it is first seen —
    including when the uploader returned the throttle as a failed
    ``UploadResult`` that no keyword re-raises. This is how the persistent
    cooldown state machine always learns about a throttle, even on paths
    where the in-call retry never fires.

    Retry ownership (kanban t_2ba3e805): the uploader that talked to the
    provider decides whether another attempt can help.

    * ``UploadResult.retryable is False`` is authoritative — the failure is
      terminal and this layer does not retry it, whatever its error text says.
      The ``on_rate_limit`` hook still fires and an unknown outcome is still
      reconciled first (a terminal *retry* must never skip the read-back that
      proves a post landed).
    * ``retryable is None`` keeps the legacy keyword heuristic untouched, so
      adapters that declare nothing behave exactly as before.
    * ``retryable is True`` stays advisory: honouring it can only *add*
      attempts (a Threads container poll costs minutes), so it is not enforced
      here.
    * A provider-declared ``Retry-After`` longer than this layer's whole
      remaining backoff budget stops the retry: re-asking inside a window the
      provider itself named cannot recover anything. A hint inside the budget
      still retries — short throttles are what in-call recovery is for. Our
      own escalation floor is *not* consulted: it is policy about when the
      next post may start, not a claim that an earlier attempt would fail.

    An UNKNOWN outcome (timeout, connection drop, 5xx raised after the request
    was sent) is settled by a read-back before any retry: pass ``reconciler``
    (a read-only destination read-back) and ``ledger`` (records the attempt)
    so a retry can never duplicate the post. ``ambiguous_safe=True`` is the
    exception, for destinations whose server de-duplicates a re-post (X).

    Args:
        operation: Async function to retry
        *args: Arguments to pass to operation
        config: Retry configuration
        on_retry: Callback called on each retry
        platform: Platform name for error categorization and reconciliation
        ambiguous_safe: Whether an unknown outcome may be retried blindly
        reconciler: Read-only read-back used to settle an unknown outcome
        ledger: Records the unknown-outcome attempt and its verdict
        content_hash: Content identity of the attempted post
        caption: Caption of the attempted post
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
                if on_rate_limit is not None and _looks_like_rate_limit(error_msg):
                    on_rate_limit(str(result.error))
                # Unknown outcomes (timeout/connection/5xx after submit) can
                # mean the request SUCCEEDED server-side with the response
                # lost. Re-invoking the operation then double-posts on
                # platforms without server-side duplicate detection (G07).
                # Instead of blind-retrying, reconcile the destination first:
                # FOUND -> published, ABSENT -> safe to retry, UNKNOWN -> block.
                if is_unknown_outcome(error_msg):
                    if ambiguous_safe:
                        raise Exception(result.error)
                    gated = await _gate_unknown_outcome(
                        result,
                        platform=platform,
                        content_hash=content_hash,
                        caption=caption,
                        ledger=ledger,
                        reconciler=reconciler,
                    )
                    if gated is not None:
                        return gated
                    # The read-back proved nothing landed — retry it.
                    raise Exception(result.error)
                # The uploader talked to the provider and declared the failure
                # terminal. Its word wins over the error text: an adapter that
                # already exhausted its own attempts must not be re-run by this
                # layer just because "try again" appears in its message.
                if result.retryable is False:
                    logger.info(
                        "Not retrying %s: the uploader declared the failure "
                        "terminal (retryable=False): %s",
                        platform or result.platform or "destination",
                        result.error,
                    )
                    return result
                retryable_keywords = [
                    "rate_limit",
                    "rate limit",
                    "too many requests",
                    "429",
                    "temporarily",
                    "try again",
                ]
                if any(kw in error_msg for kw in retryable_keywords):
                    if attempt < config.max_retries and _provider_wait_outlasts_budget(
                        config, attempt, str(result.error)
                    ):
                        return result
                    raise Exception(result.error)

            return result
        except config.retryable_exceptions as e:
            last_exception = e

            if on_rate_limit is not None and _looks_like_rate_limit(str(e).lower()):
                on_rate_limit(str(e))

            # Check if error is fatal - don't retry
            if is_fatal(e, platform):
                cat = categorize_error(e, platform)
                logger.error(
                    f"Fatal error ({cat.category.value}), not retrying: {e}"
                )
                raise

            if attempt < config.max_retries:
                if _provider_wait_outlasts_budget(
                    config, attempt, str(e)
                ):
                    # The provider named a window this call cannot wait out;
                    # report the failure now instead of re-asking inside it.
                    raise
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
