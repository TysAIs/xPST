"""Regression: the upload duration guard must actually read manifest limits.

The pre-flight guard (punch-list #2, DEEP-REVIEW-2026-09-28) called
``uploader.manifest()`` — but ``manifest`` is a PROPERTY on every uploader, so
the call raised TypeError and the bare ``except`` swallowed it, returning
``None`` for every platform: YouTube's 60 s, X's 140 s and Threads' declared
caps silently never applied. These tests use the REAL uploader classes, so
they falsify exactly that expression.
"""

from unittest.mock import MagicMock

import pytest

from xpst.config import XPSTConfig
from xpst.platforms.threads import ThreadsUploader
from xpst.platforms.x import XUploader
from xpst.platforms.youtube import YouTubeUploader
from xpst.services.upload_service import UploadService


@pytest.fixture
def service() -> UploadService:
    return UploadService(
        video_processor=MagicMock(),
        circuit_breakers=MagicMock(),
        quota_manager=MagicMock(),
        state=MagicMock(),
        notifier=MagicMock(),
        shutdown_handler=MagicMock(),
        config=XPSTConfig(),
    )


class TestDurationLimitIsLive:
    def test_youtube_declares_60s_cap_and_guard_sees_it(self, service):
        """YouTube Shorts' declared 60 s limit must reach the guard."""
        assert YouTubeUploader(XPSTConfig()).manifest.extra["max_duration_seconds"] == 60
        assert service._duration_limit(YouTubeUploader(XPSTConfig())) == 60

    def test_x_declares_140s_cap_and_guard_sees_it(self, service):
        assert service._duration_limit(XUploader(XPSTConfig())) == 140

    def test_threads_cap_reaches_the_guard(self, service):
        uploader = ThreadsUploader(XPSTConfig())
        declared = uploader.manifest.extra["max_video_duration_seconds"]
        assert service._duration_limit(uploader) == declared

    def test_uploader_without_duration_key_returns_none(self, service):
        class NoLimit:
            @property
            def manifest(self):
                return MagicMock(extra={})

        assert service._duration_limit(NoLimit()) is None

    def test_uploader_with_broken_manifest_fails_open(self, service):
        class Broken:
            @property
            def manifest(self):
                raise RuntimeError("provider exploded")

        assert service._duration_limit(Broken()) is None
