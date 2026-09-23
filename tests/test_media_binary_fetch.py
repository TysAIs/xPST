"""Fetch-on-first-use media binaries: resume, checksums, rejection, ordering.

These tests replace the HTTP layer with a fake opener, so nothing here touches
the network. They pin the contract the desktop app relies on when a machine has
no ffmpeg of its own:

  * one resumable download (HTTP range resume, bounded retries with backoff);
  * every payload verified against the pinned SHA-256 before install;
  * a partial, corrupt or wrong-architecture payload is rejected, never
    installed;
  * if a source fails, the next pinned source is tried, and a total failure is
    a loud, actionable error naming every URL attempted.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from xpst.media import binaries

REAL_BINARY = b"\xcf\xfa\xed\xfe" + b"ffmpeg-payload" * 8  # Mach-O magic + body


class FakeResponse(io.BytesIO):
    """Minimal HTTP response: status, headers, read(), close()."""

    def __init__(self, body: bytes, status: int = 200, headers: dict[str, str] | None = None) -> None:
        super().__init__(body)
        self.status = status
        self.headers = headers or {}

    def close(self) -> None:  # pragma: no cover - exercised via download_to
        super().close()


class FakeServer:
    """Serves ``payload`` with real range semantics (or a scripted failure)."""

    def __init__(
        self,
        payload: bytes,
        *,
        advertise_extra: int = 0,
        ignore_range: bool = False,
        fail_times: int = 0,
    ) -> None:
        self.payload = payload
        self.advertise_extra = advertise_extra
        self.ignore_range = ignore_range
        self.fail_times = fail_times
        self.requests: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
        self.requests.append((url, dict(headers)))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise OSError("connection reset")
        offset = 0
        rng = headers.get("Range")
        if rng and not self.ignore_range:
            offset = int(rng.split("=")[1].split("-")[0])
            if offset >= len(self.payload):
                return FakeResponse(b"", status=416, headers={})
        body = self.payload[offset:]
        status = 206 if offset and not self.ignore_range else 200
        return FakeResponse(
            body,
            status=status,
            headers={"Content-Length": str(len(body) + self.advertise_extra)},
        )


@pytest.fixture(autouse=True)
def _tiny_size_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real builds are >5MB; the tests use tiny stand-ins."""
    monkeypatch.setattr(binaries, "_MIN_BINARY_BYTES", 8)


@pytest.fixture(autouse=True)
def _isolated_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XPST_MEDIA_BIN_DIR", str(tmp_path / "bin"))
    for var in ("XPST_FFMPEG_PATH", "XPST_FFPROBE_PATH", "XPST_MEDIA_AUTO_FETCH"):
        monkeypatch.delenv(var, raising=False)


def _source(kind: str, payload: bytes, *, url: str, binary: str = "ffmpeg") -> binaries.MediaSource:
    blob = gzip.compress(payload) if kind == "gz" else payload
    return binaries.MediaSource(binary=binary, url=url, sha256=hashlib.sha256(blob).hexdigest(), kind=kind)


def _serve(
    payload: bytes, kind: str, *, url: str | None = None, **kwargs
) -> tuple[binaries.MediaSource, FakeServer]:
    source = _source(kind, payload, url=url or f"https://example.invalid/ffmpeg.{kind}")
    blob = gzip.compress(payload) if kind == "gz" else payload
    return source, FakeServer(blob, **kwargs)


# ── happy paths ─────────────────────────────────────────────────────────────


def test_gz_source_installs_verified_binary(tmp_path: Path) -> None:
    source, server = _serve(REAL_BINARY, "gz")
    dest_dir = tmp_path / "bin"

    path = binaries.fetch_media_binary(
        "ffmpeg", dest_dir=dest_dir, sources=[source], opener=server, smoke=False
    )

    assert path == dest_dir / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    assert path.read_bytes() == REAL_BINARY
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o111, "installed binary must be executable"
    # No partial/install leftovers.
    assert not list(dest_dir.glob("*.part"))
    assert not list(dest_dir.glob("*.install"))
    receipt = dest_dir / f"{path.name}.receipt.json"
    assert receipt.is_file()
    assert source.url in receipt.read_text()


def test_raw_source_installs_binary(tmp_path: Path) -> None:
    source, server = _serve(REAL_BINARY, "raw")

    path = binaries.fetch_media_binary(
        "ffprobe", dest_dir=tmp_path / "bin", sources=[source], opener=server, smoke=False
    )

    assert path.read_bytes() == REAL_BINARY


