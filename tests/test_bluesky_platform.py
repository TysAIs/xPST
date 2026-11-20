"""Bluesky destination: capability declarations and adapter behaviour.

Every network call is served by an ``httpx.MockTransport`` that emulates the
PDS's XRPC endpoints, so this module proves the request/response contract
without a live account (a live post is not part of this change: content is
authored by the operator, and posting is paused).

The identity in these fixtures is fictional on purpose — the public-safety scan
fails a real handle anywhere in the tree.
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import yaml

if TYPE_CHECKING:
    from pathlib import Path

from xpst.config import XPSTConfig
from xpst.content import ContentType, content_profile, text_limit
from xpst.platforms.base import PlatformRegistry, PlatformUploader
from xpst.platforms.bluesky import (
    BLUESKY_MAX_IMAGE_BYTES,
    BLUESKY_MAX_IMAGES_PER_POST,
    CRED_APP_PASSWORD,
    CRED_IDENTIFIER,
    CRED_SESSION,
    BlueskyUploader,
)
from xpst.providers import AuthMode, ProviderCapability
from xpst.utils.credentials import CredentialStore

IDENTIFIER = "tester.example"
APP_PASSWORD = "test-app-password-not-real"
DID = "did:plc:fixtureaccount"
SERVICE = "https://pds.example"

#: The canonical 1x1 PNG (public domain). Used so the image header reader
#: answers without ffprobe, exactly like a real post image.
ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FakePds:
    """A minimal AT Protocol PDS: the five endpoints this adapter calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.records: list[dict[str, Any]] = []
        self.blobs: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        #: Set to force the next createRecord to answer 401 once (expired token).
        self.expire_access_token_once = False
        self.create_session_count = 0
        self.rate_limited = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.rsplit("/xrpc/", 1)[-1]
        self.calls.append((request.method, path))
        body = json.loads(request.content) if request.content and request.headers.get("content-type") == "application/json" else {}

        if path == "com.atproto.server.createSession":
            self.create_session_count += 1
            if body.get("identifier") != IDENTIFIER or body.get("password") != APP_PASSWORD:
                return httpx.Response(
                    401,
                    json={"error": "AuthenticationRequired", "message": "Invalid identifier or password"},
                )
            return httpx.Response(
                200,
                json={
                    "did": DID,
                    "handle": IDENTIFIER,
                    "accessJwt": "access-token-1",
                    "refreshJwt": "refresh-token-1",
                    "active": True,
                },
            )

        if path == "com.atproto.server.refreshSession":
            return httpx.Response(
                200,
                json={"did": DID, "handle": IDENTIFIER, "accessJwt": "access-token-2", "refreshJwt": "refresh-token-2"},
            )

        if path == "com.atproto.server.getSession":
            return httpx.Response(200, json={"did": DID, "handle": IDENTIFIER, "active": True})

        if path == "com.atproto.repo.uploadBlob":
            self.blobs.append(
                {"content_type": request.headers.get("content-type"), "size": len(request.content)}
            )
            return httpx.Response(
                200,
                json={
                    "blob": {
                        "$type": "blob",
                        "ref": {"$link": f"bafyblob{len(self.blobs)}"},
                        "mimeType": request.headers.get("content-type"),
                        "size": len(request.content),
                    }
                },
            )

        if path == "com.atproto.repo.createRecord":
            if self.rate_limited:
                return httpx.Response(
                    429,
                    headers={"retry-after": "900"},
                    json={"error": "RateLimitExceeded", "message": "Too many requests"},
                )
            if self.expire_access_token_once:
                self.expire_access_token_once = False
                return httpx.Response(401, json={"error": "ExpiredToken", "message": "Token has expired"})
            self.records.append(body)
            rkey = f"3krecord{len(self.records)}"
            return httpx.Response(
                200, json={"uri": f"at://{DID}/app.bsky.feed.post/{rkey}", "cid": "bafyrecord"}
            )

        if path == "com.atproto.repo.getRecord":
            rkey = request.url.params.get("rkey", "")
            if f"at://{DID}/app.bsky.feed.post/{rkey}" in self.deleted:
                return httpx.Response(400, json={"error": "RecordNotFound", "message": "Could not locate record"})
            return httpx.Response(
                200,
                json={"uri": f"at://{DID}/app.bsky.feed.post/{rkey}", "value": {"$type": "app.bsky.feed.post"}},
            )

        if path == "com.atproto.repo.deleteRecord":
            self.deleted.append(f"at://{body.get('repo')}/app.bsky.feed.post/{body.get('rkey')}")
            return httpx.Response(200, json={})

        return httpx.Response(404, json={"error": "MethodNotImplemented", "message": path})


