"""Retry ownership: a terminal declaration stops the retry layer.

Two adapters can both decide "this failure is over": the uploader (which talks
to the provider) and the service retry layer (which wraps it). Until now the
retry layer ignored the uploader's answer — ``UploadResult.retryable`` was
recorded, exported to ``health``/``failures`` and shown in the dashboard, but
``retry_operation`` re-ran a failed upload whenever its **error text** looked
retryable. So an adapter that had already exhausted its own attempts and
declared ``retryable=False`` was retried anyway, and a provider that had just
said "too many requests" got three more of them.

This module pins the contract decided in kanban t_2ba3e805:

1. ``retryable=False`` is **authoritative**: one call, no retry, no sleeping.
   The cooldown still learns about the throttle (``on_rate_limit``) and an
   unknown-outcome attempt is still reconciled before it is called terminal —
   stopping a retry must not skip the read-back that proves a post landed.
2. ``retryable is None`` keeps the legacy keyword heuristic exactly as it was,
   so every adapter that has not declared an opinion is untouched.
3. A provider-declared ``Retry-After`` that outlasts the retry layer's whole
   remaining backoff budget stops the retry: re-asking inside a window the
   provider itself named cannot recover anything. A hint *inside* the budget
   still retries (short throttles are exactly what in-call recovery is for).
4. A static retryable-looking **error text** may not be shipped next to
   ``retryable=False`` — the audit test at the bottom fails if a new site does
   that, so the next author has to decide instead of silently contradicting
   themselves.

``retryable=True`` stays advisory: honouring it can only *add* attempts (each
Threads container poll costs minutes), so it needs its own measurement.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import xpst.utils.retry as retry_mod
from xpst.platforms.base import UploadResult
from xpst.reconcile import AttemptLedger, ReconcileOutcome, ReconcileResult
from xpst.services.upload_service import UploadService
from xpst.state import StateManager
from xpst.utils.cooldown_store import CooldownStore
from xpst.utils.retry import STANDARD_RETRY, RetryConfig, retry_operation

#: A throttle worded exactly like the Instagram instagrapi adapter's terminal
#: refusals: retryable-looking text, and the adapter says it is over.
THROTTLE_TEXT = "IG_RATE_LIMITED: Too many requests, try again later"

FAST_RETRY = RetryConfig(max_retries=3, fixed_delays=[0.0, 0.0, 0.0], jitter=0)


@pytest.fixture
def sleeps(monkeypatch):
    """Record every scheduled backoff instead of waiting for it."""
    recorded: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float, *a: Any, **kw: Any) -> None:
        recorded.append(float(delay))
        await real_sleep(0)

    monkeypatch.setattr(
        retry_mod,
        "asyncio",
        SimpleNamespace(sleep=fake_sleep, iscoroutine=asyncio.iscoroutine),
    )
    return recorded


class ScriptedOperation:
    """An upload call that counts its invocations and always fails as told."""

    def __init__(self, result_factory, *, raise_exc: Exception | None = None):
        self._factory = result_factory
        self._raise = raise_exc
        self.calls = 0

    async def __call__(self, *args: Any, **kwargs: Any) -> UploadResult:
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        return self._factory(self.calls)


def terminal_failure(error: str = THROTTLE_TEXT) -> UploadResult:
    """A failure the uploader declares terminal (no further attempts help)."""
    return UploadResult(
        success=False,
        error=error,
        platform="instagram",
        retryable=False,
        metadata={"uploader_attempts": 4},
    )


def undeclared_failure(error: str = THROTTLE_TEXT) -> UploadResult:
    """A failure with no retryability opinion (legacy adapter shape)."""
    return UploadResult(success=False, error=error, platform="instagram")


# ---------------------------------------------------------------------------
# 1. retryable=False is authoritative
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_declared_terminal_failure_is_not_retried(sleeps):
    """One call, no backoff: the uploader already gave up."""
    operation = ScriptedOperation(lambda _n: terminal_failure())

    result = await retry_operation(
        operation, config=STANDARD_RETRY, platform="instagram"
    )

    assert result.success is False
    assert operation.calls == 1, (
        f"a retryable=False result was retried ({operation.calls} calls) — the "
        "retry layer overrode the uploader's terminal declaration"
    )
    assert sleeps == [], f"a terminal declaration still slept {sleeps}"


@pytest.mark.asyncio
async def test_terminal_declaration_is_returned_unchanged(sleeps):
    """The caller keeps the adapter's own error and metadata."""
    operation = ScriptedOperation(lambda _n: terminal_failure())

    result = await retry_operation(
        operation, config=STANDARD_RETRY, platform="instagram"
    )

    assert result.error == THROTTLE_TEXT
    assert result.retryable is False
    assert result.metadata["uploader_attempts"] == 4


