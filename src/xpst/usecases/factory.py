"""Factory for creating use-cases with injected dependencies."""

from xpst.usecases.base import UseCaseDependencies, UseCaseResult
from xpst.usecases.fetch_videos import FetchNewVideosUseCase
from xpst.usecases.cross_post import CrossPostVideoUseCase
from xpst.usecases.manual_post import ManualPostUseCase
from xpst.usecases.backfill import BackfillUseCase
from xpst.usecases.health_check import HealthCheckUseCase
from xpst.usecases.delete_post import DeletePostUseCase


class UseCaseFactory:
    """Factory for creating use-case instances with shared dependencies."""

    def __init__(self, deps: UseCaseDependencies):
        self._deps = deps

    def create_fetch_videos(self) -> FetchNewVideosUseCase:
        """Create fetch new videos use-case."""
        return FetchNewVideosUseCase(self._deps)

    def create_cross_post(self) -> CrossPostVideoUseCase:
        """Create cross-post video use-case."""
        return CrossPostVideoUseCase(self._deps)

    def create_manual_post(self) -> ManualPostUseCase:
        """Create manual post use-case."""
        return ManualPostUseCase(self._deps)

    def create_backfill(self) -> BackfillUseCase:
        """Create backfill use-case."""
        return BackfillUseCase(self._deps)

    def create_health_check(self) -> HealthCheckUseCase:
        """Create health check use-case."""
        return HealthCheckUseCase(self._deps)

    def create_delete_post(self) -> DeletePostUseCase:
        """Create delete post use-case."""
        return DeletePostUseCase(self._deps)