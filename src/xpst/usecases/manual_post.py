"""Use-case for manual posting."""

from xpst.platforms.base import UploadResult
from xpst.usecases.base import BaseUseCase, ManualPostResult, UseCaseDependencies


class ManualPostUseCase(BaseUseCase):
    """Posts a local video file to specified platforms."""

    async def execute(
        self,
        video_path: str,
        caption: str,
        platforms: list[str] | None = None
    ) -> ManualPostResult:
        """Post a local video file to platforms.

        Args:
            video_path: Path to local video file
            caption: Caption text
            platforms: List of platform names. If None, posts to all configured.

        Returns:
            ManualPostResult with per-platform results
        """
        if platforms is None:
            platforms = list(self.deps.platforms.keys())
        
        target_platforms = [p for p in platforms if p in self.deps.platforms]
        
        results = {}
        all_success = True
        any_success = False
        
        for platform_name in target_platforms:
            uploader = self.deps.platforms[platform_name]
            try:
                result: UploadResult = await uploader.upload(
                    video_path=video_path,
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
        
        # Generate a video_id for tracking (hash of path + timestamp)
        import hashlib
        import time
        video_id = hashlib.md5(f"{video_path}{time.time()}".encode()).hexdigest()[:12]
        
        return ManualPostResult(
            video_id=video_id,
            caption=caption,
            results=results,
            all_success=all_success and any_success,
            partial_success=any_success and not all_success
        )