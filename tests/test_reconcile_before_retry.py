"""Reconcile-before-retry: a retry must never duplicate an unknown-outcome post.

A publish attempt can end with an UNKNOWN outcome — the request was sent but
the response never arrived (timeout, connection drop after submit, 5xx raised
after the provider accepted the post). Blind-retrying that destination posts a
second copy on every platform here, because none of them de-duplicate.

The gate under test: the attempt is recorded with enough identifiers to find it
again (platform, returned id/urn, content hash, timestamp), and any retry first
READS THE PLATFORM BACK through a mocked client:

* evidence found  -> report published, never re-upload (no duplicate)
* definitively absent -> retry is allowed
* still unknown   -> blocked with a reason, no blind retry
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from xpst.platforms.base import PlatformUploader, UploadResult
from xpst.reconcile import (
    AttemptLedger,
    PublishAttempt,
    ReconcileOutcome,
    ReconcileResult,
    is_unknown_outcome,
    recorded_id_reconciler,
    uploader_reconciler,
)
from xpst.utils.retry import RetryConfig, retry_operation

FAST_RETRY = RetryConfig(max_retries=3, fixed_delays=[0.0, 0.0, 0.0])
UNKNOWN_ERROR = "Connection timeout during upload"


class FakePlatformClient:
    """A mocked destination client: a write path plus a read-only listing.

    ``publish`` mimics an upload whose first response is lost (the post lands
    server-side, the client reports an unknown outcome); ``list_recent`` and
    ``lookup`` are the read APIs a reconciler uses.
    """

    def __init__(
        self,
        *,
        posts: list[dict[str, object]] | None = None,
        lost_responses: int = 1,
        lookup_result: bool | None = None,
        id_on_lost: bool = False,
    ) -> None:
        self.posts: list[dict[str, object]] = list(posts or [])
        self.lost_responses = lost_responses
        self.lookup_result = lookup_result
        # A 5xx raised after the provider accepted the post can still carry the
        # id/urn it issued — that identifier is the fallback read-back key.
        self.id_on_lost = id_on_lost
        self.publish_calls = 0
        self.read_calls: list[str] = []

    async def publish(self, content_hash: str, caption: str = "") -> UploadResult:
        """Write path. The first ``lost_responses`` calls land but report unknown."""
        self.publish_calls += 1
        post_id = f"post-{self.publish_calls}"
        self.posts.append(
            {"post_id": post_id, "content_hash": content_hash, "caption": caption}
        )
        if self.publish_calls <= self.lost_responses:
            return UploadResult(
                success=False,
                error=UNKNOWN_ERROR,
                platform="instagram",
                post_id=post_id if self.id_on_lost else None,
                post_url=f"https://example.test/p/{post_id}" if self.id_on_lost else None,
            )
        return UploadResult(
            success=True,
            post_id=post_id,
            post_url=f"https://example.test/p/{post_id}",
            platform="instagram",
        )

    async def list_recent(self, limit: int = 6) -> list[dict[str, object]]:
        self.read_calls.append("list_recent")
        return list(self.posts)[-limit:]

    async def lookup(self, post_id: str) -> bool | None:
        self.read_calls.append(f"lookup:{post_id}")
        return self.lookup_result


def listing_reconciler(client: FakePlatformClient):
    """Reconciler backed by the mocked client's read-only listing."""

    async def _reconcile(attempt: PublishAttempt) -> ReconcileResult:
        for post in await client.list_recent():
            if post["content_hash"] == attempt.content_hash:
                return ReconcileResult(
                    outcome=ReconcileOutcome.FOUND,
                    post_id=str(post["post_id"]),
                    post_url=f"https://example.test/p/{post['post_id']}",
                    detail="listed by the account after the lost response",
                )
        # A single feed listing cannot prove absence; that stays unknown.
        return ReconcileResult(
            outcome=ReconcileOutcome.UNKNOWN,
            detail="listing does not show it; absence not proven",
        )

    return _reconcile