def test_partial_transfer_is_resumed_not_restarted(tmp_path: Path) -> None:
    """A truncated first attempt resumes from the byte offset already on disk."""
    source, server = _serve(REAL_BINARY, "raw")
    dest_dir = tmp_path / "bin"
    dest_dir.mkdir(parents=True)
    part = dest_dir / (
        f".{binaries.media_binary_name('ffmpeg')}."
        f"{hashlib.sha256(source.url.encode()).hexdigest()[:12]}.part"
    )
    part.write_bytes(REAL_BINARY[:7])  # a previous run's partial file

    path = binaries.fetch_media_binary(
        "ffmpeg", dest_dir=dest_dir, sources=[source], opener=server, smoke=False
    )

    assert path.read_bytes() == REAL_BINARY
    assert server.requests, "no request was made"
    assert server.requests[0][1].get("Range") == "bytes=7-"


def test_retries_after_transient_network_failure(tmp_path: Path) -> None:
    source, server = _serve(REAL_BINARY, "gz", fail_times=2)

    path = binaries.fetch_media_binary(
        "ffmpeg", dest_dir=tmp_path / "bin", sources=[source], opener=server,
        retry_delay=0, attempts=4, smoke=False,
    )

    assert path.read_bytes() == REAL_BINARY
    assert len(server.requests) == 3


def test_server_without_range_support_restarts_cleanly(tmp_path: Path) -> None:
    """A 200 answer to a range request must overwrite, not append garbage."""
    source, server = _serve(REAL_BINARY, "raw", ignore_range=True)
    dest_dir = tmp_path / "bin"
    dest_dir.mkdir(parents=True)
    part = dest_dir / (
        f".{binaries.media_binary_name('ffmpeg')}."
        f"{hashlib.sha256(source.url.encode()).hexdigest()[:12]}.part"
    )
    part.write_bytes(b"stale-partial-bytes")

    path = binaries.fetch_media_binary(
        "ffmpeg", dest_dir=dest_dir, sources=[source], opener=server, smoke=False
    )

    assert path.read_bytes() == REAL_BINARY


# ── rejection paths ─────────────────────────────────────────────────────────


def test_checksum_mismatch_is_rejected_and_next_source_wins(tmp_path: Path) -> None:
    bad_gz, _ = _serve(REAL_BINARY, "gz")
    bad = binaries.MediaSource(binary="ffmpeg", url=bad_gz.url, sha256="0" * 64, kind="gz")
    good, good_server = _serve(REAL_BINARY, "raw")

    path = binaries.fetch_media_binary(
        "ffmpeg", dest_dir=tmp_path / "bin", sources=[bad, good],
        opener=lambda url, headers, timeout: good_server(
            url, headers, timeout
        ) if url == good.url else FakeResponse(gzip.compress(REAL_BINARY)),
        smoke=False,
    )

    assert path.read_bytes() == REAL_BINARY
    assert not list((tmp_path / "bin").glob("*.part")), "the rejected partial must be cleaned up"


def test_every_source_failing_raises_actionable_error(tmp_path: Path) -> None:
    bad, server = _serve(REAL_BINARY, "gz")
    broken = binaries.MediaSource(binary="ffmpeg", url=bad.url, sha256="f" * 64, kind="gz")

    with pytest.raises(binaries.MediaFetchError) as excinfo:
        binaries.fetch_media_binary(
            "ffmpeg", dest_dir=tmp_path / "bin", sources=[broken], opener=server, smoke=False
        )

    message = str(excinfo.value)
    assert "Could not download ffmpeg" in message
    assert broken.url in message
    assert "XPST_FFMPEG_PATH" in message
    assert not (tmp_path / "bin" / binaries.media_binary_name("ffmpeg")).exists()


def test_truncated_transfer_is_never_installed(tmp_path: Path) -> None:
    """Content-Length says more than the server delivered (and no range support)."""
    source, server = _serve(REAL_BINARY, "raw", advertise_extra=1000, ignore_range=True)

    with pytest.raises(binaries.MediaFetchError):
        binaries.fetch_media_binary(
            "ffmpeg", dest_dir=tmp_path / "bin", sources=[source], opener=server,
            attempts=2, retry_delay=0, smoke=False,
        )

    assert not (tmp_path / "bin" / binaries.media_binary_name("ffmpeg")).exists()