@pytest.fixture
def config(tmp_path: Path) -> XPSTConfig:
    from xpst.config import BlueskyAccountConfig

    cfg = XPSTConfig()
    cfg.config_dir = str(tmp_path)
    cfg.bluesky = BlueskyAccountConfig(enabled=True, identifier=IDENTIFIER, service=SERVICE)
    store = CredentialStore(str(tmp_path))
    store.store(CRED_IDENTIFIER, IDENTIFIER)
    store.store(CRED_APP_PASSWORD, APP_PASSWORD)
    return cfg


@pytest.fixture
def pds() -> FakePds:
    return FakePds()


@pytest.fixture
def uploader(config: XPSTConfig, pds: FakePds) -> BlueskyUploader:
    instance = BlueskyUploader(config)
    instance.transport = httpx.MockTransport(pds.handler)
    return instance


@pytest.fixture
def image(tmp_path: Path) -> Path:
    path = tmp_path / "post.png"
    path.write_bytes(ONE_PIXEL_PNG)
    return path


# ── capability declarations ─────────────────────────────────────────────────


def test_manifest_declares_text_image_and_carousel_but_no_video(config: XPSTConfig) -> None:
    manifest = BlueskyUploader(config).manifest
    assert manifest.name == "bluesky"
    assert manifest.auth_mode is AuthMode.APP_PASSWORD
    assert ProviderCapability.CAROUSEL in manifest.capabilities
    assert ProviderCapability.OFFICIAL_API in manifest.capabilities
    # The declared labels are the content contract's declared labels, which the
    # contract test compares for equality — keep them in the same order.
    assert tuple(manifest.extra["content"]) == ("text", "image", "carousel")
    assert manifest.extra["video"] is False
    assert manifest.extra["max_caption_length"] == 300
    assert manifest.extra["max_images_per_post"] == 4


def test_content_contract_matches_the_adapter() -> None:
    profile = content_profile("bluesky")
    assert profile is not None
    assert profile.implemented == frozenset(
        {ContentType.TEXT, ContentType.IMAGE, ContentType.CAROUSEL}
    )
    assert ContentType.VIDEO not in profile.implemented
    assert text_limit("bluesky") == 300


def test_video_is_not_implemented_and_the_inherited_refusal_says_so() -> None:
    """No video override, and the inherited method refuses by name.

    ``tests/test_content_contract.py`` asserts that an uploader method is
    overridden exactly when the content table calls the type implemented, so the
    absence of an ``upload`` override is what keeps "Bluesky has no video" true
    in code as well as in the table.
    """
    assert BlueskyUploader.upload is PlatformUploader.upload
    assert "bluesky" in PlatformRegistry.list_platforms()


@pytest.mark.asyncio
async def test_the_base_refusal_is_explicit_for_a_video(uploader: BlueskyUploader, tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"not a real video")
    result = await uploader.upload(video, "caption")
    assert result.success is False
    assert result.retryable is False
    assert "BLUESKY_VIDEO_UNSUPPORTED" in (result.error or "")


# ── text posts ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_text_writes_an_app_bsky_feed_post_record(
    uploader: BlueskyUploader, pds: FakePds
) -> None:
    result = await uploader.post_text("Hello from xPST")

    assert result.success is True
    assert result.outcome.value == "published"
    assert result.post_id == f"at://{DID}/app.bsky.feed.post/3krecord1"
    assert result.post_url == f"https://bsky.app/profile/{IDENTIFIER}/post/3krecord1"

    assert len(pds.records) == 1
    record = pds.records[0]
    assert record["repo"] == DID
    assert record["collection"] == "app.bsky.feed.post"
    assert record["record"]["$type"] == "app.bsky.feed.post"
    assert record["record"]["text"] == "Hello from xPST"
    assert record["record"]["createdAt"].endswith("Z")
    assert "embed" not in record["record"]


