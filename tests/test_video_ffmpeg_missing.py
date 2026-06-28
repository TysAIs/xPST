"""Tests for the friendly missing-FFmpeg error path (W3-3).

These verify the custom exception's structure and message without invoking a
real ffmpeg binary (subprocess is patched), and confirm backwards
compatibility with existing RuntimeError handlers.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from xpst.utils import video
from xpst.utils.video import FFmpegNotFoundError, VideoProcessor, ffmpeg_install_hint


def test_ffmpeg_not_found_error_subclasses_runtime_error():
    err = FFmpegNotFoundError("ffmpeg")
    assert isinstance(err, RuntimeError)


def test_error_message_is_actionable():
    err = FFmpegNotFoundError("ffmpeg")
    msg = str(err)
    assert "FFmpeg" in msg
    assert "ffmpeg.org" in msg
    assert "required" in msg.lower()
    # Carries the path that was probed.
    assert err.ffmpeg_path == "ffmpeg"


@pytest.mark.parametrize(
    "platform,needle",
    [
        ("darwin", "brew install ffmpeg"),
        ("win32", "ffmpeg.org"),
        ("linux", "apt install ffmpeg"),
    ],
)
def test_install_hint_is_platform_specific(monkeypatch, platform, needle):
    monkeypatch.setattr(sys, "platform", platform)
    assert needle in ffmpeg_install_hint()


def test_verify_raises_friendly_error_when_binary_missing(monkeypatch):
    # Simulate the ffmpeg binary not being on PATH without running anything.
    def _raise_fnf(*args, **kwargs):
        raise FileNotFoundError("no such file: ffmpeg")

    monkeypatch.setattr(video.subprocess, "run", _raise_fnf)
    with pytest.raises(FFmpegNotFoundError) as excinfo:
        VideoProcessor(ffmpeg_path="ffmpeg")
    assert "FFmpeg is required" in str(excinfo.value)


def test_verify_raises_friendly_error_on_nonzero_exit(monkeypatch):
    # ffmpeg present but returns non-zero -> still a friendly error.
    completed = subprocess.CompletedProcess(args=["ffmpeg", "-version"], returncode=1)
    monkeypatch.setattr(video.subprocess, "run", lambda *a, **k: completed)
    with pytest.raises(FFmpegNotFoundError):
        VideoProcessor(ffmpeg_path="ffmpeg")


def test_existing_runtime_error_handlers_still_catch_it(monkeypatch):
    # Callers that catch RuntimeError keep working (backwards compatibility).
    def _raise_fnf(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(video.subprocess, "run", _raise_fnf)
    caught = False
    try:
        VideoProcessor(ffmpeg_path="ffmpeg")
    except RuntimeError:
        caught = True
    assert caught


def test_verify_succeeds_when_ffmpeg_present(monkeypatch):
    completed = subprocess.CompletedProcess(args=["ffmpeg", "-version"], returncode=0)
    monkeypatch.setattr(video.subprocess, "run", lambda *a, **k: completed)
    # Should not raise.
    vp = VideoProcessor(ffmpeg_path="ffmpeg")
    assert vp.ffmpeg_path == "ffmpeg"
