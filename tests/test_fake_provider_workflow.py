"""Fake-provider workflow tests for release-grade integration coverage."""

from pathlib import Path

import pytest

from xpst.config import XPSTConfig
from xpst.platforms.base import PlatformHealth, PlatformUploader, UploadResult
from xpst.sources.base import DownloadResult, VideoMetadata, VideoSource
from xpst.usecases.base import UseCaseDeps
from xpst.usecases.health_check import HealthCheckUseCase
from xpst.usecases.manual_post import ManualPostUseCase


class FakeDestination(PlatformUploader):
    def __init__(self, config: XPSTConfig, *, should_fail: bool = False):
        super().__init__(config)
        self.should_fail = should_fail
        self.uploads: list[tuple[str, str]] = []

    async def upload(self, video_path: Path | str, caption: str) -> UploadResult:
        self.uploads.append((str(video_path), caption))
        if self.should_fail:
            return UploadResult(success=False, platform=self.platform_name, error="simulated failure")
        return UploadResult(success=True, platform=self.platform_name, post_id="fake-post")

    async def check_health(self) -> PlatformHealth:
        return PlatformHealth(platform=self.platform_name, authenticated=not self.should_fail)


class FakeSource(VideoSource):
    @property
    def source_name(self) -> str:
        return "fake_source"

    async def list_videos(self, max_count: int = 10) -> list[VideoMetadata]:
        return [VideoMetadata(video_id="fake-video", url="https://example.test/video")]

    async def download(self, video_id: str, output_dir: Path) -> DownloadResult:
        return DownloadResult(success=True, video_path=output_dir / f"{video_id}.mp4")

    async def check_health(self) -> dict[str, str]:
        return {"status": "ok"}


class FakeState:
    def get_statistics(self) -> dict[str, int]:
        return {"total_videos_tracked": 0}


class FakeQuotaManager:
    def get_all_status(self) -> dict[str, dict[str, int]]:
        return {"fake": {"remaining": 10}}


def make_deps(*, failing_destination: bool = False) -> UseCaseDeps:
    config = XPSTConfig()
    return UseCaseDeps(
        config=config,
        state=FakeState(),
        quota_manager=FakeQuotaManager(),
        circuit_breakers={},
        platforms={
            "ok": FakeDestination(config),
            "failing": FakeDestination(config, should_fail=failing_destination),
        },
        sources={"fake": FakeSource(config)},
    )


@pytest.mark.asyncio
async def test_manual_post_with_fake_providers_records_partial_success(tmp_path):
    deps = make_deps(failing_destination=True)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake video")

    result = await ManualPostUseCase(deps).execute(
        str(video),
        "caption",
        platforms=["ok", "failing"],
    )

    assert result.partial_success is True
    assert result.all_success is False
    assert result.results["ok"].success is True
    assert result.results["failing"].success is False


@pytest.mark.asyncio
async def test_health_check_uses_provider_check_health_contract():
    deps = make_deps()

    result = await HealthCheckUseCase(deps).execute()

    assert result.sources["fake"]["status"] == "ok"
    assert result.platforms["ok"].authenticated is True
    assert result.state["total_videos_tracked"] == 0
    assert result.quotas["fake"]["remaining"] == 10