@pytest.mark.asyncio
async def test_post_text_refuses_an_over_limit_post_instead_of_truncating(
    uploader: BlueskyUploader, pds: FakePds
) -> None:
    text = "x" * 301
    result = await uploader.post_text(text)

    assert result.success is False
    assert "BLUESKY_TEXT_TOO_LONG" in (result.error or "")
    assert "300" in (result.error or "")
    assert pds.records == [], "an over-limit post must never reach the network"


@pytest.mark.asyncio
async def test_post_text_refuses_an_empty_post(uploader: BlueskyUploader) -> None:
    result = await uploader.post_text("   ")
    assert result.success is False
    assert "BLUESKY_TEXT_EMPTY" in (result.error or "")


@pytest.mark.asyncio
async def test_links_are_faceted_with_utf8_byte_offsets(
    uploader: BlueskyUploader, pds: FakePds
) -> None:
    """Facet indices are byte offsets, not character offsets.

    The prefix carries a multi-byte character so a character-offset bug would
    produce a different number and the slice assertion would fail.
    """
    text = "héllo https://example.com/path done"
    await uploader.post_text(text)

    facets = pds.records[0]["record"]["facets"]
    assert len(facets) == 1
    index = facets[0]["index"]
    encoded = text.encode("utf-8")
    assert encoded[index["byteStart"] : index["byteEnd"]].decode("utf-8") == "https://example.com/path"
    assert facets[0]["features"][0]["uri"] == "https://example.com/path"
    # Proves the distinction: the character offset differs from the byte offset.
    assert index["byteStart"] != text.index("https://")


@pytest.mark.asyncio
async def test_a_trailing_full_stop_is_not_part_of_the_link(
    uploader: BlueskyUploader, pds: FakePds
) -> None:
    """Sentence punctuation glued to a URL must not end up in the facet URI."""
    text = "read this: https://example.com/page."
    await uploader.post_text(text)

    facet = pds.records[0]["record"]["facets"][0]
    assert facet["features"][0]["uri"] == "https://example.com/page"
    encoded = text.encode("utf-8")
    index = facet["index"]
    assert encoded[index["byteStart"] : index["byteEnd"]].decode("utf-8") == "https://example.com/page"


# ── session handling ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_is_created_once_and_reused(uploader: BlueskyUploader, pds: FakePds) -> None:
    await uploader.post_text("first")
    await uploader.post_text("second")
    assert pds.create_session_count == 1, "one login per adapter instance, not one per post"


@pytest.mark.asyncio
async def test_stored_session_is_reused_across_instances(
    config: XPSTConfig, pds: FakePds
) -> None:
    first = BlueskyUploader(config)
    first.transport = httpx.MockTransport(pds.handler)
    await first.post_text("first")

    second = BlueskyUploader(config)
    second.transport = httpx.MockTransport(pds.handler)
    await second.post_text("second")

    assert pds.create_session_count == 1, "the session persists in the encrypted store"


@pytest.mark.asyncio
async def test_expired_access_token_is_refreshed_once(
    uploader: BlueskyUploader, pds: FakePds
) -> None:
    pds.expire_access_token_once = True
    result = await uploader.post_text("after refresh")

    assert result.success is True
    assert ("POST", "com.atproto.server.refreshSession") in pds.calls
    assert [path for _, path in pds.calls].count("com.atproto.repo.createRecord") == 2


@pytest.mark.asyncio
async def test_a_rejected_app_password_is_an_explicit_auth_failure(
    config: XPSTConfig, pds: FakePds
) -> None:
    CredentialStore(config.config_dir).store(CRED_APP_PASSWORD, "wrong-password")
    uploader = BlueskyUploader(config)
    uploader.transport = httpx.MockTransport(pds.handler)

    result = await uploader.post_text("hello")
    assert result.success is False
    assert "BLUESKY_AUTH_FAILED" in (result.error or "")
    assert "xpst auth bluesky" in (result.error or "")