def always(outcome: ReconcileOutcome, **kwargs: object):
    """Reconciler stub that returns one fixed verdict."""

    async def _reconcile(attempt: PublishAttempt) -> ReconcileResult:
        return ReconcileResult(outcome=outcome, **kwargs)  # type: ignore[arg-type]

    return _reconcile


async def _attempt_upload(
    client: FakePlatformClient,
    ledger: AttemptLedger,
    reconciler,
    *,
    content_hash: str = "hash-abc",
) -> UploadResult:
    return await retry_operation(
        client.publish,
        content_hash,
        "caption",
        config=FAST_RETRY,
        platform="instagram",
        ambiguous_safe=False,
        reconciler=reconciler,
        ledger=ledger,
        content_hash=content_hash,
        caption="caption",
    )


# ---------------------------------------------------------------------------
# The three required outcomes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_then_found_publishes_from_evidence_without_reupload() -> None:
    """The earlier attempt landed: report it published, skip the re-upload."""
    client = FakePlatformClient()
    ledger = AttemptLedger()

    result = await _attempt_upload(client, ledger, listing_reconciler(client))

    assert result.success is True, result.error
    assert result.post_id == "post-1"
    assert result.post_url == "https://example.test/p/post-1"
    assert result.metadata["reconciled"] is True
    assert UNKNOWN_ERROR in result.metadata["original_error"]
    assert client.publish_calls == 1, "reconciled post was re-uploaded (duplicate)"


@pytest.mark.asyncio
async def test_unknown_then_absent_retries_the_destination() -> None:
    """Absence proven by the read-back: the retry is allowed and succeeds."""
    client = FakePlatformClient(lost_responses=1, lookup_result=False, id_on_lost=True)
    ledger = AttemptLedger()

    result = await retry_operation(
        client.publish,
        "hash-abc",
        "caption",
        config=FAST_RETRY,
        platform="instagram",
        ambiguous_safe=False,
        reconciler=recorded_id_reconciler(client.lookup),
        ledger=ledger,
        content_hash="hash-abc",
        caption="caption",
    )

    assert result.success is True
    assert client.publish_calls == 2, "absent post was not retried"
    assert result.post_id == "post-2"


@pytest.mark.asyncio
async def test_unknown_then_unknown_blocks_instead_of_blind_retrying() -> None:
    """Still cannot tell: block with a reason and never re-upload."""
    client = FakePlatformClient()
    ledger = AttemptLedger()

    result = await _attempt_upload(
        client, ledger, always(ReconcileOutcome.UNKNOWN, detail="read-back inconclusive")
    )

    assert result.success is False
    assert client.publish_calls == 1, "unknown outcome was blind-retried"
    error = result.error or ""
    assert "UNVERIFIED" in error
    assert "reconcile" in error.lower()
    assert result.metadata["reconciliation"]["outcome"] == "unknown"


# ---------------------------------------------------------------------------
# Attempt recording (requirement 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_outcome_records_the_attempt_identifiers() -> None:
    """An unknown outcome is recorded: platform, ids, content hash, timestamp."""
    client = FakePlatformClient()
    ledger = AttemptLedger()

    await _attempt_upload(client, ledger, always(ReconcileOutcome.UNKNOWN))

    recorded = ledger.attempts(platform="instagram")
    assert len(recorded) == 1
    attempt = recorded[0]
    assert attempt.platform == "instagram"
    assert attempt.content_hash == "hash-abc"
    assert attempt.timestamp > 0
    assert attempt.error and UNKNOWN_ERROR in attempt.error
    assert ledger.verdict(attempt) is not None


