"""Tests for retry module with error categorization"""


import pytest

from xpst.utils.retry import (
    STANDARD_RETRY,
    RetryConfig,
    retry_async,
    retry_operation,
    retry_sync,
)


class TestRetryConfig:
    """Test retry configuration"""

    def test_standard_retry_has_fixed_delays(self):
        """Standard retry should use 1s/2s/4s delays"""
        assert STANDARD_RETRY.fixed_delays == [1.0, 2.0, 4.0]
        assert STANDARD_RETRY.max_retries == 3

    def test_get_backoff_fixed_delays(self):
        """Should use fixed delays when configured"""
        config = RetryConfig(fixed_delays=[1.0, 2.0, 4.0], jitter=0)
        assert config.get_backoff(0) == 1.0
        assert config.get_backoff(1) == 2.0
        assert config.get_backoff(2) == 4.0

    def test_get_backoff_exponential(self):
        """Should use exponential backoff when no fixed delays"""
        config = RetryConfig(backoff_base=2, jitter=0)
        assert config.get_backoff(0) == 1  # 2^0 = 1
        assert config.get_backoff(1) == 2  # 2^1 = 2
        assert config.get_backoff(2) == 4  # 2^2 = 4

    def test_get_backoff_jitter(self):
        """Jitter should add randomness"""
        config = RetryConfig(backoff_base=2, jitter=0.5)
        # Run multiple times and check range
        backoffs = [config.get_backoff(1) for _ in range(100)]
        # All should be near 2.0 (±50%)
        assert all(1.0 <= b <= 3.0 for b in backoffs)
        # But not all identical (very unlikely with jitter)
        assert len(set(round(b, 2) for b in backoffs)) > 1


class TestRetrySync:
    """Test synchronous retry decorator"""

    def test_success_no_retry(self):
        """Should not retry on success"""
        call_count = 0

        @retry_sync(config=RetryConfig(max_retries=3, jitter=0))
        def success_func():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = success_func()
        assert result == "ok"
        assert call_count == 1

    def test_retry_on_retryable_error(self):
        """Should retry on retryable errors"""
        call_count = 0

        @retry_sync(config=RetryConfig(max_retries=2, backoff_base=1, jitter=0))
        def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise TimeoutError("Connection timed out")
            return "ok"

        result = flaky_func()
        assert result == "ok"
        assert call_count == 3

    def test_no_retry_on_fatal_error(self):
        """Should NOT retry on fatal errors"""
        call_count = 0

        @retry_sync(config=RetryConfig(max_retries=3, jitter=0))
        def fatal_func():
            nonlocal call_count
            call_count += 1
            raise Exception("401 Unauthorized - session expired")

        with pytest.raises(Exception, match="Unauthorized"):
            fatal_func()

        assert call_count == 1  # Only called once, no retries


class TestRetryAsync:
    """Test asynchronous retry decorator"""

    @pytest.mark.asyncio
    async def test_async_retry_on_retryable(self):
        """Should retry async operations on retryable errors"""
        call_count = 0

        @retry_async(config=RetryConfig(max_retries=2, backoff_base=1, jitter=0))
        async def flaky_async():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("Connection refused")
            return "ok"

        result = await flaky_async()
        assert result == "ok"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_async_no_retry_on_fatal(self):
        """Should NOT retry async operations on fatal errors"""
        call_count = 0

        @retry_async(config=RetryConfig(max_retries=3, jitter=0))
        async def fatal_async():
            nonlocal call_count
            call_count += 1
            raise Exception("403 Forbidden - account banned")

        with pytest.raises(Exception, match="Forbidden"):
            await fatal_async()

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retry_operation_with_platform(self):
        """Should use platform context for error categorization"""
        call_count = 0

        async def flaky_upload():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise Exception("X_RATE_LIMITED: Too many requests")
            return "uploaded"

        result = await retry_operation(
            flaky_upload,
            config=RetryConfig(max_retries=2, backoff_base=1, jitter=0),
            platform="x",
        )
        assert result == "uploaded"

    @pytest.mark.asyncio
    async def test_retry_operation_fatal_no_retry(self):
        """Fatal platform errors should not retry"""
        call_count = 0

        async def auth_expired():
            nonlocal call_count
            call_count += 1
            raise Exception("IG_SESSION_EXPIRED: Run 'xpst auth instagram'")

        with pytest.raises(Exception, match="SESSION_EXPIRED"):
            await retry_operation(
                auth_expired,
                config=RetryConfig(max_retries=3, jitter=0),
                platform="instagram",
            )

        assert call_count == 1
