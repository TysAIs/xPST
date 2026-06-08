"""
Error categorization for XPST

Classifies errors as retryable vs fatal to optimize retry behavior.
Retryable errors trigger automatic retries with backoff.
Fatal errors fail immediately without wasting retry attempts.

Retryable:
- Network timeouts (ConnectionError, TimeoutError, socket timeout)
- HTTP 429 (Too Many Requests / rate limit)
- HTTP 503 (Service Unavailable)
- HTTP 500 (Internal Server Error)
- DNS resolution failures
- Connection resets

Fatal:
- HTTP 401 (Unauthorized - session expired)
- HTTP 403 (Forbidden - banned/invalid credentials)
- Invalid video format (codec/resolution unsupported)
- File not found
- Invalid configuration
- Quota exceeded (no point retrying same day)
"""

import re
from dataclasses import dataclass
from enum import Enum


class ErrorCategory(Enum):
    """Error classification for retry decisions"""
    RETRYABLE = "retryable"
    FATAL = "fatal"
    UNKNOWN = "unknown"


# HTTP status code patterns
_RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
_FATAL_HTTP_CODES = {400, 401, 403, 404, 405, 413, 415, 422}

# Error message patterns (case-insensitive)
_RETRYABLE_PATTERNS = [
    r"timeout",
    r"timed?\s*out",
    r"connection\s*(reset|refused|error|aborted|blocked)",
    r"network\s*(error|unreachable)",
    r"temporary\s*failure",
    r"service\s*unavailable",
    r"bad\s*gateway",
    r"gateway\s*timeout",
    r"dns\s*resolution",
    r"name\s*resolution",
    r"too\s*many\s*(requests|retries)",
    r"rate\s*limit",
    r"429",
    r"503",
    r"502",
    r"500",
    r"eof\s*(error|occurred)",
    r"broken\s*pipe",
    r"socket\s*(error|timeout)",
    r"ssl\s*(error|handshake)",
    r"server\s*(is\s*)?busy",
    r"try\s*again",
    r"temporarily",
    r"httplib2",
    r"transport\s*error",
    r"requests\.exceptions",
    r"connectionpool",
    r"remote\s*end\s*closed",
    r"incomplete\s*read",
    r"chunked\s*encoding",
]

_FATAL_PATTERNS = [
    r"unauthorized",
    r"401",
    r"forbidden",
    r"403",
    r"invalid\s*(video|format|codec|file)",
    r"unsupported\s*(format|codec|resolution|media)",
    r"video\s*(format|codec)\s*not\s*supported",
    r"invalid\s*grant",
    r"token\s*(expired|invalid|revoked)",
    r"credentials\s*(expired|invalid|missing|not\s*found)",
    r"session\s*(expired|invalid|not\s*found)",
    r"login\s*(required|failed|expired)",
    r"authentication\s*(failed|expired|required)",
    r"quota\s*(exceeded|reached|limit)",
    r"daily\s*(quota|limit)\s*(exceeded|reached)",
    r"file\s*not\s*found",
    r"no\s*such\s*file",
    r"permission\s*denied",
    r"account\s*(suspended|locked|disabled|restricted)",
    r"banned",
    r"(?<!connection\s)\bblocked\b",
    r"\bnot\s*found\b",
    r"no\s*channel",
    r"client_secrets.*not\s*found",
    r"cookies.*not\s*found",
    r"session.*not\s*found",
]

# Compile patterns for performance
_retryable_re = re.compile("|".join(_RETRYABLE_PATTERNS), re.IGNORECASE)
_fatal_re = re.compile("|".join(_FATAL_PATTERNS), re.IGNORECASE)


@dataclass
class CategorizedError:
    """An error with its category and metadata"""
    category: ErrorCategory
    message: str
    original_exception: Exception | None = None
    http_status: int | None = None
    platform: str | None = None

    @property
    def is_retryable(self) -> bool:
        """Check if this error should be retried."""
        return self.category == ErrorCategory.RETRYABLE

    @property
    def is_fatal(self) -> bool:
        """Check if this error is fatal (should not retry)."""
        return self.category == ErrorCategory.FATAL