@pytest.mark.asyncio
async def test_missing_credentials_are_named_not_guessed(tmp_path: Path, pds: FakePds) -> None:
    from xpst.config import BlueskyAccountConfig

    cfg = XPSTConfig()
    cfg.config_dir = str(tmp_path)
    cfg.bluesky = BlueskyAccountConfig(enabled=True, identifier="", service=SERVICE)
    uploader = BlueskyUploader(cfg)
    uploader.transport = httpx.MockTransport(pds.handler)

    result = await uploader.post_text("hello")
    assert result.success is False
    assert "BLUESKY_NOT_CONFIGURED" in (result.error or "")


# ── images ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_image_sends_a_blob_then_an_embed(
    uploader: BlueskyUploader, pds: FakePds, image: Path
) -> None:
    result = await uploader.upload_image(image, "an image")

    assert result.success is True
    assert len(pds.blobs) == 1
    assert pds.blobs[0]["content_type"] == "image/png"
    record = pds.records[0]["record"]
    assert record["text"] == "an image"
    embed = record["embed"]
    assert embed["$type"] == "app.bsky.embed.images"
    assert len(embed["images"]) == 1
    item = embed["images"][0]
    assert item["alt"] == ""
    assert item["image"]["ref"]["$link"]
    assert item["aspectRatio"] == {"width": 1, "height": 1}


@pytest.mark.asyncio
async def test_an_oversized_image_is_refused_before_any_network_call(
    uploader: BlueskyUploader, pds: FakePds, tmp_path: Path
) -> None:
    path = tmp_path / "huge.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (BLUESKY_MAX_IMAGE_BYTES + 1))

    result = await uploader.upload_image(path, "too big")

    assert result.success is False
    assert "BLUESKY_IMAGE_REJECTED" in (result.error or "")
    assert "2,000,000 bytes" in (result.error or "")
    assert pds.calls == [], "a rejected image must not be uploaded"


@pytest.mark.asyncio
async def test_an_unsupported_image_type_is_refused(uploader: BlueskyUploader, tmp_path: Path) -> None:
    path = tmp_path / "diagram.bmp"
    path.write_bytes(b"BM" + b"\x00" * 64)
    result = await uploader.upload_image(path, "bmp")
    assert result.success is False
    assert "BLUESKY_IMAGE_REJECTED" in (result.error or "")


@pytest.mark.asyncio
async def test_carousel_publishes_every_image_in_order(
    uploader: BlueskyUploader, pds: FakePds, tmp_path: Path
) -> None:
    paths = []
    for index in range(3):
        path = tmp_path / f"slide{index}.png"
        path.write_bytes(ONE_PIXEL_PNG)
        paths.append(path)

    result = await uploader.upload_carousel(paths, "three slides")

    assert result.success is True
    assert len(pds.blobs) == 3
    images = pds.records[0]["record"]["embed"]["images"]
    assert len(images) == 3
    assert [item["image"]["ref"]["$link"] for item in images] == [
        "bafyblob1",
        "bafyblob2",
        "bafyblob3",
    ]


@pytest.mark.asyncio
async def test_carousel_refuses_more_images_than_the_lexicon_allows(
    uploader: BlueskyUploader, pds: FakePds, tmp_path: Path
) -> None:
    paths = []
    for index in range(BLUESKY_MAX_IMAGES_PER_POST + 1):
        path = tmp_path / f"extra{index}.png"
        path.write_bytes(ONE_PIXEL_PNG)
        paths.append(path)

    result = await uploader.upload_carousel(paths, "too many")

    assert result.success is False
    assert "BLUESKY_CAROUSEL_TOO_MANY" in (result.error or "")
    assert "4" in (result.error or "")
    assert pds.calls == [], "nothing is uploaded and nothing is dropped silently"


@pytest.mark.asyncio
async def test_carousel_with_one_image_is_refused(
    uploader: BlueskyUploader, image: Path
) -> None:
    result = await uploader.upload_carousel([image], "one")
    assert result.success is False
    assert "BLUESKY_CAROUSEL_TOO_FEW" in (result.error or "")


