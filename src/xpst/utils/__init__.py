"""Shared utilities for xPST.

Exports are loaded lazily so importing a small utility module, such as
``xpst.utils.platform``, does not pull in video/config dependencies.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "CircuitBreaker",
    "CircuitBreakerManager",
    "CircuitBreakerOpenError",
    "CredentialStore",
    "get_logger",
    "setup_logging",
    "QuotaManager",
    "RetryConfig",
    "retry_async",
    "retry_sync",
    "SessionManager",
    "VideoProcessor",
    "metrics",
]


def __getattr__(name: str) -> Any:
    if name in {"CircuitBreaker", "CircuitBreakerManager", "CircuitBreakerOpenError"}:
        from .circuit_breaker import CircuitBreaker, CircuitBreakerManager, CircuitBreakerOpenError
        return {
            "CircuitBreaker": CircuitBreaker,
            "CircuitBreakerManager": CircuitBreakerManager,
            "CircuitBreakerOpenError": CircuitBreakerOpenError,
        }[name]
    if name == "CredentialStore":
        from .credentials import CredentialStore
        return CredentialStore
    if name in {"get_logger", "setup_logging"}:
        from .logger import get_logger, setup_logging
        return {"get_logger": get_logger, "setup_logging": setup_logging}[name]
    if name == "QuotaManager":
        from .quota import QuotaManager
        return QuotaManager
    if name in {"RetryConfig", "retry_async", "retry_sync"}:
        from .retry import RetryConfig, retry_async, retry_sync
        return {
            "RetryConfig": RetryConfig,
            "retry_async": retry_async,
            "retry_sync": retry_sync,
        }[name]
    if name == "SessionManager":
        from .sessions import SessionManager
        return SessionManager
    if name == "VideoProcessor":
        from .video import VideoProcessor
        return VideoProcessor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
