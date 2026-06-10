"""Use-case for cross-posting videos to platforms."""

from xpst.platforms.base import UploadResult
from xpst.usecases.base import BaseUseCase, CrossPostResult


class CrossPostVideoUseCase(BaseUseCase):
    """Cross-posts a video to multiple platforms."""

    async def execute(
        self,
        video_id: str,
        caption: str,
        platforms: list[str] | None = None
    ) -> CrossPostResult:
        """Cross-post a video to specified platforms.

        Args:
            video_id: ID of the video to post
            caption: Caption text (will be adapted per platform)
            platforms: List of platform names. If None, posts to all configured platforms.

        Returns:
            CrossPostResult with per-platform results
        """
        # Determine target platforms
        if platforms is None:
            platforms = list(self.deps.platforms.keys())

        # Filter to only configured platforms
        target_platforms = [p for p in platforms if p in self.deps.platforms]

        results = {}
        all_success = True
        any_success = False

        for platform_name in target_platforms:
            uploader = self.deps.platforms[platform_name]
            try:
                result: UploadResult = await uploader.upload(
                    video_id=video_id,
                    caption=caption
                )
                results[platform_name] = result
                if result.success:
                    any_success = True
                else:
                    all_success = False
            except Exception as e:
                results[platform_name] = UploadResult(
                    success=False,
                    platform=platform_name,
                    error=str(e)
                )
                all_success = False

        return CrossPostResult(
            video_id=video_id,
            caption=caption,
            results=results,
            all_success=all_success and any_success,
            partial_success=any_success and not all_success
        )
