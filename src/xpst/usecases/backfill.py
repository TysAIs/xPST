"""Use-case for backfill operations."""

from xpst.usecases.base import BackfillResult, BaseUseCase


class BackfillUseCase(BaseUseCase):
    """Fetches historical content and cross-posts it."""

    async def execute(
        self,
        source_name: str = "tiktok",
        max_count: int = 10,
        platforms: list[str] | None = None
    ) -> BackfillResult:
        """Fetch and post older content from source.

        Args:
            source_name: Source to fetch from
            max_count: Maximum videos to attempt
            platforms: Target platforms (None = all configured)

        Returns:
            BackfillResult with statistics
        """
        if platforms is None:
            platforms = list(self.deps.platforms.keys())
        target_platforms = [p for p in platforms if p in self.deps.platforms]

        # Fetch many videos (backfill needs more candidates)
        videos = await self.deps.source_service.fetch_new_videos(source_name, max_count * 3)

        if not videos:
            return BackfillResult(attempted=0, successful=0, results=[])

        # Filter to only truly new (not in state)
        new_videos = self.deps.source_service.filter_new(
            videos, self.deps.state, self.deps.platforms
        )

        # Limit to max_count
        videos_to_post = new_videos[:max_count]

        results = []
        successful = 0

        cross_post_uc = self.deps.usecase_factory.create_cross_post()

        for video in videos_to_post:
            result = await cross_post_uc.execute(
                video_id=video.video_id,
                caption=video.caption or "",
                platforms=target_platforms
            )
            results.append(result)
            if result.all_success:
                successful += 1

            # Respect rate limits - small delay between posts
            import asyncio
            await asyncio.sleep(1)

        return BackfillResult(
            attempted=len(videos_to_post),
            successful=successful,
            results=results
        )
