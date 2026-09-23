"""Composer media-preview contracts: streaming with range support + thumbnails.

The composer's promise is that a selected asset is *visible* before it is
posted without the engine ever paying a full-file read for it. These tests
hold that line at the two places it can break:

* ``GET /api/media/stream`` must implement HTTP range semantics (206 +
  ``Content-Range``) and must read the source in bounded chunks — the
  "does not read the whole file into memory" acceptance test spies on every
  ``read()`` the route performs and fails if one call ever asks for more than
  one chunk.
* ``GET /api/media/thumb`` must give the UI a small JPEG when it can and a
  plain 404 (never an error page, never the source file) when it cannot.

No test touches the user's ``~/.xpst``: the preview cache is redirected to
``tmp_path``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst.dashboard import media_preview
from xpst.dashboard.api import create_api_router


@pytest.fixture(autouse=True)
def _preview_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every generated thumbnail inside the test's tmp dir."""
    cache = tmp_path / "previews"
    monkeypatch.setenv("XPST_PREVIEW_CACHE", str(cache))
    return cache


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(create_api_router(str(tmp_path / "cfg")))
    return TestClient(app)


def _write_media(tmp_path: Path, name: str = "clip.mp4", size: int = 4096) -> Path:
    target = tmp_path / name
    target.write_bytes(bytes(range(256)) * (size // 256) + b"\x00" * (size % 256))
    return target


# ── Streaming ─────────────────────────────────────────────────────────────


def test_stream_serves_the_whole_file_and_advertises_range_support(tmp_path: Path) -> None:
    media = _write_media(tmp_path, size=2048)
    response = _client(tmp_path).get("/api/media/stream", params={"path": str(media)})

    assert response.status_code == 200
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-type"] == "video/mp4"
    assert int(response.headers["content-length"]) == 2048
    assert response.content == media.read_bytes()


def test_a_range_request_answers_206_with_the_exact_byte_span(tmp_path: Path) -> None:
    media = _write_media(tmp_path, size=4096)
    response = _client(tmp_path).get(
        "/api/media/stream", params={"path": str(media)}, headers={"Range": "bytes=100-199"}
    )

    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 100-199/4096"
    assert int(response.headers["content-length"]) == 100
    assert response.content == media.read_bytes()[100:200]


def test_an_open_ended_range_streams_to_the_end_of_the_file(tmp_path: Path) -> None:
    media = _write_media(tmp_path, size=4096)
    response = _client(tmp_path).get(
        "/api/media/stream", params={"path": str(media)}, headers={"Range": "bytes=4000-"}
    )

    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 4000-4095/4096"
    assert response.content == media.read_bytes()[4000:]


def test_a_range_past_the_end_of_the_file_is_416_not_the_whole_file(tmp_path: Path) -> None:
    media = _write_media(tmp_path, size=1024)
    response = _client(tmp_path).get(
        "/api/media/stream", params={"path": str(media)}, headers={"Range": "bytes=99999-"}
    )

    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */1024"
    assert response.json()["size_bytes"] == 1024


def test_the_stream_route_never_reads_the_whole_file_into_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance test: preview does not cost a full-file read.

    A 16 MiB file is requested as a 1 KiB range while every ``read()`` the
    route performs is recorded. The route must (a) stay within the requested
    span and (b) never ask for more than one chunk at a time — a
    ``read_bytes()``/whole-file read shows up here as a single oversized read.
    """
    size = 16 * 1024 * 1024
    media = tmp_path / "big.mp4"
    media.write_bytes(b"\x00" * size)

    reads: list[int] = []
    real_open = open

    class SpyFile:
        def __init__(self, handle):  # noqa: ANN001
            self._handle = handle

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *exc):  # noqa: ANN002
            return self._handle.__exit__(*exc)

        def seek(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return self._handle.seek(*args, **kwargs)

        def read(self, amount: int | None = -1) -> bytes:
            reads.append(amount if amount is not None else -1)
            return self._handle.read(amount)

    def spy_open(file, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        handle = real_open(file, *args, **kwargs)
        if str(file) == str(media):
            return SpyFile(handle)
        return handle

    monkeypatch.setattr(media_preview, "open", spy_open, raising=False)

    response = _client(tmp_path).get(
        "/api/media/stream", params={"path": str(media)}, headers={"Range": "bytes=1048576-1049599"}
    )

    assert response.status_code == 206
    assert len(response.content) == 1024
    assert reads, "the route read nothing — the spy did not observe the preview path"
    assert max(reads) <= media_preview.DEFAULT_CHUNK_SIZE
    assert sum(reads) <= 1024 + media_preview.DEFAULT_CHUNK_SIZE
    assert max(reads) < size, "the preview asked for the whole file in one read"


def test_iter_file_chunks_is_bounded_for_a_whole_file_stream(tmp_path: Path) -> None:
    """Even the no-Range path is chunked, not slurped."""
    media = tmp_path / "chunky.mp4"
    media.write_bytes(b"x" * (media_preview.DEFAULT_CHUNK_SIZE * 2 + 5))

    chunks = list(media_preview.iter_file_chunks(media, chunk_size=1024))

    assert all(len(chunk) <= 1024 for chunk in chunks)
    assert sum(len(chunk) for chunk in chunks) == media.stat().st_size
    assert len(chunks) > 1


# ── Refusals ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("", "no path"),
        ("clip.mp4", "relative path"),
        ("/nope/missing.mp4", "missing file"),
    ],
)
def test_stream_refuses_anything_that_is_not_a_real_media_file(tmp_path: Path, path: str, reason: str) -> None:
    response = _client(tmp_path).get("/api/media/stream", params={"path": path})

    assert response.status_code in (400, 404), f"{reason} answered {response.status_code}"
    assert "detail" in response.json()


def test_stream_refuses_a_non_media_extension_and_a_directory(tmp_path: Path) -> None:
    note = tmp_path / "notes.txt"
    note.write_text("not media", encoding="utf-8")
    directory = tmp_path / "folder.mp4"
    directory.mkdir()
    client = _client(tmp_path)

    for target in (note, directory):
        response = client.get("/api/media/stream", params={"path": str(target)})
        assert response.status_code == 400, f"{target} answered {response.status_code}"
        assert "detail" in response.json()


# ── Thumbnails ────────────────────────────────────────────────────────────


def test_thumb_404s_when_no_thumbnail_can_be_generated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing ffmpeg degrades the preview; it never becomes an error page."""
    monkeypatch.setattr(media_preview, "resolve_ffmpeg_path", lambda: None)
    monkeypatch.setattr(media_preview.shutil, "which", lambda _name: None)
    media = _write_media(tmp_path)

    response = _client(tmp_path).get("/api/media/thumb", params={"path": str(media)})

    assert response.status_code == 404
    assert "stream the original" in response.json()["detail"].lower()


def test_thumb_generates_once_then_serves_the_cached_jpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = _write_media(tmp_path, size=4096)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003, ANN202
        calls.append(list(command))
        Path(command[-1]).write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF jpeg-ish")
        return type("Done", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

    monkeypatch.setattr(media_preview.subprocess, "run", fake_run)
    client = _client(tmp_path)

    first = client.get("/api/media/thumb", params={"path": str(media)})
    second = client.get("/api/media/thumb", params={"path": str(media)})

    assert first.status_code == 200
    assert first.headers["content-type"] == "image/jpeg"
    assert first.content.startswith(b"\xff\xd8")
    assert second.status_code == 200
    assert len(calls) == 1, "the second request should have been a cache hit"
    # One frame, decoded from an input seek — not a walk of the whole video.
    assert "-frames:v" in calls[0]
    assert calls[0].index("-ss") < calls[0].index("-i")


def test_thumb_cache_key_changes_when_the_source_changes(tmp_path: Path) -> None:
    media = _write_media(tmp_path, size=1024)
    first = media_preview.thumbnail_cache_key(media, 640, media.stat())
    media.write_bytes(b"\x01" * 4096)
    second = media_preview.thumbnail_cache_key(media, 640, media.stat())

    assert first != second, "a modified source must not reuse the old frame"


# ── Path validation ───────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="symlink permissions differ on Windows CI")
def test_path_validation_resolves_symlinks_and_rejects_directories(tmp_path: Path) -> None:
    media = _write_media(tmp_path)
    link = tmp_path / "link.mp4"
    link.symlink_to(media)

    assert media_preview.resolve_media_path(link) == media.resolve()
    with pytest.raises(media_preview.MediaPathError):
        media_preview.resolve_media_path(tmp_path)
    with pytest.raises(media_preview.MediaPathError):
        media_preview.resolve_media_path("")


def test_range_parsing_handles_suffix_and_open_ranges() -> None:
    assert media_preview.parse_byte_range(None, 100) is None
    assert media_preview.parse_byte_range("bytes=0-9", 100) == (0, 9)
    assert media_preview.parse_byte_range("bytes=90-", 100) == (90, 99)
    assert media_preview.parse_byte_range("bytes=-10", 100) == (90, 99)
    assert media_preview.parse_byte_range("bytes=0-500", 100) == (0, 99)
    assert media_preview.parse_byte_range("bytes=0-9,20-29", 100) is None
    with pytest.raises(ValueError):
        media_preview.parse_byte_range("bytes=500-", 100)
