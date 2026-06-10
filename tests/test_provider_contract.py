"""Tests for platform-agnostic provider metadata."""

from pathlib import Path
from typing import Any

from xpst.config import XPSTConfig
from xpst.platforms.base import PlatformHealth, PlatformRegistry, PlatformUploader, UploadResult
from xpst.providers import AuthMode, ProviderCapability, ProviderManifest, ProviderRole
from xpst.sources.base import DownloadResult, SourceRegistry, VideoMetadata, VideoSource


class DummyUploader(PlatformUploader):
    @property
    def manifest(self) -> ProviderManifest:
        base = super().manifest
        return ProviderManifest(
            name=base.name,
            display_name="Dummy Destination",
            roles=base.roles,
            capabilities=base.capabilities + (ProviderCapability.DELETE,),
            auth_mode=AuthMode.NONE,
        )

    async def upload(self, video_path: Path, caption: str) -> UploadResult:
        return UploadResult(success=True, platform=self.platform_name)

    async def check_health(self) -> PlatformHealth:
        return PlatformHealth(platform=self.platform_name, authenticated=True)


class DummySource(VideoSource):
    @property
    def source_name(self) -> str:
        return "dummy"

    async def list_videos(self, max_count: int = 10) -> list[VideoMetadata]:
        return []

    async def download(self, video_id: str, output_dir: Path) -> DownloadResult:
        return DownloadResult(success=False, error="not implemented")

    async def check_health(self) -> dict[str, Any]:
        return {"status": "ok"}


def test_provider_manifest_serializes_to_json_shape():
    manifest = ProviderManifest(
        name="demo",
        display_name="Demo",
        roles=(ProviderRole.SOURCE, ProviderRole.DESTINATION),
        capabilities=(ProviderCapability.LIST, ProviderCapability.UPLOAD),
        auth_mode=AuthMode.OAUTH,
        is_official_api=True,
        docs_url="https://example.test/docs",
    )

    data = manifest.to_dict()

    assert data["name"] == "demo"
    assert data["roles"] == ["source", "destination"]
    assert data["capabilities"] == ["list", "upload"]
    assert data["auth_mode"] == "oauth"
    assert data["is_official_api"] is True


def test_platform_registry_returns_provider_manifests():
    original = PlatformRegistry._registry.copy()
    try:
        PlatformRegistry._registry.clear()
        PlatformRegistry.register("dummy", DummyUploader)

        manifests = PlatformRegistry.list_manifests(XPSTConfig())

        assert len(manifests) == 1
        assert manifests[0].name == "dummy"
        assert ProviderRole.DESTINATION in manifests[0].roles
        assert ProviderCapability.UPLOAD in manifests[0].capabilities
        assert ProviderCapability.DELETE in manifests[0].capabilities
    finally:
        PlatformRegistry._registry = original


def test_source_registry_returns_provider_manifests():
    original = SourceRegistry._registry.copy()
    try:
        SourceRegistry._registry.clear()
        SourceRegistry.register("dummy", DummySource)

        manifests = SourceRegistry.list_manifests(XPSTConfig())

        assert len(manifests) == 1
        assert manifests[0].name == "dummy"
        assert ProviderRole.SOURCE in manifests[0].roles
        assert ProviderCapability.LIST in manifests[0].capabilities
        assert ProviderCapability.DOWNLOAD in manifests[0].capabilities
    finally:
        SourceRegistry._registry = original


def test_concrete_platform_manifests_expose_real_capabilities():
    original = PlatformRegistry._registry.copy()
    try:
        PlatformRegistry._registry.clear()
        PlatformRegistry.auto_discover()

        manifests = {
            manifest.name: manifest
            for manifest in PlatformRegistry.list_manifests(XPSTConfig())
        }

        assert set(manifests) >= {"youtube", "instagram", "x"}
        assert manifests["youtube"].auth_mode == AuthMode.OAUTH
        assert manifests["youtube"].is_official_api is True
        assert ProviderCapability.DELETE in manifests["youtube"].capabilities
        assert ProviderCapability.CAROUSEL in manifests["instagram"].capabilities
        assert manifests["instagram"].auth_mode == AuthMode.SESSION
        assert ProviderCapability.CAROUSEL in manifests["x"].capabilities
        assert manifests["x"].auth_mode == AuthMode.COOKIES
    finally:
        PlatformRegistry._registry = original


def test_concrete_source_manifests_expose_real_capabilities():
    original = SourceRegistry._registry.copy()
    try:
        SourceRegistry._registry.clear()
        SourceRegistry.auto_discover()

        manifests = {
            manifest.name: manifest
            for manifest in SourceRegistry.list_manifests(XPSTConfig())
        }

        assert set(manifests) >= {"tiktok", "youtube", "instagram", "x", "local"}
        assert manifests["local"].auth_mode == AuthMode.LOCAL
        assert ProviderCapability.LOCAL_ONLY in manifests["local"].capabilities
        assert ProviderCapability.CAROUSEL in manifests["tiktok"].capabilities
        assert ProviderCapability.COOKIE_AUTH in manifests["youtube"].capabilities
        assert manifests["instagram"].auth_mode == AuthMode.SESSION
        assert manifests["x"].auth_mode == AuthMode.COOKIES
    finally:
        SourceRegistry._registry = original
