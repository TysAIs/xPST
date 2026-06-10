"""Use-case for fetching new videos from sources."""

from xpst.sources.base import VideoMetadata
from xpst.usecases.base import BaseUseCase, FetchVideosResult, UseCaseDependencies


class FetchNewVideosUseCase(BaseUseCase):
    """Fetches new videos from sources and filters to unposted ones."""

    async def execute(
        self,
        source_name: str = "tiktok",
        max_count: int = 5,
        catch_up: bool = False
    ) -> FetchVideosResult:
        """Fetch new videos from the specified source.

        Args:
            source_name: Name of the source to fetch from (default: tiktok)
            max_count: Maximum number of videos to fetch
            catch_up: If True, fetch more videos to compensate for downtime

        Returns:
            FetchVideosResult with list of new VideoMetadata objects
        """
        actual_max = 20 if catch_up else max_count

        # Fetch videos from source
        videos = await self.deps.source_service.fetch_new_videos(
            source_name, actual_max
        )

        if not videos:
            return FetchVideosResult(videos=[], catch_up=catch_up, fetch_count=0)

        # Filter to only new videos
        new_videos = self.deps.source_service.filter_new(
            videos, self.deps.state, self.deps.platforms
        )

        return FetchVideosResult(
            videos=new_videos,
            catch_up=catch_up,
            fetch_count=len(new_videos)
        )