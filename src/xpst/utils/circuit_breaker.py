"""
Circuit breaker pattern for xPST

Implements the circuit breaker pattern to prevent cascading failures
when a platform becomes unavailable.

States:
- CLOSED: Normal operation, requests pass through
- OPEN: Too many failures, requests are blocked
- HALF_OPEN: Testing if service has recovered

Example usage:
    from xpst.utils.circuit_breaker import CircuitBreaker

    breaker = CircuitBreaker("youtube", failure_threshold=5, reset_timeout=3600)

    if breaker.allow_request():
        try:
            result = await upload_video()
            breaker.record_success()
        except Exception as e:
            breaker.record_failure(str(e))
    else:
        raise CircuitBreakerOpenError("YouTube is temporarily disabled")
"""

import time
from dataclasses import dataclass, field
from enum import Enum


class CircuitState(Enum):
    """Circuit breaker states"""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Blocking requests
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitBreaker:
    """
    Circuit breaker implementation.

    Attributes:
        name: Name of the protected service
        failure_threshold: Number of failures before opening
        reset_timeout: Seconds before attempting recovery
        half_open_max: Max requests allowed in half-open state
    """
    name: str
    failure_threshold: int = 5
    reset_timeout: int = 3600  # 1 hour
    half_open_max: int = 3

    # Internal state
    _state: CircuitState = field(default=CircuitState.CLOSED, repr=False)
    _failure_count: int = field(default=0, repr=False)
    _success_count: int = field(default=0, repr=False)
    _last_failure_time: float | None = field(default=None, repr=False)
    _last_error: str | None = field(default=None, repr=False)
    _half_open_requests: int = field(default=0, repr=False)

    @property
    def state(self) -> CircuitState:
        """Get current circuit state, checking for automatic transitions"""
        if self._state == CircuitState.OPEN and self._last_failure_time and time.time() - self._last_failure_time >= self.reset_timeout:
                self._state = CircuitState.HALF_OPEN
                self._half_open_requests = 0
        return self._state

    def allow_request(self) -> bool:
        """
        Check if a request should be allowed.

        Returns:
            True if request is allowed, False if circuit is open
        """
        current_state = self.state

        if current_state == CircuitState.CLOSED:
            return True

        if current_state == CircuitState.OPEN:
            return False

        if current_state == CircuitState.HALF_OPEN:
            # Allow limited requests in half-open state
            if self._half_open_requests < self.half_open_max:
                self._half_open_requests += 1
                return True
            return False

        return False

    def record_success(self) -> None:
        """
        Record a successful request.

        If in HALF_OPEN state, transitions to CLOSED.
        """
        self._success_count += 1

        if self._state == CircuitState.HALF_OPEN:
            # Transition to CLOSED on success
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._half_open_requests = 0
            self._last_error = None

    def record_failure(self, error: str = "") -> None:
        """
        Record a failed request.

        If failure count exceeds threshold, transitions to OPEN.

        Args:
            error: Error message
        """
        self._failure_count += 1
        self._last_failure_time = time.time()
        self._last_error = error

        if self._state == CircuitState.HALF_OPEN:
            # Transition back to OPEN on failure
            self._state = CircuitState.OPEN
            self._half_open_requests = 0
        elif self._failure_count >= self.failure_threshold:
            # Transition to OPEN
            self._state = CircuitState.OPEN

    def reset(self) -> None:
        """Reset circuit breaker to CLOSED state"""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = None
        self._last_error = None
        self._half_open_requests = 0

    @property
    def is_open(self) -> bool:
        """Check if circuit is open"""
        return self.state == CircuitState.OPEN

    @property
    def is_closed(self) -> bool:
        """Check if circuit is closed"""
        return self.state == CircuitState.CLOSED

    @property
    def failure_count(self) -> int:
        """Get failure count"""
        return self._failure_count

    @property
    def success_count(self) -> int:
        """Get success count"""
        return self._success_count

    @property
    def last_error(self) -> str | None:
        """Get last error message"""
        return self._last_error

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization"""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "last_failure_time": self._last_failure_time,
            "last_error": self._last_error,
            "failure_threshold": self.failure_threshold,
            "reset_timeout": self.reset_timeout,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CircuitBreaker":
        """Create from dictionary"""
        breaker = cls(
            name=data["name"],
            failure_threshold=data.get("failure_threshold", 5),
            reset_timeout=data.get("reset_timeout", 3600),
        )
        breaker._state = CircuitState(data.get("state", "closed"))
        breaker._failure_count = data.get("failure_count", 0)
        breaker._success_count = data.get("success_count", 0)
        breaker._last_failure_time = data.get("last_failure_time")
        breaker._last_error = data.get("last_error")
        return breaker


class CircuitBreakerOpenError(Exception):
    """Exception raised when circuit breaker is open"""

    def __init__(self, service_name: str, last_error: str | None = None) -> None:
        """Initialize circuit breaker open exception.

        Args:
            service_name: Name of the service with open circuit.
            last_error: The error that triggered the circuit to open.
        """
        self.service_name = service_name
        self.last_error = last_error
        message = f"Circuit breaker is open for {service_name}"
        if last_error:
            message += f": {last_error}"
        super().__init__(message)


class CircuitBreakerManager:
    """
    Manager for multiple circuit breakers.

    Tracks circuit breakers for all platforms and provides
    unified access and serialization.
    """

    def __init__(self) -> None:
        """Initialize an empty circuit breaker manager."""
        self._breakers: dict[str, CircuitBreaker] = {}

    def get_or_create(
        self,
        name: str,
        failure_threshold: int = 5,
        reset_timeout: int = 3600,
    ) -> CircuitBreaker:
        """
        Get or create a circuit breaker.

        Args:
            name: Service name
            failure_threshold: Failure threshold
            reset_timeout: Reset timeout

        Returns:
            Circuit breaker instance
        """
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(
                name=name,
                failure_threshold=failure_threshold,
                reset_timeout=reset_timeout,
            )
        return self._breakers[name]

    def allow_request(self, name: str) -> bool:
        """Check if a request is allowed for a named service.

        Returns True if no circuit breaker exists for the service
        (permissive default for untracked services).

        Args:
            name: Service/platform name.

        Returns:
            True if request should proceed, False if circuit is open.
        """

        if name not in self._breakers:
            return True  # No breaker = allow
        return self._breakers[name].allow_request()

    def record_success(self, name: str) -> None:
        """Record a successful request for a service.

        No-op if no circuit breaker exists for the service.

        Args:
            name: Service/platform name.
        """

        if name in self._breakers:
            self._breakers[name].record_success()

    def record_failure(self, name: str, error: str = "") -> None:
        """Record a failed request for a service.

        Auto-creates a circuit breaker if one doesn't exist yet.

        Args:
            name: Service/platform name.
            error: Error message for diagnostics.
        """

        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(name=name)
        self._breakers[name].record_failure(error)

    def reset(self, name: str) -> None:
        """Reset a circuit breaker"""
        if name in self._breakers:
            self._breakers[name].reset()

    def get_status(self) -> dict[str, dict]:
        """Get status of all managed circuit breakers.

        Returns:
            Dict mapping service name to its serialized state.
        """

        return {name: breaker.to_dict() for name, breaker in self._breakers.items()}

    def to_dict(self) -> dict[str, dict]:
        """Serialize all circuit breakers"""
        return {name: breaker.to_dict() for name, breaker in self._breakers.items()}

    def from_dict(self, data: dict[str, dict]) -> None:
        """Load circuit breakers from dictionary"""
        for name, breaker_data in data.items():
            self._breakers[name] = CircuitBreaker.from_dict(breaker_data)
