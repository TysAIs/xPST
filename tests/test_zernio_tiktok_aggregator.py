"""Unit tests for the Zernio TikTok aggregator route.

Everything here runs against recorded/mocked vendor shapes
(``tests/fixtures/zernio/``): no API key exists yet and no request is made. The
properties under test are the ones that make this route safe to wire up before a
key exists:

* an API-key auth mode is a first-class vocabulary value, never ``oauth``;
* the route is not a platform — it cannot fork TikTok's identity;
* publishing is refused when ``PUBLIC_TO_EVERYONE`` is not offered, and on a
  capacity refusal no post is created;
* ``207`` (a 2xx that means failure) is a failure;
* ``published`` with a null ``platformPostUrl`` is PENDING, never PUBLISHED, and
  never carries a fabricated id;
* readback rejects TikTok's fake-id page shape (no ``itemStruct``) and never
  reports VERIFIED on a read failure;
* the API key stays out of config.yaml.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from xpst import visibility
from xpst.auth_status import _auth_mode_for
from xpst.config import XPSTConfig
from xpst.content import TEXT_LIMITS
from xpst.platforms.base import (
    DeleteOutcome,
    PlatformRegistry,
    ReadbackState,
    UploadOutcome,
)
from xpst.platforms.tiktok import TikTokUploader
from xpst.platforms.tiktok_route import build_tiktok_uploader, tiktok_provider
from xpst.platforms.zernio_tiktok import (
    PUBLIC_LEVEL,
    ZernioResponse,
    ZernioTikTokUploader,
    ZernioTransportError,
    map_zernio_error,
)
from xpst.providers import AuthMode
from xpst.reconcile import PublishAttempt

FIXTURES = Path(__file__).parent / "fixtures" / "zernio"
CAPTION = "hello world test post"
VIDEO_ID = "7412345678901234567"


# ── helpers ───────────────────────────────────────────────────────────────


def payload(name: str) -> dict[str, Any]:
    """A recorded vendor response body."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def page(name: str) -> str:
    """A recorded HTML page."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def response(name: str, status: int = 200) -> ZernioResponse:
    """A recorded vendor response as the client would return it."""
    return ZernioResponse(status=status, payload=payload(name), text=json.dumps(payload(name)))


def make_config(tmp_path: Path, *, provider: str = "zernio", account_id: str = "acc_tiktok_1") -> XPSTConfig:
    """A config for the aggregator route, in an isolated config dir."""
    config = XPSTConfig()
    config.config_dir = str(tmp_path / "cfg")
    # Only TikTok is under test: every other destination stays disabled so no
    # surface this module touches probes another platform.
    for name in ("youtube", "x", "instagram", "threads", "facebook", "messenger", "bluesky", "rumble"):
        account = getattr(config, name, None)
        if account is not None:
            account.enabled = False
    config.tiktok.enabled = True
    config.tiktok.username = "example.creator"
    config.tiktok.provider = provider
    config.tiktok.aggregator.account_id = account_id
    config.tiktok.aggregator.profile_id = "profile_1"
    config.tiktok.aggregator.api_key_ref = "zernio_api_key"
    return config


def make_video(tmp_path: Path) -> Path:
    """A tiny real file: the uploader reads bytes, it does not decode video."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake-video-bytes" * 64)
    return video