def categorize_error(
    error: Exception,
    platform: str | None = None,
) -> CategorizedError:
    """
    Categorize an exception as retryable or fatal.

    Args:
        error: The exception to categorize
        platform: Optional platform name for context

    Returns:
        CategorizedError with classification
    """
    error_str = str(error)
    error_type = type(error).__name__
    full_msg = f"{error_type}: {error_str}"

    # Check for HTTP status codes in the error
    http_status = _extract_http_status(error, error_str)

    # Check by HTTP status code first (most reliable)
    if http_status is not None:
        if http_status in _RETRYABLE_HTTP_CODES:
            return CategorizedError(
                category=ErrorCategory.RETRYABLE,
                message=error_str,
                original_exception=error,
                http_status=http_status,
                platform=platform,
            )
        if http_status in _FATAL_HTTP_CODES:
            return CategorizedError(
                category=ErrorCategory.FATAL,
                message=error_str,
                original_exception=error,
                http_status=http_status,
                platform=platform,
            )

    # Check for common retryable exception types
    if isinstance(error, (ConnectionError, TimeoutError, OSError)):
        return CategorizedError(
            category=ErrorCategory.RETRYABLE,
            message=error_str,
            original_exception=error,
            platform=platform,
        )

    # Check for specific third-party exception types
    error_module = type(error).__module__ or ""
    if ("requests" in error_module or "urllib3" in error_module) and ("ConnectionError" in error_type or "Timeout" in error_type):


            return CategorizedError(
                category=ErrorCategory.RETRYABLE,
                message=error_str,
                original_exception=error,
                platform=platform,
            )

    # Check for httpx errors
    if "httpx" in error_module and ("Timeout" in error_type or "Connect" in error_type):
        return CategorizedError(
            category=ErrorCategory.RETRYABLE,
            message=error_str,
            original_exception=error,
            platform=platform,
        )

    # Check for google auth errors
    if "google" in error_module and ("RefreshError" in error_type or "AuthError" in error_type):
        return CategorizedError(
            category=ErrorCategory.FATAL,
            message=error_str,
            original_exception=error,
            platform=platform,
        )

    # Pattern match on error message
    if _fatal_re.search(full_msg):
        return CategorizedError(
            category=ErrorCategory.FATAL,
            message=error_str,
            original_exception=error,
            platform=platform,
        )

    if _retryable_re.search(full_msg):
        return CategorizedError(
            category=ErrorCategory.RETRYABLE,
            message=error_str,
            original_exception=error,
            platform=platform,
        )

    # Check for known platform-specific error prefixes
    if platform:
        platform_result = _check_platform_error(error_str, platform)
        if platform_result is not None:
            return CategorizedError(
                category=platform_result,
                message=error_str,
                original_exception=error,
                platform=platform,
            )

    # Default: unknown - treat as retryable (be optimistic)
    return CategorizedError(
        category=ErrorCategory.UNKNOWN,
        message=error_str,
        original_exception=error,
        platform=platform,
    )


def _extract_http_status(error: Exception, error_str: str) -> int | None:
    """Extract an HTTP status code from an exception or error message.

    Checks: exception attributes (status_code, code, status), response
    objects, and regex patterns in the error string.

    Args:
        error: The exception object.
        error_str: String representation of the error.

    Returns:
        HTTP status code (100-599) or None if not found.
    """

    # Check if exception has a status_code attribute
    for attr in ("status_code", "code", "status"):
        val = getattr(error, attr, None)
        if isinstance(val, int) and 100 <= val < 600:
            return val

    # Check response object
    response = getattr(error, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None) or getattr(response, "status", None)
        if isinstance(status, int) and 100 <= status < 600:
            return status

    # Pattern match in error message
    status_match = re.search(r"\b([45]\d{2})\b", error_str)
    if status_match:
        return int(status_match.group(1))

    return None


def _check_platform_error(error_str: str, platform: str) -> ErrorCategory:
    """Classify platform-specific error patterns.

    Each platform has known error prefixes (e.g., ``YOUTUBE_QUOTA_EXCEEDED``,
    ``X_SESSION_EXPIRED``, ``IG_RATE_LIMITED``) that indicate whether
    the error is retryable or fatal.

    Args:
        error_str: Error message string.
        platform: Platform name (youtube, x, instagram).

    Returns:
        ErrorCategory if a pattern matched, None otherwise.
    """

    error_lower = error_str.lower()

    if platform == "youtube":
        if "quota" in error_lower:
            return ErrorCategory.FATAL
        if "youtube_auth_expired" in error_lower or "youtube_upload_error" in error_lower:
            return ErrorCategory.RETRYABLE

    elif platform == "x":
        if "x_session_expired" in error_lower:
            return ErrorCategory.FATAL
        if "x_rate_limited" in error_lower:
            return ErrorCategory.RETRYABLE
        if "duplicate" in error_lower:
            return ErrorCategory.FATAL  # Not an error, already posted

    elif platform == "instagram":
        if "ig_session_expired" in error_lower:
            return ErrorCategory.FATAL
        if "ig_rate_limited" in error_lower:
            return ErrorCategory.RETRYABLE
        if "ig_invalid_format" in error_lower:
            return ErrorCategory.FATAL

    return None


def is_retryable(error: Exception, platform: str | None = None) -> bool:
    """
    Quick check if an error is retryable.

    Args:
        error: The exception to check
        platform: Optional platform name

    Returns:
        True if the error should be retried
    """
    return categorize_error(error, platform).is_retryable


def is_fatal(error: Exception, platform: str | None = None) -> bool:
    """
    Quick check if an error is fatal (should not retry).

    Args:
        error: The exception to check
        platform: Optional platform name

    Returns:
        True if the error should NOT be retried
    """
    return categorize_error(error, platform).is_fatal
