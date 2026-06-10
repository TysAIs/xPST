"""Use-case layer for xPST.

Provides high-level business logic operations with dependency injection.
This layer sits between the CLI/engine and the platform/source implementations.
"""

from xpst.usecases.backfill import BackfillUseCase
from xpst.usecases.base import (
    BackfillResult,
    BaseUseCase,
    CrossPostResult,
    DeletePostResult,
    FetchVideosResult,
    HealthCheckResult,
    ManualPostResult,
    UseCaseDependencies,
    UseCaseDeps,
    UseCaseResult,
)
from xpst.usecases.cross_post import CrossPostVideoUseCase
from xpst.usecases.delete_post import DeletePostUseCase
from xpst.usecases.factory import UseCaseFactory
from xpst.usecases.fetch_videos import FetchNewVideosUseCase
from xpst.usecases.health_check import HealthCheckUseCase
from xpst.usecases.manual_post import ManualPostUseCase

__all__ = [
    "BaseUseCase",
    "UseCaseDependencies",
    "UseCaseDeps",
    "UseCaseResult",
    "FetchVideosResult",
    "CrossPostResult",
    "ManualPostResult",
    "BackfillResult",
    "HealthCheckResult",
    "DeletePostResult",
    "UseCaseFactory",
    "FetchNewVideosUseCase",
    "CrossPostVideoUseCase",
    "ManualPostUseCase",
    "BackfillUseCase",
    "HealthCheckUseCase",
    "DeletePostUseCase",
]