class FakeZernioClient:
    """Stand-in for :class:`ZernioClient` with recorded responses.

    Every method records its name so a test can assert what the adapter did
    *not* call (no create on a refusal, no create during reconciliation).
    """

    def __init__(self, **responses: Any) -> None:
        self._responses = responses
        self.calls: list[str] = []
        self.requests: list[dict[str, Any]] = []

    async def _answer(self, name: str, default: Any = None) -> Any:
        self.calls.append(name)
        value = self._responses.get(name, default)
        if isinstance(value, Exception):
            raise value
        return value

    async def accounts(self) -> Any:
        return await self._answer("accounts", response("accounts.json"))

    async def account_health(self, account_id: str) -> Any:
        self.requests.append({"account_health": account_id})
        return await self._answer("account_health", response("account_health.json"))

    async def creator_info(self, account_id: str) -> Any:
        self.requests.append({"creator_info": account_id})
        return await self._answer("creator_info", response("creator_info_public.json"))

    async def publish_check(self, account_id: str, *, profile_id: str = "", caption_length: int = 0) -> Any:
        self.requests.append({"publish_check": account_id, "caption_length": caption_length})
        return await self._answer("publish_check", ZernioResponse(200, {"canPublish": True}, "{}"))

    async def presign_media(self, *, filename: str, size: int, content_type: str) -> Any:
        self.requests.append({"presign": filename, "size": size})
        return await self._answer("presign_media", response("presign.json"))

    async def upload_media(self, upload_url: str, data: bytes, *, content_type: str) -> Any:
        self.requests.append({"upload_media": upload_url, "bytes": len(data)})
        return await self._answer("upload_media", ZernioResponse(200, None, ""))

    async def create_post(self, body: dict[str, Any], *, idempotency_key: str) -> Any:
        self.requests.append({"create_post": body, "idempotency_key": idempotency_key})
        return await self._answer("create_post", response("post_published.json", 201))

    async def get_post(self, post_id: str) -> Any:
        return await self._answer("get_post", response("post_published.json"))

    async def account_posts(self, account_id: str) -> Any:
        return await self._answer("account_posts", response("posts_listing.json"))

    async def delete_post(self, post_id: str) -> Any:
        return await self._answer("delete_post", ZernioResponse(204, None, ""))

    async def fetch_public_page(self, url: str) -> tuple[int, str]:
        self.requests.append({"fetch_public_page": url})
        return await self._answer("fetch_public_page", (200, page("readback_real_page.html")))


def made_uploader(config: XPSTConfig, client: FakeZernioClient) -> ZernioTikTokUploader:
    uploader = ZernioTikTokUploader(config, client=client)  # type: ignore[arg-type]
    uploader.remember_caption(CAPTION)
    return uploader


# ── vocabulary: auth mode, route, registry ────────────────────────────────


def test_api_key_is_a_distinct_auth_mode() -> None:
    assert AuthMode.API_KEY.value == "api_key"
    assert AuthMode.API_KEY is not AuthMode.OAUTH
    assert AuthMode.API_KEY is not AuthMode.CONTENT_POSTING_API


def test_aggregator_route_is_public_only() -> None:
    row = visibility.support("tiktok", route=visibility.ROUTE_TIKTOK_AGGREGATOR)
    assert row is not None
    assert dict(row.levels) == {visibility.PUBLIC: PUBLIC_LEVEL}
    assert row.requestable == (visibility.PUBLIC,)
    assert row.level_for(visibility.PRIVATE) is None
    refused = visibility.refusal("tiktok", visibility.PRIVATE, route=visibility.ROUTE_TIKTOK_AGGREGATOR)
    assert refused is not None and "cannot honour" in refused
    assert visibility.refusal("tiktok", visibility.PUBLIC, route=visibility.ROUTE_TIKTOK_AGGREGATOR) is None


def test_publish_route_follows_the_configured_provider(tmp_path: Path) -> None:
    aggregator = make_config(tmp_path, provider="zernio")
    local = make_config(tmp_path, provider="local")
    assert tiktok_provider(aggregator) == "zernio"
    assert visibility.publish_route(aggregator, "tiktok") == visibility.ROUTE_TIKTOK_AGGREGATOR
    assert visibility.publish_route(local, "tiktok") == visibility.ROUTE_API


def test_route_adapter_does_not_become_a_platform(tmp_path: Path) -> None:
    PlatformRegistry.auto_discover()
    platforms = PlatformRegistry.list_platforms()
    assert "zernio_tiktok" not in platforms
    assert "zernio" not in platforms
    assert PlatformRegistry.get("tiktok", make_config(tmp_path)).__class__ is TikTokUploader
    assert ZernioTikTokUploader.auto_register is False


