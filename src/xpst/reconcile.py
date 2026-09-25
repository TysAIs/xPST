"""Reconcile-before-retry for unknown publish outcomes.

A publish attempt can end with an **unknown** outcome: the request was sent but
the response never arrived (timeout, connection drop after submit, a 5xx raised
after the provider accepted the post). Blind-retrying that destination posts a
second copy, because none of the destinations xPST supports de-duplicate a
re-post for us.

This module is the vocabulary and the bookkeeping for the gate:

* :class:`PublishAttempt` — the identifiers recorded for an unknown-outcome
  attempt: platform, the returned post id/urn when the provider gave one, the
  content hash, caption and timestamp.
* :class:`AttemptLedger` — records those attempts per ``platform:content_hash``
  and stores each reconciliation verdict, so a retry can be decided against
  real evidence instead of hope.
* :class:`ReconcileResult` / :class:`ReconcileOutcome` — the verdict:
  ``FOUND`` (the earlier attempt landed → report published, do not upload
  again), ``ABSENT`` (definitively not there → the retry may proceed),
  ``UNKNOWN`` (still cannot tell → block, never blind-retry).
* :func:`reconcile_attempt` — record + run the reconciler + store the verdict.
  A reconciler that itself fails yields ``UNKNOWN``, never a retry.
* :func:`recorded_id_reconciler` — builds a reconciler from the recorded
  post id/urn for destinations with no listing API.
* :func:`uploader_reconciler` — adapts an uploader's read-only
  ``reconcile_publish`` method, when it has a real one.

Reconcilers must use read-only calls only (a listing, a lookup by id) so
reconciliation can never publish to the account it is checking.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

from xpst.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = get_logger(__name__)

# Error text that means "we cannot tell whether the request landed". Deliberately
# specific: a 429/rate limit is not here because it means the provider refused
# the request outright (nothing was published, so a retry can never duplicate).
UNKNOWN_OUTCOME_MARKERS: tuple[str, ...] = (
    "timeout",
    "timed out",
    "connection",
    "connection reset",
    "connection aborted",
    "connection dropped",
    "broken pipe",
    "remote end closed",
    "read timed out",
    "incomplete read",
    "500",
    "502",
    "503",
    "504",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "server error",
)


def is_unknown_outcome(error: str | None) -> bool:
    """Whether ``error`` describes an attempt whose landing is unknown.

    True for failures raised after the request was sent (timeout, connection
    loss, 5xx), where the post may already be live; False for errors that prove
    nothing was published (validation, auth, rate limit).
    """
    if not error:
        return False
    lowered = str(error).lower()
    return any(marker in lowered for marker in UNKNOWN_OUTCOME_MARKERS)


class ReconcileOutcome(str, Enum):
    """Verdict of reading a destination back after an unknown-outcome attempt."""

    FOUND = "found"  # evidence the earlier attempt landed
    ABSENT = "absent"  # definitively not there; a retry cannot duplicate
    UNKNOWN = "unknown"  # cannot tell; a retry must be refused


@dataclass(frozen=True)
class PublishAttempt:
    """The recorded identifiers of an unknown-outcome publish attempt.

    ``post_id``/``post_url`` are whatever the provider returned on the attempt
    (a 5xx after the id was issued still carries it); when the provider returned
    nothing they stay ``None`` and the reconciler has to use a listing.
    """

    platform: str
    content_hash: str
    caption: str = ""
    post_id: str | None = None
    post_url: str | None = None
    error: str | None = None
    timestamp: float = field(default_factory=time.time)

    @property
    def key(self) -> str:
        """Stable ledger key: one unresolved attempt per destination+content."""
        return f"{self.platform}:{self.content_hash}"

    def to_dict(self) -> dict[str, Any]:
        """Serialize for state, logs and the dashboard."""
        return {
            "platform": self.platform,
            "content_hash": self.content_hash,
            "caption": self.caption,
            "post_id": self.post_id,
            "post_url": self.post_url,
            "error": self.error,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class ReconcileResult:
    """What a read-back established about a recorded attempt."""

    outcome: ReconcileOutcome
    post_id: str | None = None
    post_url: str | None = None
    detail: str = ""

    @property
    def is_found(self) -> bool:
        return self.outcome is ReconcileOutcome.FOUND

    @property
    def is_absent(self) -> bool:
        return self.outcome is ReconcileOutcome.ABSENT

    @property
    def is_unknown(self) -> bool:
        return self.outcome is ReconcileOutcome.UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "post_id": self.post_id,
            "post_url": self.post_url,
            "detail": self.detail,
        }


class Reconciler(Protocol):
    """Read-only read-back of a recorded attempt.

    Implementations MUST NOT publish, upload or delete — only read the account
    (a listing) or look up the recorded id/urn.
    """

    async def __call__(self, attempt: PublishAttempt) -> ReconcileResult: ...


@dataclass
class AttemptLedger:
    """Records unknown-outcome attempts and the verdict of reconciling them.

    Keyed by ``platform:content_hash`` so the retry of a destination reconciles
    *that* attempt; re-recording the same destination+content is idempotent and
    keeps the first identifiers (the ones the provider actually returned).
    """

    max_attempts: int = 200
    _attempts: dict[str, PublishAttempt] = field(default_factory=dict)
    _verdicts: dict[str, ReconcileResult] = field(default_factory=dict)

    def record(self, attempt: PublishAttempt) -> PublishAttempt:
        """Record ``attempt``, idempotently, and return the recorded attempt."""
        key = attempt.key
        existing = self._attempts.get(key)
        if existing is not None:
            return existing
        if len(self._attempts) >= self.max_attempts:
            self._evict_one()
        self._attempts[key] = attempt
        return attempt

    def _evict_one(self) -> None:
        """Drop one resolved entry (or the oldest) to bound memory."""
        for key, attempt in self._attempts.items():
            verdict = self.verdict(attempt)
            if verdict is not None and not verdict.is_unknown:
                del self._attempts[key]
                self._verdicts.pop(key, None)
                return
        oldest_key = next(iter(self._attempts))
        del self._attempts[oldest_key]
        self._verdicts.pop(oldest_key, None)

    def attempts(self, platform: str | None = None) -> tuple[PublishAttempt, ...]:
        """All recorded attempts, optionally for one platform."""
        values = tuple(self._attempts.values())
        if platform is None:
            return values
        return tuple(a for a in values if a.platform == platform)

    def unresolved(self, platform: str, content_hash: str) -> tuple[PublishAttempt, ...]:
        """Attempts for this destination+content with no conclusive verdict."""
        attempt = self._attempts.get(f"{platform}:{content_hash}")
        if attempt is None:
            return ()
        verdict = self.verdict(attempt)
        if verdict is None or verdict.is_unknown:
            return (attempt,)
        return ()

    def verdict(self, attempt: PublishAttempt) -> ReconcileResult | None:
        """The stored reconciliation verdict for ``attempt``, if any."""
        return self._verdicts.get(attempt.key)

    def record_verdict(self, attempt: PublishAttempt, result: ReconcileResult) -> None:
        """Store ``result`` as the verdict for ``attempt``."""
        self.record(attempt)
        self._verdicts[attempt.key] = result

    def clear(self) -> None:
        """Forget every recorded attempt (tests and config resets)."""
        self._attempts.clear()
        self._verdicts.clear()


async def reconcile_attempt(
    attempt: PublishAttempt,
    *,
    ledger: AttemptLedger | None = None,
    reconciler: Reconciler | None = None,
) -> ReconcileResult:
    """Record ``attempt``, run the read-back, and store the verdict.

    A missing reconciler or a reconciler that raises resolves to ``UNKNOWN``:
    an unreadable account is not evidence that the post is absent.
    """
    if ledger is not None:
        ledger.record(attempt)

    if reconciler is None:
        result = ReconcileResult(
            outcome=ReconcileOutcome.UNKNOWN,
            detail=f"no read-back path for {attempt.platform}",
        )
    else:
        try:
            result = await reconciler(attempt)
        except Exception as exc:  # noqa: BLE001 - a failed read-back is not absence
            logger.warning(
                "Reconciliation read-back failed for %s: %s", attempt.platform, exc
            )
            result = ReconcileResult(
                outcome=ReconcileOutcome.UNKNOWN,
                detail=f"read-back failed: {str(exc)[:200]}",
            )
        if not isinstance(result, ReconcileResult):
            result = ReconcileResult(
                outcome=ReconcileOutcome.UNKNOWN,
                detail=f"reconciler returned {type(result).__name__}",
            )

    if ledger is not None:
        ledger.record_verdict(attempt, result)
    logger.info(
        "Reconciled %s attempt for %s: %s (%s)",
        attempt.platform,
        attempt.content_hash[:12] or "(no hash)",
        result.outcome.value,
        result.detail,
    )
    return result


def recorded_id_reconciler(
    lookup: Callable[[str], Awaitable[bool | None]],
) -> Reconciler:
    """Reconciler for destinations with no listing: verify the recorded id/urn.

    ``lookup`` returns ``True`` when the id resolves, ``False`` only when the
    platform definitively reports it missing (a 404 on the specific id), and
    ``None`` when the answer is inconclusive. Anything other than ``True``/
    ``False`` stays ``UNKNOWN`` — a network error while looking up the id is not
    proof the post is absent.
    """

    async def _reconcile(attempt: PublishAttempt) -> ReconcileResult:
        if not attempt.post_id:
            return ReconcileResult(
                outcome=ReconcileOutcome.UNKNOWN,
                detail="no recorded post id/urn to verify and no listing available",
            )
        try:
            found = await lookup(attempt.post_id)
        except Exception as exc:  # noqa: BLE001 - inconclusive, never absence
            return ReconcileResult(
                outcome=ReconcileOutcome.UNKNOWN,
                detail=f"lookup of {attempt.post_id} failed: {str(exc)[:200]}",
            )
        if found is True:
            return ReconcileResult(
                outcome=ReconcileOutcome.FOUND,
                post_id=attempt.post_id,
                post_url=attempt.post_url,
                detail=f"recorded post {attempt.post_id} resolves on {attempt.platform}",
            )
        if found is False:
            return ReconcileResult(
                outcome=ReconcileOutcome.ABSENT,
                detail=(
                    f"{attempt.platform} reports recorded post {attempt.post_id} missing"
                ),
            )
        return ReconcileResult(
            outcome=ReconcileOutcome.UNKNOWN,
            detail=f"lookup of {attempt.post_id} was inconclusive",
        )

    return _reconcile


def uploader_reconciler(uploader: Any) -> Reconciler | None:
    """Adapt a destination adapter's read-only ``reconcile_publish`` method.

    Returns ``None`` for adapters without a real coroutine read-back (test
    doubles and adapters that only ever blind-retry), so the gate falls back to
    blocking an unknown outcome rather than inventing a verdict.

    The check reads the method off the class: ``MagicMock(spec=PlatformUploader)``
    auto-creates an async ``reconcile_publish`` attribute that would otherwise
    look like a real read-back in unit tests.
    """
    class_method = getattr(type(uploader), "reconcile_publish", None)
    if class_method is None or not inspect.iscoroutinefunction(class_method):
        return None
    method = uploader.reconcile_publish

    async def _reconcile(attempt: PublishAttempt) -> ReconcileResult:
        return await method(attempt)  # type: ignore[no-any-return]

    return _reconcile


__all__ = [
    "UNKNOWN_OUTCOME_MARKERS",
    "AttemptLedger",
    "PublishAttempt",
    "ReconcileOutcome",
    "ReconcileResult",
    "Reconciler",
    "is_unknown_outcome",
    "reconcile_attempt",
    "recorded_id_reconciler",
    "uploader_reconciler",
]
