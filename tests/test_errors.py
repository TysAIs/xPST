"""Tests for error categorization module"""

from xpst.utils.errors import (
    ErrorCategory,
    categorize_error,
    is_fatal,
    is_retryable,
)


class TestCategorizeError:
    """Test error categorization logic"""

    def test_retryable_timeout(self):
        """Timeout errors should be retryable"""
        err = TimeoutError("Connection timed out")
        result = categorize_error(err)
        assert result.category == ErrorCategory.RETRYABLE
        assert result.is_retryable
        assert not result.is_fatal

    def test_retryable_connection_error(self):
        """Connection errors should be retryable"""
        err = ConnectionError("Connection reset by peer")
        result = categorize_error(err)
        assert result.category == ErrorCategory.RETRYABLE

    def test_retryable_os_error(self):
        """OS errors (network) should be retryable"""
        err = OSError("Network is unreachable")
        result = categorize_error(err)
        assert result.category == ErrorCategory.RETRYABLE

    def test_retryable_429(self):
        """HTTP 429 should be retryable"""
        err = Exception("HTTP 429 Too Many Requests")
        result = categorize_error(err)
        assert result.category == ErrorCategory.RETRYABLE

    def test_retryable_503(self):
        """HTTP 503 should be retryable"""
        err = Exception("503 Service Unavailable")
        result = categorize_error(err)
        assert result.category == ErrorCategory.RETRYABLE

    def test_retryable_rate_limit(self):
        """Rate limit messages should be retryable"""
        err = Exception("Rate limit exceeded, try again later")
        result = categorize_error(err)
        assert result.category == ErrorCategory.RETRYABLE

    def test_retryable_502(self):
        """HTTP 502 should be retryable"""
        err = Exception("Bad Gateway (502)")
        result = categorize_error(err)
        assert result.category == ErrorCategory.RETRYABLE

    def test_fatal_401(self):
        """HTTP 401 should be fatal"""
        err = Exception("401 Unauthorized")
        result = categorize_error(err)
        assert result.category == ErrorCategory.FATAL
        assert result.is_fatal
        assert not result.is_retryable

    def test_fatal_403(self):
        """HTTP 403 should be fatal"""
        err = Exception("403 Forbidden")
        result = categorize_error(err)
        assert result.category == ErrorCategory.FATAL

    def test_fatal_invalid_format(self):
        """Invalid video format should be fatal"""
        err = Exception("Invalid video format: codec not supported")
        result = categorize_error(err)
        assert result.category == ErrorCategory.FATAL

    def test_fatal_token_expired(self):
        """Expired tokens should be fatal"""
        err = Exception("Token expired - re-authenticate")
        result = categorize_error(err)
        assert result.category == ErrorCategory.FATAL

    def test_fatal_session_expired(self):
        """Expired sessions should be fatal"""
        err = Exception("Session expired, login required")
        result = categorize_error(err)
        assert result.category == ErrorCategory.FATAL

    def test_fatal_quota_exceeded(self):
        """Quota exceeded should be fatal (no point retrying same day)"""
        err = Exception("Daily quota exceeded")
        result = categorize_error(err)
        assert result.category == ErrorCategory.FATAL

    def test_fatal_file_not_found(self):
        """File not found should be fatal"""
        err = Exception("File not found: /path/to/video.mp4")
        result = categorize_error(err)
        assert result.category == ErrorCategory.FATAL

    def test_unknown_errors_default_retryable(self):
        """Unknown errors default to UNKNOWN (treated as retryable)"""
        err = Exception("Something weird happened")
        result = categorize_error(err)
        assert result.category == ErrorCategory.UNKNOWN

    def test_platform_youtube_quota(self):
        """YouTube quota errors should be fatal"""
        err = Exception("YOUTUBE_QUOTA_EXCEEDED: Daily quota limit reached")
        result = categorize_error(err, platform="youtube")
        assert result.category == ErrorCategory.FATAL

    def test_platform_youtube_auth(self):
        """YouTube auth errors should be retryable (may be temporary)"""
        err = Exception("YOUTUBE_AUTH_EXPIRED: Run 'xpst auth youtube'")
        result = categorize_error(err, platform="youtube")
        assert result.category == ErrorCategory.RETRYABLE

    def test_platform_x_session_expired(self):
        """X session expired should be fatal"""
        err = Exception("X_SESSION_EXPIRED: Run 'xpst auth x'")
        result = categorize_error(err, platform="x")
        assert result.category == ErrorCategory.FATAL

    def test_platform_x_rate_limited(self):
        """X rate limit should be retryable"""
        err = Exception("X_RATE_LIMITED: Too many requests")
        result = categorize_error(err, platform="x")
        assert result.category == ErrorCategory.RETRYABLE

    def test_platform_ig_session_expired(self):
        """IG session expired should be fatal"""
        err = Exception("IG_SESSION_EXPIRED: Run 'xpst auth instagram'")
        result = categorize_error(err, platform="instagram")
        assert result.category == ErrorCategory.FATAL

    def test_platform_ig_invalid_format(self):
        """IG invalid format should be fatal"""
        err = Exception("IG_INVALID_FORMAT: Video format not supported")
        result = categorize_error(err, platform="instagram")
        assert result.category == ErrorCategory.FATAL

    def test_platform_ig_rate_limited(self):
        """IG rate limit should be retryable"""
        err = Exception("IG_RATE_LIMITED: Too many requests")
        result = categorize_error(err, platform="instagram")
        assert result.category == ErrorCategory.RETRYABLE

    def test_error_with_status_code_attribute(self):
        """Errors with status_code attribute should be categorized"""
        err = Exception("Request failed")
        err.status_code = 429
        result = categorize_error(err)
        assert result.category == ErrorCategory.RETRYABLE
        assert result.http_status == 429

    def test_error_with_response_status(self):
        """Errors with response.status should be categorized"""

        class FakeResponse:
            status_code = 401

        err = Exception("Auth failed")
        err.response = FakeResponse()
        result = categorize_error(err)
        assert result.category == ErrorCategory.FATAL
        assert result.http_status == 401

    def test_categorized_error_message(self):
        """CategorizedError should preserve the message"""
        err = ValueError("test error message")
        result = categorize_error(err)
        assert result.message == "test error message"
        assert result.original_exception is err

    def test_categorized_error_platform(self):
        """CategorizedError should track platform"""
        err = Exception("test")
        result = categorize_error(err, platform="youtube")
        assert result.platform == "youtube"


class TestHelperFunctions:
    """Test is_retryable and is_fatal helpers"""

    def test_is_retryable_timeout(self):
        assert is_retryable(TimeoutError("timed out"))

    def test_is_retryable_rate_limit(self):
        assert is_retryable(Exception("429 rate limit"))

    def test_is_fatal_unauthorized(self):
        assert is_fatal(Exception("401 Unauthorized"))

    def test_is_fatal_format(self):
        assert is_fatal(Exception("Invalid video format"))

    def test_is_retryable_with_platform(self):
        assert is_retryable(Exception("X_RATE_LIMITED"), platform="x")

    def test_is_fatal_with_platform(self):
        assert is_fatal(Exception("IG_SESSION_EXPIRED"), platform="instagram")