def test_route_builder_selects_by_config(tmp_path: Path) -> None:
    assert isinstance(build_tiktok_uploader(make_config(tmp_path, provider="local")), TikTokUploader)
    assert isinstance(
        build_tiktok_uploader(make_config(tmp_path, provider="zernio")), ZernioTikTokUploader
    )
    bad = make_config(tmp_path)
    bad.tiktok.provider = "zernoi"
    with pytest.raises(ValueError, match="accounts.tiktok.provider"):
        build_tiktok_uploader(bad)


def test_manifests_report_api_key_never_oauth(tmp_path: Path) -> None:
    aggregator = make_config(tmp_path, provider="zernio")
    manifests = [ZernioTikTokUploader(aggregator).manifest, TikTokUploader(aggregator).manifest]
    for manifest in manifests:
        assert manifest.name == "tiktok"
        assert manifest.auth_mode is AuthMode.API_KEY
        assert manifest.auth_mode is not AuthMode.OAUTH
        assert manifest.is_official_api is False
    assert "oauth" not in {capability.value for capability in manifests[0].capabilities}


def test_auth_status_reports_api_key_for_the_aggregator(tmp_path: Path) -> None:
    assert _auth_mode_for(make_config(tmp_path, provider="zernio"), "tiktok") == "api_key"
    assert _auth_mode_for(make_config(tmp_path, provider="local"), "tiktok") == "source_only"


