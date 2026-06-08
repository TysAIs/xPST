"""
Prometheus metrics for XPST.

Optional — only active if prometheus_client is installed.
Tracks uploads, encoding duration, active platforms, circuit breaker state.

Usage:
    from xpst.utils.metrics import metrics

    metrics.record_upload("youtube", "success", duration=2.5)
    metrics.set_active_platforms(3)
    metrics.set_circuit_breaker_state("instagram", is_open=True)

    # In dashboard server:
    from xpst.utils.metrics import metrics_text
    text = metrics_text()  # Returns Prometheus exposition format
"""

import time

try:
    from prometheus_client import (
        REGISTRY,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    _HAS_PROMETHEUS = True
except ImportError:
    _HAS_PROMETHEUS = False

__all__ = ["metrics", "metrics_text", "MetricsTracker"]


class _NullMetrics:
    """No-op metrics when prometheus_client is not installed."""

    def record_upload(self, platform: str, status: str, duration: float = 0.0) -> None:
        pass

    def record_encoding(self, platform: str, duration: float = 0.0) -> None:
        pass

    def set_active_platforms(self, count: int) -> None:
        pass

    def set_circuit_breaker_state(self, platform: str, is_open: bool) -> None:
        pass

    def start_timer(self) -> float:
        return 0.0

    def observe_duration(self, start: float) -> float:
        return 0.0


if _HAS_PROMETHEUS:

    class MetricsTracker:
        """Prometheus metrics tracker for XPST."""

        def __init__(self) -> None:
            self._uploads_total = Counter(
                "xpst_uploads_total",
                "Total number of uploads",
                ["platform", "status"],
            )
            self._upload_duration = Histogram(
                "xpst_upload_duration_seconds",
                "Upload duration in seconds",
                ["platform"],
                buckets=(1, 5, 10, 30, 60, 120, 300, 600),
            )
            self._encoding_duration = Histogram(
                "xpst_encoding_duration_seconds",
                "Encoding duration in seconds",
                ["platform"],
                buckets=(1, 5, 10, 30, 60, 120, 300),
            )
            self._active_platforms = Gauge(
                "xpst_active_platforms",
                "Number of active platforms",
            )
            self._circuit_breaker_state = Gauge(
                "xpst_circuit_breaker_state",
                "Circuit breaker state (1=open, 0=closed)",
                ["platform"],
            )

        def record_upload(self, platform: str, status: str, duration: float = 0.0) -> None:
            """Record an upload attempt."""
            self._uploads_total.labels(platform=platform, status=status).inc()
            if duration > 0:
                self._upload_duration.labels(platform=platform).observe(duration)

        def record_encoding(self, platform: str, duration: float = 0.0) -> None:
            """Record encoding duration."""
            self._encoding_duration.labels(platform=platform).observe(duration)

        def set_active_platforms(self, count: int) -> None:
            """Set the number of active platforms."""
            self._active_platforms.set(count)

        def set_circuit_breaker_state(self, platform: str, is_open: bool) -> None:
            """Set circuit breaker state (1=open, 0=closed)."""
            self._circuit_breaker_state.labels(platform=platform).set(1 if is_open else 0)

        def start_timer(self) -> float:
            """Start a timer, returns the start time."""
            return time.monotonic()

        def observe_duration(self, start: float) -> float:
            """Calculate duration since start."""
            return time.monotonic() - start

    metrics = MetricsTracker()

else:
    metrics = _NullMetrics()


def metrics_text() -> str:
    """Return Prometheus exposition format text.

    Returns empty string if prometheus_client is not installed.
    """
    if _HAS_PROMETHEUS:
        return generate_latest(REGISTRY).decode("utf-8")
    return ""