@pytest.mark.asyncio
async def test_found_attempt_is_recorded_with_its_verdict_and_resolved() -> None:
    """A reconciled post leaves no unresolved attempt behind for a retry."""
    client = FakePlatformClient()
    ledger = AttemptLedger()

    await _attempt_upload(client, ledger, listing_reconciler(client))

    assert len(ledger.attempts(platform="instagram")) == 1
    attempt = ledger.attempts(platform="instagram")[0]
    verdict = ledger.verdict(attempt)
    assert verdict is not None and verdict.outcome is ReconcileOutcome.FOUND
    assert ledger.unresolved("instagram", "hash-abc") == ()


# ---------------------------------------------------------------------------
# Recorded id/urn path and classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recorded_id_is_used_when_the_platform_has_no_listing() -> None:
    """The first attempt's id/urn is the fallback evidence source."""
    client = FakePlatformClient(lookup_result=True, id_on_lost=True)
    ledger = AttemptLedger()

    result = await retry_operation(
        client.publish,
        "hash-abc",
        "caption",
        config=FAST_RETRY,
        platform="instagram",
        ambiguous_safe=False,
        reconciler=recorded_id_reconciler(client.lookup),
        ledger=ledger,
        content_hash="hash-abc",
    )

    assert result.success is True
    assert result.post_id == "post-1"
    assert client.publish_calls == 1
    assert client.read_calls == ["lookup:post-1"]


@pytest.mark.asyncio
async def test_reconciler_failure_is_unknown_not_a_retry() -> None:
    """A read-back that itself errors must not authorise a retry."""
    client = FakePlatformClient()

    async def broken(attempt: PublishAttempt) -> ReconcileResult:
        raise RuntimeError("401 Unauthorized")

    ledger = AttemptLedger()
    result = await _attempt_upload(client, ledger, broken)

    assert result.success is False
    assert client.publish_calls == 1
    assert result.metadata["reconciliation"]["outcome"] == "unknown"


def test_unknown_outcome_classification_is_narrow() -> None:
    assert is_unknown_outcome("Connection timeout during upload") is True
    assert is_unknown_outcome("503 Service Unavailable") is True
    assert is_unknown_outcome("video format not supported") is False


@pytest.mark.asyncio
async def test_ambiguous_failure_without_a_reconciler_still_blocks() -> None:
    """No read-back available: keep today's safe behaviour (no blind retry)."""
    client = FakePlatformClient()
    ledger = AttemptLedger()

    result = await retry_operation(
        client.publish,
        "hash-abc",
        config=FAST_RETRY,
        platform="instagram",
        ambiguous_safe=False,
        ledger=ledger,
    )

    assert result.success is False
    assert client.publish_calls == 1


# ---------------------------------------------------------------------------
# Uploader read-back path (existing verification API, read-only)
# ---------------------------------------------------------------------------


class _ReconcilingUploader(PlatformUploader):
    """Minimal real adapter: an unknown write outcome + a read-back verdict.

    Not a mock: the retry gate only trusts a ``reconcile_publish`` defined on
    the adapter's class, which is how a real destination adapter is written.
    """

    def __init__(self, verdict: ReconcileResult) -> None:
        from xpst.config import XPSTConfig

        super().__init__(XPSTConfig())
        self._verdict = verdict
        self.posts = 0
        self.reads: list[PublishAttempt] = []

    async def upload(self, video_path: Path, caption: str) -> UploadResult:
        raise NotImplementedError

    async def check_health(self):  # pragma: no cover - not exercised
        raise NotImplementedError

    async def post_text(self, text: str) -> UploadResult:
        self.posts += 1
        return UploadResult(success=False, error=UNKNOWN_ERROR, platform="instagram")

    async def reconcile_publish(self, attempt: PublishAttempt) -> ReconcileResult:
        self.reads.append(attempt)
        return self._verdict


def _make_upload_service():
    from unittest.mock import MagicMock

    from xpst.services.upload_service import UploadService

    return UploadService(
        video_processor=MagicMock(),
        circuit_breakers=MagicMock(),
        quota_manager=MagicMock(),
        state=MagicMock(),
        notifier=MagicMock(),
        shutdown_handler=MagicMock(),
        config=MagicMock(),
    )