def test_config_persists_the_reference_not_the_secret(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    path = tmp_path / "config.yaml"
    config.save(str(path))
    text = path.read_text(encoding="utf-8")
    assert "provider: zernio" in text
    assert "api_key_ref: zernio_api_key" in text
    assert "vendor_schedule: false" in text
    # No key material, and no field that could carry it.
    assert "zernio_api_key_value" not in text
    assert "secret-key-material" not in text


def test_aggregator_config_has_no_secret_field() -> None:
    """The config block can only NAME the credential, never carry it."""
    import dataclasses

    from xpst.config import TikTokAggregatorConfig

    names = {item.name for item in dataclasses.fields(TikTokAggregatorConfig)}
    assert "api_key_ref" in names
    assert names.isdisjoint({"api_key", "key", "api_secret", "token", "secret"})


def test_auth_status_surface_reports_api_key_for_the_route(tmp_path: Path) -> None:
    """The live auth-status payload must not label this route oauth."""
    import asyncio

    from xpst.auth_status import collect_live_auth_status_async

    status = asyncio.run(collect_live_auth_status_async(make_config(tmp_path)))
    entry = status["tiktok"]
    assert entry["auth_mode"] == "api_key"
    assert entry["auth_mode"] != "oauth"
    assert "AGGREGATOR_NOT_CONFIGURED" in (entry.get("error") or "")
    assert entry["roles"]["video_destination"]["auth_mode"] == "api_key"
    assert entry["roles"]["video_destination"]["auth_mode"] != "oauth"


def test_offline_catalog_does_not_claim_a_configured_aggregator_without_a_key(
    tmp_path: Path,
) -> None:
    """No key, no ready destination — the catalog is not a probe, so it must not guess."""
    from xpst.provider_truth import canonical_provider_catalog
    from xpst.utils.credentials import CredentialStore

    config = make_config(tmp_path)
    entry = canonical_provider_catalog(config)["providers"]
    tiktok = next(item for item in entry if item["name"] == "tiktok")
    assert tiktok["auth_mode"] == "api_key"
    assert tiktok["state"] != "ready"
    assert tiktok["role_status"]["video_destination"]["ready"] is False

    # With a credential stored the destination counts as configured.
    CredentialStore(config.config_dir).store("zernio_api_key", "fake-aggregator-key")
    entry = canonical_provider_catalog(config)["providers"]
    tiktok = next(item for item in entry if item["name"] == "tiktok")
    assert tiktok["role_status"]["video_destination"]["ready"] is True
    assert "fake-aggregator-key" not in json.dumps(tiktok)


def test_config_roundtrip_reads_the_aggregator_block(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.tiktok.aggregator.vendor_schedule = True
    path = tmp_path / "config.yaml"
    config.save(str(path))
    loaded = XPSTConfig.load(str(path))
    assert loaded.tiktok.provider == "zernio"
    assert loaded.tiktok.aggregator.account_id == "acc_tiktok_1"
    assert loaded.tiktok.aggregator.profile_id == "profile_1"
    assert loaded.tiktok.aggregator.vendor_schedule is True


# ── health ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_requires_a_tiktok_account(tmp_path: Path) -> None:
    client = FakeZernioClient(accounts=ZernioResponse(200, {"accounts": []}, "{}"))
    health = await ZernioTikTokUploader(make_config(tmp_path), client=client).check_health()  # type: ignore[arg-type]
    assert health.authenticated is True
    assert health.session_valid is False
    assert health.error is not None and "AGGREGATOR_NO_TIKTOK_ACCOUNT" in health.error


@pytest.mark.asyncio
async def test_health_is_healthy_for_a_postable_account(tmp_path: Path) -> None:
    health = await ZernioTikTokUploader(make_config(tmp_path), client=FakeZernioClient()).check_health()  # type: ignore[arg-type]
    assert health.authenticated and health.session_valid and health.error is None
    assert health.details["account_id"] == "acc_tiktok_1"
    assert health.details["lane"] == "business"
    assert PUBLIC_LEVEL in health.details["privacy_levels"]
    assert health.details["route"] == visibility.ROUTE_TIKTOK_AGGREGATOR


@pytest.mark.asyncio
async def test_health_without_a_key_makes_no_request(tmp_path: Path) -> None:
    client = FakeZernioClient()
    uploader = ZernioTikTokUploader(make_config(tmp_path))  # no injected client, no stored key
    health = await uploader.check_health()
    assert health.authenticated is False
    assert health.error is not None and "AGGREGATOR_NOT_CONFIGURED" in health.error
    assert client.calls == []


@pytest.mark.asyncio
async def test_health_needs_reconnect_is_not_healthy(tmp_path: Path) -> None:
    client = FakeZernioClient(
        account_health=ZernioResponse(200, {"canPost": True, "needsReconnect": True}, "{}")
    )
    health = await ZernioTikTokUploader(make_config(tmp_path), client=client).check_health()  # type: ignore[arg-type]
    assert health.authenticated is True
    assert health.session_valid is False
    assert health.error is not None and "NEEDS_RECONNECT" in health.error


# ── upload: refusals before any post exists ───────────────────────────────


@pytest.mark.asyncio
async def test_refuses_when_public_level_is_not_offered(tmp_path: Path) -> None:
    client = FakeZernioClient(creator_info=response("creator_info_no_public.json"))
    result = await made_uploader(make_config(tmp_path), client).upload(make_video(tmp_path), CAPTION)
    assert result.outcome == UploadOutcome.FAILED
    assert result.success is False
    assert result.retryable is False
    assert result.error is not None and "TIKTOK_VISIBILITY_UNAVAILABLE" in result.error
    assert PUBLIC_LEVEL in result.error
    assert "create_post" not in client.calls, "a post must never be created on a visibility refusal"
    assert "presign_media" not in client.calls


@pytest.mark.asyncio
async def test_capacity_refusal_creates_no_post(tmp_path: Path) -> None:
    client = FakeZernioClient(publish_check=response("capacity_full.json"))
    result = await made_uploader(make_config(tmp_path), client).upload(make_video(tmp_path), CAPTION)
    assert result.outcome == UploadOutcome.FAILED
    assert result.retryable is True
    assert result.error is not None and "TIKTOK_DIRECT_POST_CAPACITY" in result.error
    assert "create_post" not in client.calls
    assert "presign_media" not in client.calls


@pytest.mark.asyncio
async def test_private_visibility_is_refused_before_any_call(tmp_path: Path) -> None:
    client = FakeZernioClient()
    result = await made_uploader(make_config(tmp_path), client).upload(
        make_video(tmp_path), CAPTION, visibility=visibility.PRIVATE
    )
    assert result.outcome == UploadOutcome.FAILED
    assert result.error is not None and visibility.VISIBILITY_UNAVAILABLE_CODE in result.error
    assert client.calls == []


@pytest.mark.asyncio
async def test_caption_over_the_limit_is_refused(tmp_path: Path) -> None:
    client = FakeZernioClient()
    too_long = "x" * (TEXT_LIMITS["tiktok"] + 1)
    result = await made_uploader(make_config(tmp_path), client).upload(make_video(tmp_path), too_long)
    assert result.outcome == UploadOutcome.FAILED
    assert result.error is not None and "TEXT_TOO_LONG" in result.error
    assert client.calls == []


@pytest.mark.asyncio
async def test_missing_account_id_refuses_without_requests(tmp_path: Path) -> None:
    client = FakeZernioClient()
    config = make_config(tmp_path, account_id="")
    result = await made_uploader(config, client).upload(make_video(tmp_path), CAPTION)
    assert result.outcome == UploadOutcome.FAILED
    assert result.error is not None and "AGGREGATOR_NOT_CONFIGURED" in result.error
    assert client.calls == []


# ── upload: interpreting the vendor's answer ──────────────────────────────


@pytest.mark.asyncio
async def test_published_with_url_is_published_with_the_video_id(tmp_path: Path) -> None:
    client = FakeZernioClient()
    result = await made_uploader(make_config(tmp_path), client).upload(make_video(tmp_path), CAPTION)
    assert result.outcome == UploadOutcome.PUBLISHED
    assert result.post_id == VIDEO_ID
    assert result.post_url is not None and VIDEO_ID in result.post_url
    assert result.metadata["vendor_post_id"] == "zpost_1"
    assert result.metadata["route"] == visibility.ROUTE_TIKTOK_AGGREGATOR
    create = next(item for item in client.requests if "create_post" in item)
    body = create["create_post"]
    assert body["publishNow"] is True
    assert body["tiktokSettings"]["privacy_level"] == PUBLIC_LEVEL
    assert body["tiktokSettings"]["content_preview_confirmed"] is True
    assert body["tiktokSettings"]["express_consent_given"] is True
    assert create["idempotency_key"].startswith("xpst-")


@pytest.mark.asyncio
async def test_published_without_a_url_is_pending_and_invents_no_id(tmp_path: Path) -> None:
    client = FakeZernioClient(create_post=response("post_published_url_null.json"))
    result = await made_uploader(make_config(tmp_path), client).upload(make_video(tmp_path), CAPTION)
    assert result.outcome == UploadOutcome.PENDING
    assert result.success is False
    assert result.post_id is None, "a vendor post id is not a platform post id"
    assert result.post_url is None
    assert result.metadata["vendor_post_id"] == "zpost_2"


@pytest.mark.asyncio
async def test_207_with_a_failed_status_is_a_failure(tmp_path: Path) -> None:
    client = FakeZernioClient(create_post=response("post_207_failed.json", 207))
    result = await made_uploader(make_config(tmp_path), client).upload(make_video(tmp_path), CAPTION)
    assert result.outcome == UploadOutcome.FAILED
    assert result.success is False
    assert result.post_id is None
    assert result.retryable is False
    assert result.error is not None and "TIKTOK_CONTENT_FLAGGED" in result.error
    assert result.metadata["http_status"] == 207
    assert result.metadata.get("vendor_post_id") == "zpost_3"


@pytest.mark.asyncio
async def test_uninterpretable_207_is_a_failure_not_a_pending_post(tmp_path: Path) -> None:
    """A 207 whose body says nothing is not evidence that anything was posted."""
    client = FakeZernioClient(create_post=ZernioResponse(207, {"id": "zpost_4"}, "{}"))
    result = await made_uploader(make_config(tmp_path), client).upload(make_video(tmp_path), CAPTION)
    assert result.outcome == UploadOutcome.FAILED
    assert result.post_id is None
    assert result.error is not None and "TIKTOK_PLATFORM_REJECTED" in result.error
    assert result.metadata["http_status"] == 207


@pytest.mark.asyncio
async def test_401_maps_to_aggregator_auth_failed(tmp_path: Path) -> None:
    client = FakeZernioClient(create_post=response("post_401.json", 401))
    result = await made_uploader(make_config(tmp_path), client).upload(make_video(tmp_path), CAPTION)
    assert result.outcome == UploadOutcome.FAILED
    assert result.error is not None and "AGGREGATOR_AUTH_FAILED" in result.error
    assert result.retryable is False


@pytest.mark.asyncio
async def test_preflight_read_failure_is_retryable_and_not_an_unknown_outcome(tmp_path: Path) -> None:
    """A failure before the create cannot have posted anything, so a retry is safe."""
    client = FakeZernioClient(creator_info=ZernioTransportError("timed out"))
    result = await made_uploader(make_config(tmp_path), client).upload(make_video(tmp_path), CAPTION)
    assert result.outcome == UploadOutcome.FAILED
    assert result.retryable is True
    assert result.error is not None and "AGGREGATOR_UNAVAILABLE" in result.error
    assert "unknown_outcome" not in result.metadata
    assert "create_post" not in client.calls


@pytest.mark.asyncio
async def test_transport_failure_is_an_unknown_outcome(tmp_path: Path) -> None:
    from xpst.reconcile import is_unknown_outcome

    client = FakeZernioClient(create_post=ZernioTransportError("timed out"))
    result = await made_uploader(make_config(tmp_path), client).upload(make_video(tmp_path), CAPTION)
    assert result.outcome == UploadOutcome.FAILED
    assert result.metadata["unknown_outcome"] is True
    assert result.error is not None and "AGGREGATOR_UNKNOWN_OUTCOME" in result.error
    assert is_unknown_outcome(result.error) is True, "the retry gate must reconcile, not blind-retry"


@pytest.mark.asyncio
async def test_vendor_scheduling_needs_the_opt_in(tmp_path: Path) -> None:
    client = FakeZernioClient()
    refused = await made_uploader(make_config(tmp_path), client).upload(
        make_video(tmp_path), CAPTION, scheduled_for="2030-01-01T10:00:00+00:00"
    )
    assert refused.outcome == UploadOutcome.FAILED
    assert refused.error is not None and "TIKTOK_SCHEDULE_LOCAL" in refused.error

    config = make_config(tmp_path)
    config.tiktok.aggregator.vendor_schedule = True
    client = FakeZernioClient()
    accepted = await made_uploader(config, client).upload(
        make_video(tmp_path), CAPTION, scheduled_for="2030-01-01T10:00:00+00:00"
    )
    assert accepted.outcome == UploadOutcome.PUBLISHED
    body = next(item for item in client.requests if "create_post" in item)["create_post"]
    assert body["publishNow"] is False
    assert body["scheduledFor"] == "2030-01-01T10:00:00+00:00"


@pytest.mark.asyncio
async def test_past_vendor_schedule_is_refused(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.tiktok.aggregator.vendor_schedule = True
    result = await made_uploader(config, FakeZernioClient()).upload(
        make_video(tmp_path), CAPTION, scheduled_for="2020-01-01T10:00:00+00:00"
    )
    assert result.outcome == UploadOutcome.FAILED
    assert result.error is not None and "TIKTOK_SCHEDULE_IN_PAST" in result.error


# ── error mapping table ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kwargs", "code", "retryable"),
    [
        ({"http_status": 401, "error_type": "authentication_error"}, "AGGREGATOR_AUTH_FAILED", False),
        ({"http_status": 402}, "AGGREGATOR_BILLING_BLOCKED", False),
        ({"http_status": 403, "code": "account_disconnected"}, "TIKTOK_ACCOUNT_NEEDS_RECONNECT", False),
        ({"http_status": 403, "error_type": "permission_error"}, "AGGREGATOR_PERMISSION_DENIED", False),
        ({"http_status": 404}, "AGGREGATOR_POST_NOT_FOUND", False),
        ({"http_status": 409, "code": "idempotency_conflict"}, "AGGREGATOR_IN_FLIGHT", True),
        ({"http_status": 409}, "TIKTOK_DUPLICATE_CONTENT", False),
        ({"http_status": 429, "error_type": "rate_limited"}, "AGGREGATOR_RATE_LIMITED", True),
        ({"http_status": 429, "code": "daily_limit_reached"}, "TIKTOK_QUOTA_EXHAUSTED", False),
        ({"http_status": 400, "error_type": "invalid_field_value"}, "TIKTOK_REQUEST_INVALID", False),
        ({"http_status": 503, "error_type": "temporarily_unavailable"}, "AGGREGATOR_UNAVAILABLE", True),
        ({"http_status": 500, "error_type": "internal_error"}, "AGGREGATOR_ERROR", True),
        (
            {"http_status": 502, "error_type": "platform_error", "platform_error": "video URL returned an error"},
            "TIKTOK_MEDIA_FETCH_FAILED",
            True,
        ),
    ],
)
def test_error_mapping_table(kwargs: dict[str, Any], code: str, retryable: bool) -> None:
    mapped = map_zernio_error(**kwargs)
    assert mapped.code == code
    assert mapped.retryable is retryable


def test_error_mapping_ignores_unrelated_wording() -> None:
    """A reworded ``error`` string must not change the code.

    Only ``type``/``code``/status decide; the message is a fallback last resort
    and unknown wording maps to the generic error.
    """
    mapped = map_zernio_error(http_status=418, platform_error="something the vendor reworded")
    assert mapped.code == "AGGREGATOR_ERROR"
    assert mapped.retryable is False


def test_error_detail_is_redacted() -> None:
    mapped = map_zernio_error(
        http_status=401,
        error_type="authentication_error",
        platform_error="request failed for api_key=fakevalue123",
    )
    assert "fakevalue123" not in mapped.detail
    assert "<redacted>" in mapped.detail


# ── readback ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_readback_verifies_a_real_page(tmp_path: Path) -> None:
    client = FakeZernioClient()
    result = await made_uploader(make_config(tmp_path), client).readback(VIDEO_ID)
    assert result.state == ReadbackState.VERIFIED
    assert result.url == f"https://www.tiktok.com/@example.creator/video/{VIDEO_ID}"
    assert result.visibility == "public"
    fetched = next(item for item in client.requests if "fetch_public_page" in item)["fetch_public_page"]
    assert "oembed" not in fetched, "oEmbed echoes any id and would produce a false PASS"
    assert "/video/" in fetched


@pytest.mark.asyncio
async def test_readback_rejects_the_fake_id_page(tmp_path: Path) -> None:
    """The reproduced fake-id signature must never be reported as verified."""
    client = FakeZernioClient(fetch_public_page=(200, page("readback_fake_id_page.html")))
    result = await made_uploader(make_config(tmp_path), client).readback(VIDEO_ID)
    assert result.state == ReadbackState.NOT_FOUND
    assert result.state != ReadbackState.VERIFIED
    assert result.verified is False


@pytest.mark.asyncio
async def test_readback_rejects_a_login_wall_as_unknown(tmp_path: Path) -> None:
    client = FakeZernioClient(fetch_public_page=(200, page("readback_login_wall.html")))
    result = await made_uploader(make_config(tmp_path), client).readback(VIDEO_ID)
    assert result.state == ReadbackState.UNKNOWN


@pytest.mark.asyncio
async def test_readback_does_not_call_absence_on_a_network_error(tmp_path: Path) -> None:
    client = FakeZernioClient(fetch_public_page=ZernioTransportError("connection reset"))
    result = await made_uploader(make_config(tmp_path), client).readback(VIDEO_ID)
    assert result.state == ReadbackState.UNKNOWN
    assert result.state != ReadbackState.NOT_FOUND


@pytest.mark.asyncio
async def test_readback_rejects_a_different_video_id(tmp_path: Path) -> None:
    client = FakeZernioClient(fetch_public_page=(200, page("readback_wrong_id_page.html")))
    result = await made_uploader(make_config(tmp_path), client).readback(VIDEO_ID)
    assert result.state == ReadbackState.NOT_FOUND


@pytest.mark.asyncio
async def test_readback_is_unknown_when_the_author_does_not_match(tmp_path: Path) -> None:
    client = FakeZernioClient(fetch_public_page=(200, page("readback_wrong_author_page.html")))
    result = await made_uploader(make_config(tmp_path), client).readback(VIDEO_ID)
    assert result.state == ReadbackState.UNKNOWN


@pytest.mark.asyncio
async def test_readback_does_not_fabricate_a_url_without_a_handle(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.tiktok.username = ""
    result = await made_uploader(config, FakeZernioClient()).readback(VIDEO_ID)
    assert result.state == ReadbackState.UNKNOWN
    assert result.url is None


# ── delete ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_of_a_published_video_is_unsupported(tmp_path: Path) -> None:
    client = FakeZernioClient()
    result = await made_uploader(make_config(tmp_path), client).delete(VIDEO_ID)
    assert result.outcome == DeleteOutcome.UNSUPPORTED
    assert result.ok is False
    assert "no delete API" in (result.detail or "")
    assert client.calls == []


@pytest.mark.asyncio
async def test_delete_of_a_vendor_post_reports_deleted(tmp_path: Path) -> None:
    client = FakeZernioClient()
    result = await made_uploader(make_config(tmp_path), client).delete("zpost_9")
    assert result.outcome == DeleteOutcome.DELETED
    assert result.ok is True
    assert "delete_post" in client.calls


@pytest.mark.asyncio
async def test_delete_failure_is_never_reported_as_deleted(tmp_path: Path) -> None:
    client = FakeZernioClient(delete_post=ZernioResponse(500, {"type": "internal_error"}, "{}"))
    result = await made_uploader(make_config(tmp_path), client).delete("zpost_9")
    assert result.outcome == DeleteOutcome.PENDING
    assert result.ok is False
    assert result.detail is not None and "AGGREGATOR_ERROR" in result.detail


# ── reconciliation ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_finds_the_post_by_content(tmp_path: Path) -> None:
    client = FakeZernioClient()
    attempt = PublishAttempt(platform="tiktok", content_hash="abc123", caption=CAPTION)
    result = await made_uploader(make_config(tmp_path), client).reconcile_publish(attempt)
    assert result.outcome.value == "found"
    assert VIDEO_ID in str(result.post_url)
    assert "create_post" not in client.calls, "reconciliation must never re-issue the create"


@pytest.mark.asyncio
async def test_reconcile_reports_absent_only_on_a_readable_empty_listing(tmp_path: Path) -> None:
    client = FakeZernioClient(account_posts=ZernioResponse(200, {"posts": []}, "{}"))
    attempt = PublishAttempt(platform="tiktok", content_hash="abc123", caption=CAPTION)
    result = await made_uploader(make_config(tmp_path), client).reconcile_publish(attempt)
    assert result.outcome.value == "absent"
    assert "create_post" not in client.calls


@pytest.mark.asyncio
async def test_reconcile_is_unknown_without_identifiers(tmp_path: Path) -> None:
    client = FakeZernioClient(account_posts=ZernioResponse(200, {"posts": []}, "{}"))
    attempt = PublishAttempt(platform="tiktok", content_hash="abc123")
    result = await made_uploader(make_config(tmp_path), client).reconcile_publish(attempt)
    assert result.outcome.value == "unknown"
    assert "no id, URL or caption" in result.detail


@pytest.mark.asyncio
async def test_reconcile_is_unknown_when_the_listing_fails(tmp_path: Path) -> None:
    client = FakeZernioClient(account_posts=ZernioTransportError("timed out"))
    attempt = PublishAttempt(platform="tiktok", content_hash="abc123", caption=CAPTION)
    result = await made_uploader(make_config(tmp_path), client).reconcile_publish(attempt)
    assert result.outcome.value == "unknown"
    assert "create_post" not in client.calls
