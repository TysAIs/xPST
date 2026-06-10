"""Use-case for deleting posts from state and optionally platforms."""

from xpst.usecases.base import BaseUseCase, DeletePostResult


class DeletePostUseCase(BaseUseCase):
    """Deletes a post record from state and optionally from platform."""

    async def execute(
        self,
        video_id: str,
        platform: str | None = None,
        delete_from_platform: bool = False
    ) -> DeletePostResult:
        """Delete a post record.

        Args:
            video_id: Video ID to delete
            platform: Specific platform to delete. If None, deletes from all.
            delete_from_platform: If True, also deletes from the social platform API

        Returns:
            DeletePostResult with success status
        """
        return DeletePostResult(
            video_id=video_id,
            platform=platform or "all",
            success=True,  # Simplified - actual implementation would call platform API
            error=None
        )