@pytest.mark.asyncio
async def test_declared_terminal_failure_still_reports_to_the_cooldown(sleeps):
    """Stopping the retry must not hide the throttle from the state machine."""
    seen: list[str] = []
    operation = ScriptedOperation(lambda _n: terminal_failure())

    await retry_operation(
        operation,
        config=STANDARD_RETRY,
        platform="instagram",
        on_rate_limit=seen.append,
    )

    assert seen == [THROTTLE_TEXT], "the cooldown never learned about the throttle"
    assert operation.calls == 1


@pytest.mark.asyncio
async def test_undeclared_retryable_text_keeps_the_legacy_heuristic(sleeps):
    """retryable=None is untouched: the keyword heuristic still retries."""
    operation = ScriptedOperation(lambda _n: undeclared_failure())

    with pytest.raises(Exception, match="Too many requests"):
        await retry_operation(operation, config=STANDARD_RETRY, platform="instagram")

    assert operation.calls == STANDARD_RETRY.max_retries + 1, (
        "an adapter with no retryability opinion changed behaviour"
    )
    assert len(sleeps) == STANDARD_RETRY.max_retries


@pytest.mark.asyncio
async def test_undeclared_non_retryable_text_is_still_not_retried(sleeps):
    """The heuristic half is unchanged in the other direction too."""
    operation = ScriptedOperation(
        lambda _n: undeclared_failure("IG_INVALID_FORMAT: Video format not supported")
    )

    result = await retry_operation(
        operation, config=STANDARD_RETRY, platform="instagram"
    )

    assert result.success is False
    assert operation.calls == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_terminal_declaration_still_reconciles_an_unknown_outcome():
    """Stopping a retry must not skip the read-back that proves a post landed.

    A 5xx-shaped failure may still mean the post is live. The reconcile gate
    runs before the terminal check, so the result is published from evidence
    instead of being reported as a plain failure.
    """

    async def reconciler(attempt):  # noqa: ANN001
        return ReconcileResult(
            outcome=ReconcileOutcome.FOUND,
            post_id="ig-media-9",
            post_url="https://www.instagram.com/reel/found9/",
            detail="listed by the account",
        )

    operation = ScriptedOperation(
        lambda _n: UploadResult(
            success=False,
            error="500 Server Error for url: https://i.instagram.com/api/v1/media/configure/",
            platform="instagram",
            retryable=False,
        )
    )
    ledger = AttemptLedger()

    result = await retry_operation(
        operation,
        config=FAST_RETRY,
        platform="instagram",
        ambiguous_safe=False,
        reconciler=reconciler,
        ledger=ledger,
        content_hash="hash-t",
        caption="caption",
    )

    assert result.success is True, result.error
    assert result.metadata.get("reconciled") is True
    assert operation.calls == 1


# ---------------------------------------------------------------------------
# 2. A provider-declared wait that outlasts the retry budget stops the retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_wait_beyond_the_retry_budget_stops_after_one_attempt(sleeps):
    """Retry-After 900 vs a 6s budget: the other three asks are provably futile."""
    operation = ScriptedOperation(
        lambda _n: undeclared_failure(f"{THROTTLE_TEXT} (Retry-After: 900)")
    )

    result = await retry_operation(
        operation, config=STANDARD_RETRY, platform="instagram"
    )

    assert result.success is False
    assert operation.calls == 1, (
        f"{operation.calls} provider requests inside a window the provider said "
        "would last 900s; the remaining retry budget is only "
        f"{STANDARD_RETRY.remaining_budget_s(0):.0f}s"
    )
    assert sleeps == []


