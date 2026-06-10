"""Base classes for use-cases in xPST.

Provides the foundation for the use-case layer with dependency injection support.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class UseCaseDependencies(Protocol):
    """Protocol for use-case dependencies - enables DI and testing.

    Any object implementing this protocol can be passed to use-cases.
    The CrossPostEngine implements this protocol.
    """

    config: Any
    state: Any
    video_processor: Any
    circuit_breakers: Any
    quota_manager: Any
    notifier: Any
    shutdown_handler: Any
    session_manager: Any
    upload_service: Any
    source_service: Any
    crash_recovery: Any
    anti_bot: Any
    platforms: dict[str, Any]
    sources: dict[str, Any]


@dataclass
class UseCaseDeps:
    """Concrete implementation of UseCaseDependencies for runtime use."""
    config: Any
    state: Any
    video_processor: Any = None
    circuit_breakers: Any = None
    quota_manager: Any = None
    notifier: Any = None
    shutdown_handler: Any = None
    session_manager: Any = None
    upload_service: Any = None
    source_service: Any = None
    crash_recovery: Any = None
    anti_bot: Any = None
    platforms: dict[str, Any] = None
    sources: dict[str, Any] = None


@dataclass
class UseCaseResult:
    """Generic result container for use-case executions."""
    success: bool
    data: Any = None
    error: str | None = None


class BaseUseCase(ABC):
    """Base class for all use-cases.

    Use-cases should be stateless and receive all dependencies via constructor.
    This allows for easy testing and composition.
    """

    def __init__(self, deps: UseCaseDependencies):
        self.deps = deps

    @abstractmethod
    async def execute(self, *args, **kwargs) -> UseCaseResult:
        """Execute the use-case."""
        pass


@dataclass
class FetchVideosResult:
    """Result of fetching new videos."""
    videos: list[Any] = None
    catch_up: bool = False
    fetch_count: int = 0


@dataclass
class CrossPostResult:
    """Result of cross-posting a single video."""
    video_id: str
    caption: str
    results: dict[str, Any] = None
    all_success: bool = False
    partial_success: bool = False


@dataclass
class ManualPostResult:
    """Result of manual posting."""
    video_id: str
    caption: str
    results: dict[str, Any] = None
    all_success: bool = False
    partial_success: bool = False


@dataclass
class BackfillResult:
    """Result of backfill operation."""
    attempted: int
    successful: int
    results: list[Any] = None


@dataclass
class HealthCheckResult:
    """Result of health check."""
    sources: dict[str, Any]
    platforms: dict[str, Any]
    circuit_breakers: dict[str, Any]
    state: dict[str, Any]
    quotas: dict[str, Any]


@dataclass
class DeletePostResult:
    """Result of delete post operation."""
    video_id: str
    platform: str
    success: bool
    error: str | None = None
