"""Bundled media-binary resolution (Tauri app zero-download goal).

Covers the XPST_FFMPEG_PATH / XPST_FFPROBE_PATH / XPST_YTDLP_PATH env
overrides the Tauri shell sets on the spawned engine, plus the
resolve_ytdlp_path() fallback chain.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from xpst.utils.platform import (
    get_ytdlp_fallback_path,
    resolve_ffmpeg_path,
    resolve_ffprobe_path,
    resolve_ytdlp_path,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("XPST_FFMPEG_PATH", "XPST_FFPROBE_PATH", "XPST_YTDLP_PATH"):
        monkeypatch.delenv(var, raising=False)


def test_ffmpeg_env_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = tmp_path / "ffmpeg"
    fake.write_bytes(b"#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("XPST_FFMPEG_PATH", str(fake))
    assert resolve_ffmpeg_path() == str(fake)


def test_ffmpeg_env_override_ignores_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XPST_FFMPEG_PATH", "/nonexistent/ffmpeg")
    # Falls through to PATH / well-known probing; must not return the bogus path.
    assert resolve_ffmpeg_path() != "/nonexistent/ffmpeg"


def test_ffprobe_env_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = tmp_path / "ffprobe"
    fake.write_bytes(b"#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("XPST_FFPROBE_PATH", str(fake))
    assert resolve_ffprobe_path() == str(fake)


def test_ffprobe_env_override_ignores_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XPST_FFPROBE_PATH", "/nonexistent/ffprobe")
    assert resolve_ffprobe_path() != "/nonexistent/ffprobe"


def test_ytdlp_env_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = tmp_path / "yt-dlp"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("XPST_YTDLP_PATH", str(fake))
    assert resolve_ytdlp_path() == fake


def test_ytdlp_env_override_ignores_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XPST_YTDLP_PATH", "/nonexistent/yt-dlp")
    resolved = resolve_ytdlp_path()
    assert resolved is None or str(resolved) != "/nonexistent/yt-dlp"


def test_ytdlp_fallback_used_when_no_env_and_no_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("xpst.utils.platform.shutil.which", lambda name: None)
    fallback = tmp_path / "yt-dlp"
    fallback.write_text("#!/bin/sh\n", encoding="utf-8")
    fallback.chmod(0o755)
    monkeypatch.setattr(
        "xpst.utils.platform.get_ytdlp_fallback_path", lambda: fallback
    )
    assert resolve_ytdlp_path() == fallback


def test_ytdlp_returns_none_when_nothing_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("xpst.utils.platform.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "xpst.utils.platform.get_ytdlp_fallback_path",
        lambda: tmp_path / "does-not-exist",
    )
    assert resolve_ytdlp_path() is None


def test_get_ytdlp_fallback_path_platform_shapes() -> None:
    path = get_ytdlp_fallback_path()
    assert isinstance(path, Path)
    assert path.name in {"yt-dlp", "yt-dlp.exe"}