@pytest.mark.asyncio
async def test_provider_wait_inside_the_retry_budget_still_retries(sleeps):
    """Retry-After 1 fits the budget: short throttles keep in-call recovery."""
    operation = ScriptedOperation(
        lambda _n: undeclared_failure(f"{THROTTLE_TEXT} (Retry-After: 1)")
    )

    with pytest.raises(Exception, match="Too many requests"):
        await retry_operation(operation, config=STANDARD_RETRY, platform="instagram")

    assert operation.calls == STANDARD_RETRY.max_retries + 1
    assert len(sleeps) == STANDARD_RETRY.max_retries


@pytest.mark.asyncio
async def test_throttle_without_a_hint_keeps_retrying(sleeps):
    """No Retry-After: our own ladder floor is policy, not the provider's word."""
    operation = ScriptedOperation(lambda _n: undeclared_failure())

    with pytest.raises(Exception, match="Too many requests"):
        await retry_operation(operation, config=STANDARD_RETRY, platform="instagram")

    assert operation.calls == STANDARD_RETRY.max_retries + 1


@pytest.mark.asyncio
async def test_raised_exception_with_a_long_provider_wait_is_not_retried(sleeps):
    """The same rule applies to the raise path (X/twikit style failures)."""
    operation = ScriptedOperation(
        lambda _n: None,
        raise_exc=Exception("429 Client Error: Too Many Requests (Retry-After: 900)"),
    )

    with pytest.raises(Exception, match="Too Many Requests"):
        await retry_operation(operation, config=STANDARD_RETRY, platform="x")

    assert operation.calls == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_raised_exception_with_a_short_provider_wait_is_retried(sleeps):
    operation = ScriptedOperation(
        lambda _n: None,
        raise_exc=Exception("429 Client Error: Too Many Requests (Retry-After: 2)"),
    )

    with pytest.raises(Exception, match="Too Many Requests"):
        await retry_operation(operation, config=STANDARD_RETRY, platform="x")

    assert operation.calls == STANDARD_RETRY.max_retries + 1


def test_retry_budget_shrinks_with_each_attempt():
    """The budget is the unjittered sum of every remaining scheduled delay."""
    assert STANDARD_RETRY.remaining_budget_s(0) == pytest.approx(7.0)  # 1 + 2 + 4
    assert STANDARD_RETRY.remaining_budget_s(1) == pytest.approx(6.0)  # 2 + 4
    assert STANDARD_RETRY.remaining_budget_s(2) == pytest.approx(4.0)  # 4
    assert STANDARD_RETRY.remaining_budget_s(99) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 3. The same contract through the real service seam
# ---------------------------------------------------------------------------