@pytest.mark.asyncio
async def test_upload_service_reconciles_a_text_post_instead_of_duplicating() -> None:
    """The product path: a found read-back publishes once, never twice."""
    service = _make_upload_service()
    uploader = _ReconcilingUploader(
        ReconcileResult(
            outcome=ReconcileOutcome.FOUND,
            post_id="p9",
            post_url="https://example.test/p/p9",
            detail="listed",
        )
    )

    result = await service.upload_text_to_platform(
        uploader, "hello world", "instagram", "vid-1"
    )

    assert result.success is True, result.error
    assert result.post_id == "p9"
    assert uploader.posts == 1, "the service re-posted a reconciled text post"
    assert len(uploader.reads) == 1


@pytest.mark.asyncio
async def test_upload_service_blocks_a_text_retry_while_unknown() -> None:
    """Still unknown after the read-back: no retry, a reason instead."""
    service = _make_upload_service()
    uploader = _ReconcilingUploader(
        ReconcileResult(outcome=ReconcileOutcome.UNKNOWN, detail="listing inconclusive")
    )

    result = await service.upload_text_to_platform(
        uploader, "hello world", "instagram", "vid-2"
    )

    assert result.success is False
    assert uploader.posts == 1
    assert "UNVERIFIED" in (result.error or "")


@pytest.mark.asyncio
async def test_upload_service_retries_a_text_post_proven_absent() -> None:
    """Absence proven by the read-back: the service may publish again."""
    service = _make_upload_service()
    uploader = _ReconcilingUploader(
        ReconcileResult(outcome=ReconcileOutcome.ABSENT, detail="404 on the recorded id")
    )

    result = await service.upload_text_to_platform(
        uploader, "hello world", "instagram", "vid-3"
    )

    assert result.success is False  # the adapter keeps failing the write
    assert uploader.posts >= 2, "an absent post was never retried"


def test_uploader_reconciler_ignores_mock_uploaders() -> None:
    """Test doubles without a real read-back must not become reconcilers."""
    from unittest.mock import MagicMock

    assert uploader_reconciler(MagicMock(spec=PlatformUploader)) is None
    assert uploader_reconciler(None) is None


@pytest.mark.asyncio
async def test_instagram_uploader_reconciles_through_its_own_read_api(tmp_path: Path) -> None:
    """InstagramUploader.reconcile_publish reads back with user_medias only."""
    from xpst.config import XPSTConfig
    from xpst.platforms.instagram import InstagramUploader, _normalize_caption

    caption = "xPST reconcile gate canary"
    calls: list[str] = []
    media = SimpleNamespace(
        pk=987654321,
        code="DdQj3rWCn66",
        caption_text=caption,
        taken_at=None,
        media_type=2,
        product_type="clips",
    )

    class _Client:
        user_id = "1"

        def user_medias(self, user_id, amount=0):
            calls.append("user_medias")
            return [media]

        def clip_upload(self, *args, **kwargs):  # pragma: no cover - never used here
            calls.append("clip_upload")
            raise AssertionError("reconciliation must never publish")

    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    uploader = InstagramUploader(config)
    uploader._get_client = AsyncMock(return_value=_Client())  # type: ignore[method-assign]

    reconciler = uploader_reconciler(uploader)
    assert reconciler is not None

    attempt = PublishAttempt(
        platform="instagram",
        content_hash="hash-abc",
        timestamp=0.0,
        caption=caption,
    )
    with patch("asyncio.sleep", new=AsyncMock()):
        result = await reconciler(attempt)

    assert result.outcome is ReconcileOutcome.FOUND
    assert result.post_id == "987654321"
    assert result.post_url == "https://www.instagram.com/reel/DdQj3rWCn66/"
    assert calls == ["user_medias"]
    assert _normalize_caption(caption)  # sanity: caption helper is importable