def test_corrupt_payload_is_rejected_and_next_source_wins(tmp_path: Path) -> None:
    payload = b"this is not an executable at all, just text" * 4
    source, server = _serve(payload, "raw", url="https://example.invalid/bad-ffmpeg")
    good, good_server = _serve(REAL_BINARY, "gz", url="https://example.invalid/good-ffmpeg.gz")

    def opener(url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
        if url == source.url:
            return server(url, headers, timeout)
        return good_server(url, headers, timeout)

    path = binaries.fetch_media_binary(
        "ffmpeg", dest_dir=tmp_path / "bin", sources=[source, good], opener=opener, smoke=False
    )

    assert path.read_bytes() == REAL_BINARY


def test_failed_smoke_check_rejects_the_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A right-magic binary that cannot run (wrong architecture) is rejected."""
    source, server = _serve(REAL_BINARY, "raw")
    good, good_server = _serve(REAL_BINARY, "gz")
    calls: list[Path] = []

    def fake_smoke(path: Path) -> tuple[bool, str]:
        calls.append(path)
        return (len(calls) > 1, "exec format error" if len(calls) == 1 else "")

    monkeypatch.setattr(binaries, "_smoke_check", fake_smoke)

    def opener(url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
        if url == source.url:
            return server(url, headers, timeout)
        return good_server(url, headers, timeout)

    path = binaries.fetch_media_binary(
        "ffmpeg", dest_dir=tmp_path / "bin", sources=[source, good], opener=opener
    )

    assert path.read_bytes() == REAL_BINARY
    assert len(calls) == 2


def test_no_pinned_source_for_platform_is_an_explicit_error(tmp_path: Path) -> None:
    with pytest.raises(binaries.MediaFetchError) as excinfo:
        binaries.fetch_media_binary(
            "ffmpeg", dest_dir=tmp_path / "bin", platform="plan9-mips",
            opener=lambda url, headers, timeout: FakeResponse(b""), smoke=False,
        )

    assert "plan9-mips" in str(excinfo.value)


# ── source table integrity ──────────────────────────────────────────────────


def test_every_platform_has_two_pinned_sources_per_binary() -> None:
    for platform in ("macos-arm64", "macos-x64", "linux-x64", "linux-arm64", "win-x64"):
        for binary in binaries.MEDIA_BINARIES:
            sources = binaries.media_binary_sources(binary, platform)
            assert len(sources) == 2, f"{platform}/{binary} must have a compressed and a raw source"
            assert [s.kind for s in sources] == ["gz", "raw"]
            for source in sources:
                assert source.url.startswith("https://")
                assert binaries.FFSTATIC_TAG in source.url, "sources must be pinned to an immutable tag"
                assert len(source.sha256) == 64 and int(source.sha256, 16) >= 0


def test_compressed_source_is_preferred_smaller_download() -> None:
    sources = binaries.media_binary_sources("ffmpeg", "macos-arm64")
    assert sources[0].url.endswith(".gz")


# ── ensure_media_binaries ───────────────────────────────────────────────────


def _explode(url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
    raise AssertionError(f"no network call expected, got {url}")


def test_no_download_when_system_ffmpeg_is_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    system_dir = tmp_path / "system"
    for name in ("ffmpeg", "ffprobe"):
        exe = system_dir / binaries.media_binary_name(name)
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(REAL_BINARY)
        exe.chmod(0o755)
    monkeypatch.setattr("xpst.utils.platform.system_media_dirs", lambda: [system_dir])
    monkeypatch.setattr("xpst.utils.platform.shutil.which", lambda name: None)

    report = binaries.ensure_media_binaries(dest_dir=tmp_path / "bin", opener=_explode)

    assert report["ffmpeg"]["ok"] and report["ffmpeg"]["source"] == "system"
    assert report["ffprobe"]["ok"] and report["ffprobe"]["source"] == "system"
    assert not (tmp_path / "bin").exists()


def test_downloads_when_nothing_is_installed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("xpst.utils.platform.system_media_dirs", lambda: [tmp_path / "empty"])
    monkeypatch.setattr("xpst.utils.platform.shutil.which", lambda name: None)
    server = FakeServer(REAL_BINARY)

    def fake_sources(binary: str, platform: str | None = None) -> tuple[binaries.MediaSource, ...]:
        return (
            binaries.MediaSource(
                binary=binary,
                url=f"https://example.invalid/{binary}.raw",
                sha256=hashlib.sha256(REAL_BINARY).hexdigest(),
                kind="raw",
            ),
        )

    monkeypatch.setattr(binaries, "media_binary_sources", fake_sources)
    lines: list[str] = []

    report = binaries.ensure_media_binaries(
        dest_dir=tmp_path / "bin",
        opener=lambda url, headers, timeout: server(url, headers, timeout),
        log=lines.append,
        smoke=False,
    )

    assert report["ffmpeg"]["ok"] and report["ffmpeg"]["source"] == "fetched"
    assert report["ffprobe"]["ok"]
    assert any("downloading a verified static build" in line for line in lines)
    assert any("ready at" in line for line in lines)


def test_auto_fetch_can_be_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("xpst.utils.platform.system_media_dirs", lambda: [tmp_path / "empty"])
    monkeypatch.setattr("xpst.utils.platform.shutil.which", lambda name: None)
    monkeypatch.setenv("XPST_MEDIA_AUTO_FETCH", "0")

    report = binaries.ensure_media_binaries(dest_dir=tmp_path / "bin", opener=_explode, smoke=False)

    assert not report["ffmpeg"]["ok"]
    assert "fetch disabled" in report["ffmpeg"]["error"]


def test_ensure_reports_fetch_failure_without_raising(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("xpst.utils.platform.system_media_dirs", lambda: [tmp_path / "empty"])
    monkeypatch.setattr("xpst.utils.platform.shutil.which", lambda name: None)

    report = binaries.ensure_media_binaries(
        names=("ffmpeg",),
        dest_dir=tmp_path / "bin",
        platform="plan9-mips",
        opener=_explode,
        smoke=False,
    )

    assert not report["ffmpeg"]["ok"]
    assert "plan9-mips" in report["ffmpeg"]["error"]


def test_status_reports_env_system_fetched_and_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    system_dir = tmp_path / "system"
    exe = system_dir / binaries.media_binary_name("ffprobe")
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(REAL_BINARY)
    exe.chmod(0o755)
    fetched_dir = tmp_path / "bin"
    fetched = fetched_dir / binaries.media_binary_name("ffmpeg")
    fetched.parent.mkdir(parents=True)
    fetched.write_bytes(REAL_BINARY)
    fetched.chmod(0o755)
    monkeypatch.setattr("xpst.utils.platform.system_media_dirs", lambda: [system_dir])
    monkeypatch.setattr("xpst.utils.platform.shutil.which", lambda name: None)

    status = binaries.media_binary_status(dest_dir=fetched_dir)

    assert status["ffmpeg"]["source"] == "fetched"
    assert status["ffprobe"]["source"] == "system"

    override = tmp_path / "override-ffmpeg"
    override.write_bytes(REAL_BINARY)
    override.chmod(0o755)
    monkeypatch.setenv("XPST_FFMPEG_PATH", str(override))
    assert binaries.media_binary_status(dest_dir=fetched_dir)["ffmpeg"]["source"] == "env"

    monkeypatch.setattr("xpst.utils.platform.system_media_dirs", lambda: [tmp_path / "empty"])
    monkeypatch.delenv("XPST_FFMPEG_PATH")
    monkeypatch.setattr("xpst.utils.platform.shutil.which", lambda name: None)
    monkeypatch.setenv("XPST_MEDIA_BIN_DIR", str(tmp_path / "nothing-here"))
    assert binaries.media_binary_status(dest_dir=tmp_path / "nothing-here")["ffmpeg"]["source"] == "missing"


def test_platform_key_is_one_of_the_pinned_platforms() -> None:
    assert binaries.platform_key() in {"macos-arm64", "macos-x64", "linux-x64", "linux-arm64", "win-x64"}


def test_download_to_reports_retry_attempts(tmp_path: Path) -> None:
    server = FakeServer(REAL_BINARY, fail_times=1)
    lines: list[str] = []

    dest = tmp_path / "blob"
    binaries.download_to(
        "https://example.invalid/blob", dest, attempts=3, retry_delay=0,
        opener=server, log=lines.append,
    )

    assert dest.read_bytes() == REAL_BINARY
    assert any("attempt 1/3 failed" in line for line in lines)