class TerminalThrottleUploader:
    """Uploader double: a throttle it declares terminal, counted per call."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def upload(self, video_path, caption, **kwargs):  # noqa: ANN001, ANN003
        self._calls.append("upload")
        return terminal_failure()

    def health_check(self):  # noqa: ANN001
        return None


class _NullVideoProcessor:
    ffmpeg_path = None

    def get_video_info(self, path):  # noqa: ANN001
        return {"streams": [], "format": {}}

    async def encode_for_platform(self, video_path, platform, **kw):  # noqa: ANN001, ANN003
        return video_path


class _NullBreakers:
    _breakers: dict = {}

    def allow_request(self, platform):  # noqa: ANN001
        return True

    def record_success(self, platform):  # noqa: ANN001
        pass

    def record_failure(self, platform, error):  # noqa: ANN001
        pass


class _NullQuota:
    def preflight(self, platform):  # noqa: ANN001
        pass

    def record_upload(self, platform):  # noqa: ANN001
        pass


def make_service(tmp_path: Path) -> UploadService:
    # Encoding profiles resolve to permissive doubles; only ``config_dir``
    # needs a real value (the cooldown store persists under it).
    config = MagicMock()
    config.config_dir = str(tmp_path)
    service = UploadService(
        video_processor=_NullVideoProcessor(),
        circuit_breakers=_NullBreakers(),
        quota_manager=_NullQuota(),
        state=StateManager(str(tmp_path)),
        notifier=SimpleNamespace(
            notify_upload_failure=lambda **kw: None,
            notify_circuit_breaker=lambda *a, **kw: None,
        ),
        shutdown_handler=SimpleNamespace(
            update_phase=lambda *a, **kw: None, add_temp_file=lambda p: None
        ),
        config=config,
        anti_bot=None,
    )
    # Config is a bare mock here; the cooldown store needs only a dir.
    service.rate_cooldowns = CooldownStore(tmp_path)
    return service


@pytest.mark.asyncio
async def test_service_stops_at_one_provider_request_when_the_uploader_declares_terminal(
    tmp_path, sleeps
):
    """End to end: one throttled post costs ONE provider request and still cools."""
    calls: list[str] = []
    uploader = TerminalThrottleUploader(calls)
    service = make_service(tmp_path)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 8192)

    result = await service.upload_to_platform(
        uploader=uploader,
        video_path=video,
        caption="retry ownership",
        platform_name="instagram",
        video_id="vid-terminal",
    )

    assert result.success is False
    assert len(calls) == 1, (
        f"one throttled post cost {len(calls)} provider requests; the uploader "
        "declared the failure terminal"
    )
    assert service.rate_cooldowns.remaining_s("instagram") > 0, (
        "the cooldown did not learn about the terminal throttle"
    )
    assert service.state.is_video_posted("vid-terminal", "instagram") is False


# ---------------------------------------------------------------------------
# 4. Audit: no retryable=False site may carry a retryable-looking static text
# ---------------------------------------------------------------------------

#: Static error texts that legitimately contain a retryable keyword while the
#: result is declared terminal — none today. Add an entry (with a reason) only
#: when the text describes the *provider's* wording and the declaration is the
#: considered answer, and say why in the review.
STATIC_TEXT_ALLOWLIST: dict[str, str] = {}

_RETRY_KEYWORDS = (
    "rate_limit",
    "rate limit",
    "too many requests",
    "429",
    "temporarily",
    "try again",
)

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"


def _static_error_text(node: ast.Call) -> str | None:
    """Constant part of an ``error=`` argument (None when fully dynamic)."""
    for kw in node.keywords:
        if kw.arg != "error":
            continue
        parts: list[str] = []
        value = kw.value
        nodes = value.values if isinstance(value, ast.JoinedStr) else [value]
        for sub in nodes:
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                parts.append(sub.value)
        return "".join(parts) if parts else None
    return None


def _terminal_sites() -> list[tuple[str, int, str, str]]:
    """Every ``UploadResult(... retryable=False ...)`` with a static error text."""
    sites: list[tuple[str, int, str, str]] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "UploadResult"):
                continue
            declared = None
            for kw in node.keywords:
                if kw.arg == "retryable" and isinstance(kw.value, ast.Constant):
                    declared = kw.value.value
            if declared is not False:
                continue
            text = _static_error_text(node)
            if text is None:
                continue
            sites.append((str(path.relative_to(SRC_ROOT.parent)), node.lineno, text, ""))
    return sites


def test_audit_scanned_the_whole_tree():
    """Guard against a vacuous scan (a moved root would otherwise pass)."""
    sites = _terminal_sites()
    assert len(sites) >= 25, (
        f"only {len(sites)} retryable=False sites with a static error text were "
        f"found under {SRC_ROOT} — the audit is not looking at the real tree"
    )


def test_no_terminal_site_carries_a_retryable_looking_static_text():
    """A static '429'/'try again' next to retryable=False is a contradiction.

    Before this contract the retry layer read the text and retried anyway; now
    the declaration wins, so the two must agree or the site silently becomes
    one-shot. Failing here forces the author to pick one.
    """
    contradictions = []
    for path, lineno, text, _ in _terminal_sites():
        key = f"{path}:{lineno}"
        if key in STATIC_TEXT_ALLOWLIST:
            continue
        lowered = text.lower()
        hits = [kw for kw in _RETRY_KEYWORDS if kw in lowered]
        if hits:
            contradictions.append(f"{key} declares retryable=False but its error "
                                  f"text says {hits}: {text[:120]!r}")
    assert not contradictions, "\n".join(contradictions)


def test_every_retryable_false_site_is_terminal_by_declaration():
    """Sanity: the declaration itself is serialized for the operator surfaces."""
    assert terminal_failure().to_dict()["terminal"] is True
    assert terminal_failure().to_dict()["retryable"] is False