# ── rate limits, health, delete, readback ──────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_carries_the_provider_window(
    uploader: BlueskyUploader, pds: FakePds
) -> None:
    """A 429 must hand the cooldown machine the provider's own Retry-After."""
    pds.rate_limited = True
    result = await uploader.post_text("throttled")

    assert result.success is False
    assert "BLUESKY_RATE_LIMITED" in (result.error or "")
    assert "Retry-After: 900" in (result.error or "")
    assert result.retryable is True
    assert result.metadata["retry_after_s"] == 900


@pytest.mark.asyncio
async def test_check_health_reports_the_handle(uploader: BlueskyUploader) -> None:
    health = await uploader.check_health()
    assert health.authenticated is True
    assert health.session_valid is True
    assert health.details["handle"] == IDENTIFIER


@pytest.mark.asyncio
async def test_check_health_reports_missing_credentials(tmp_path: Path) -> None:
    from xpst.config import BlueskyAccountConfig

    cfg = XPSTConfig()
    cfg.config_dir = str(tmp_path)
    cfg.bluesky = BlueskyAccountConfig(enabled=True, identifier="", service=SERVICE)
    health = await BlueskyUploader(cfg).check_health()
    assert health.authenticated is False
    assert "handle" in (health.error or "").lower()


@pytest.mark.asyncio
async def test_delete_removes_the_record_and_readback_confirms_it(
    uploader: BlueskyUploader, pds: FakePds
) -> None:
    posted = await uploader.post_text("to be deleted")
    assert posted.post_id is not None

    before = await uploader.readback(posted.post_id)
    assert before.state.value == "verified"

    deleted = await uploader.delete(posted.post_id)
    assert deleted.outcome.value == "deleted"

    after = await uploader.readback(posted.post_id)
    assert after.state.value == "not_found"


@pytest.mark.asyncio
async def test_readback_rejects_a_non_at_uri(uploader: BlueskyUploader) -> None:
    result = await uploader.readback("not-a-uri")
    assert result.state.value == "unknown"


# ── credential storage ─────────────────────────────────────────────────────


def test_the_app_password_never_reaches_config_yaml(config: XPSTConfig, tmp_path: Path) -> None:
    """A credential in a plaintext config file is the defect this prevents."""
    config.save(str(tmp_path / "config.yaml"))
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))

    bluesky_section = saved["accounts"]["bluesky"]
    assert bluesky_section["enabled"] is True
    assert bluesky_section["identifier"] == IDENTIFIER
    assert APP_PASSWORD not in json.dumps(saved), "the app password must not be serialized"
    assert set(bluesky_section) == {"enabled", "identifier", "service", "proxy"}


def test_stored_session_is_reused_from_the_encrypted_store(config: XPSTConfig) -> None:
    store = CredentialStore(config.config_dir)
    store.store_json(CRED_SESSION, {"did": DID, "handle": IDENTIFIER, "accessJwt": "cached"})
    uploader = BlueskyUploader(config)
    assert uploader._stored_session()["accessJwt"] == "cached"


# ── connect flow ───────────────────────────────────────────────────────────

def test_connect_normalises_a_scheme_less_service_like_publishing(monkeypatch) -> None:
    """A bare host must work at connect time, not only at publish time.

    ``connect_bluesky`` verifies the credential against a configured service
    host; the uploader always normalised a scheme-less value (``pds.example``
    -> ``https://pds.example``) while the connect-time call did not, so the
    same config failed in one path and worked in the other.
    """

    import xpst.connect as connect_mod
    from xpst.platforms.bluesky import _service_base

    seen: dict[str, Any] = {}

    class _Response:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {"accessJwt": "access", "refreshJwt": "refresh", "handle": IDENTIFIER, "did": DID}

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def post(self, url: str, json: Any = None) -> _Response:
            seen["url"] = url
            seen["json"] = json
            return _Response()

    monkeypatch.setattr(connect_mod.httpx, "AsyncClient", _Client)

    session = connect_mod._verify_bluesky_app_password(
        IDENTIFIER, APP_PASSWORD, "pds.example"
    )

    assert session is not None and session["accessJwt"] == "access"
    assert seen["url"] == f"{_service_base('pds.example')}/xrpc/com.atproto.server.createSession"
    assert seen["url"] == "https://pds.example/xrpc/com.atproto.server.createSession"
    assert seen["json"] == {"identifier": IDENTIFIER, "password": APP_PASSWORD}
