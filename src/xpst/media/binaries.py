"""Fetch-on-first-use media helper binaries (ffmpeg / ffprobe).

xPST used to ship ffmpeg + ffprobe *inside* the desktop bundle: ~87 MB of a
192 MB app. That is why the app was heavy and why the release lane kept going
red on a flaky mirror. The bundle now carries no ffmpeg, and the binaries are
resolved at runtime in a strict order:

    1. ``XPST_FFMPEG_PATH`` / ``XPST_FFPROBE_PATH``  (explicit user override)
    2. a system install (PATH, then well-known locations probed directly)
    3. a previously fetched copy under the media bin dir (``~/.xpst/bin``)

A machine that already has ffmpeg downloads nothing. A machine that does not
gets **one** resumable, checksum-verified download into ``~/.xpst/bin``
(``XPST_MEDIA_BIN_DIR`` overrides the directory, ``XPST_MEDIA_AUTO_FETCH=0``
disables the automatic fetch).

Downloads are pinned to an immutable upstream release tag and every payload is
verified against a pinned SHA-256 before it is installed. A partial transfer is
resumed (HTTP range) rather than restarted, and a corrupt payload is rejected
outright — never installed.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from xpst.utils.platform import get_config_dir, get_ffmpeg_name, get_ffprobe_name

#: The helper binaries xPST needs at runtime.
MEDIA_BINARIES: tuple[str, ...] = ("ffmpeg", "ffprobe")

#: Immutable upstream release used for the static builds. Both the compressed
#: (``.gz``, ~27 MB total on macOS arm64) and the raw artifacts are published
#: here with a per-asset SHA-256, which is what we pin below.
FFSTATIC_TAG = "b6.1.1"
FFSTATIC_BASE = (
    f"https://github.com/eugeneware/ffmpeg-static/releases/download/{FFSTATIC_TAG}"
)

#: platform key -> upstream artifact suffix.
_ARTIFACT_SUFFIX: dict[str, str] = {
    "macos-arm64": "darwin-arm64",
    "macos-x64": "darwin-x64",
    "linux-x64": "linux-x64",
    "linux-arm64": "linux-arm64",
    "win-x64": "win32-x64",
}

#: (platform, binary) -> (sha256 of "<binary>-<suffix>.gz", sha256 of the raw
#: binary). Copied from the release API for tag b6.1.1 (immutable) and verified
#: locally on 2026-09-15 by downloading both macOS arm64 artifacts, checking the
#: compressed digest, decompressing, and checking the raw digest again.
_DIGESTS: dict[tuple[str, str], tuple[str, str]] = {
    ("macos-arm64", "ffmpeg"): (
        "8923876afa8db5585022d7860ec7e589af192f441c56793971276d450ed3bbfa",
        "a90e3db6a3fd35f6074b013f948b1aa45b31c6375489d39e572bea3f18336584",
    ),
    ("macos-arm64", "ffprobe"): (
        "d986a8ec7b030899fe66a8a288ed809a3543338705a3ce178cfb85869c5d80be",
        "bb2db6f5d8cef919da12fbf592119a987202a8c060a886f3cab091f9cab90b64",
    ),
    ("macos-x64", "ffmpeg"): (
        "929b375c1182d956c51f7ac25e0b2b0411fb01f6f407aa15c9758efeb4242106",
        "ebdddc936f61e14049a2d4b549a412b8a40deeff6540e58a9f2a2da9e6b18894",
    ),
    ("macos-x64", "ffprobe"): (
        "d4da574d6e2e197bd259b47d69cf262df9e312af24ad960444f6d806d3d4c186",
        "fa3add0ce901f7241abe0dfc0155d958fc834aca3f8ce61f87cc712ae669c1e0",
    ),
    ("linux-x64", "ffmpeg"): (
        "bfe8a8fc511530457b528c48d77b5737527b504a3797a9bc4866aeca69c2dffa",
        "e7e7fb30477f717e6f55f9180a70386c62677ef8a4d4d1a5d948f4098aa3eb99",
    ),
    ("linux-x64", "ffprobe"): (
        "25d9b6ccb05e3d9de9e04e31e2506d8dd7f9f0418981965ac6df12e8d3afd067",
        "4f231a1960d83e403d08f7971e271707bec278a9ae18e21b8b5b03186668450d",
    ),
    ("linux-arm64", "ffmpeg"): (
        "754a678672298bc68156adff58aa7385a592c2b30b1d0ae8750c45c915c4bac0",
        "6bb182d0d75d23028db82e9e4f723ca69b853d055698486e6984ddb2c06fb8ce",
    ),
    ("linux-arm64", "ffprobe"): (
        "2ab6aba60ee84412dff9188720703376cb4e7aaf7e0b5e43aa8249f2acae5bf8",
        "d17ae9b4c297d48e2521ba14e417bb0537c6ff77c584cdbcd6bb0d8d0307a2e8",
    ),
    ("win-x64", "ffmpeg"): (
        "8883a3dffbd0a16cf4ef95206ea05283f78908dbfb118f73c83f4951dcc06d77",
        "04e1307997530f9cf2fe35cba2ca7e8875ca91da02f89d6c7243df819c94ad00",
    ),
    ("win-x64", "ffprobe"): (
        "f309e6223ad89d2fe54bccd420a7709b66fd27540674e92309578ed491a43c8d",
        "3a7e2dc003dc2cd1472827e4c7c4f056ae1ae0ae7c5bbc580c99b49827351ba4",
    ),
}

#: Minimum plausible size (bytes) for a real static ffmpeg/ffprobe build. A
#: shorter payload is a truncated download, whatever its checksum says.
_MIN_BINARY_BYTES = 5 * 1024 * 1024

#: Executable magic numbers: Mach-O (32/64, both endiannesses), ELF, PE.
_MAGIC = (
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xfe\xed\xfa\xce",
    b"\xca\xfe\xba\xbe",
    b"\x7fELF",
    b"MZ",
)

DEFAULT_ATTEMPTS = 3
DEFAULT_TIMEOUT = 60.0
DEFAULT_RETRY_DELAY = 2.0

Logger = Callable[[str], None]


class MediaFetchError(RuntimeError):
    """Raised when no source produced a verified media binary."""

    def __init__(self, binary: str, attempted: Sequence[str], reason: str) -> None:
        self.binary = binary
        self.attempted = list(attempted)
        self.reason = reason
        super().__init__(
            f"Could not download {binary}: {reason}. "
            f"Tried {len(self.attempted)} source(s): {', '.join(self.attempted) or 'none'}. "
            f"Install {binary} with your package manager (macOS: `brew install ffmpeg`) or "
            f"point XPST_{binary.upper()}_PATH at an existing binary."
        )


@dataclass(frozen=True)
class MediaSource:
    """One downloadable artifact with its pinned digest."""

    binary: str
    url: str
    sha256: str
    #: "gz" (gzipped binary) or "raw" (bare binary).
    kind: str

    @property
    def label(self) -> str:
        return self.url


class _Response(Protocol):
    """The subset of ``http.client.HTTPResponse`` the downloader uses."""

    status: int
    headers: Any

    def read(self, amt: int = ...) -> bytes: ...

    def close(self) -> None: ...


#: Signature of the HTTP opener seam (returns a response-like object with
#: ``status``, ``headers``, ``read()`` and ``close()``).
Opener = Callable[[str, dict[str, str], float], Any]


def _emit(log: Logger | None, message: str) -> None:
    if log is not None:
        log(message)


def platform_key() -> str:
    """Return the media-platform key for this host (``XPST_MEDIA_PLATFORM`` wins)."""
    override = os.environ.get("XPST_MEDIA_PLATFORM", "").strip()
    if override:
        return override
    if sys.platform == "darwin":
        return "macos-arm64" if _machine_arm() else "macos-x64"
    if sys.platform == "win32":
        return "win-x64"
    return "linux-arm64" if _machine_arm() else "linux-x64"


def _machine_arm() -> bool:
    import platform as _platform

    machine = (_platform.machine() or "").lower()
    return machine in {"arm64", "aarch64"}


def media_bin_dir() -> Path:
    """Directory holding fetch-on-first-use binaries (``XPST_MEDIA_BIN_DIR`` wins)."""
    override = os.environ.get("XPST_MEDIA_BIN_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return get_config_dir() / "bin"


def media_binary_name(binary: str) -> str:
    """Platform-specific file name for ``binary``."""
    if binary == "ffmpeg":
        return get_ffmpeg_name()
    if binary == "ffprobe":
        return get_ffprobe_name()
    return f"{binary}.exe" if sys.platform == "win32" else binary


def media_binary_sources(binary: str, platform: str | None = None) -> tuple[MediaSource, ...]:
    """Pinned sources for ``binary`` on ``platform`` (compressed first)."""
    key = platform or platform_key()
    suffix = _ARTIFACT_SUFFIX.get(key)
    digests = _DIGESTS.get((key, binary))
    if suffix is None or digests is None:
        return ()
    gz_sha, raw_sha = digests
    return (
        MediaSource(binary=binary, url=f"{FFSTATIC_BASE}/{binary}-{suffix}.gz", sha256=gz_sha, kind="gz"),
        MediaSource(binary=binary, url=f"{FFSTATIC_BASE}/{binary}-{suffix}", sha256=raw_sha, kind="raw"),
    )


def sha256_file(path: Path) -> str:
    """SHA-256 of a file, streamed (no full-file read into memory)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_opener(url: str, headers: dict[str, str], timeout: float) -> _Response:
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 - pinned https URLs
    return urllib.request.urlopen(request, timeout=timeout)  # nosec B310 - pinned https release URL; sha256 verified by caller  # type: ignore[return-value]


def _content_length(response: _Response) -> int | None:
    headers = response.headers
    raw = headers.get("Content-Length") if hasattr(headers, "get") else None
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _status(response: _Response) -> int:
    status = getattr(response, "status", None)
    if status is None:  # pragma: no cover - defensive for exotic responses
        status = getattr(response, "code", 0)
    return int(status or 0)


def download_to(
    url: str,
    dest: Path,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    timeout: float = DEFAULT_TIMEOUT,
    retry_delay: float = DEFAULT_RETRY_DELAY,
    opener: Opener | None = None,
    log: Logger | None = None,
) -> Path:
    """Download ``url`` to ``dest``, resuming a partial file when possible.

    Resilience: HTTP range resume, bounded retries with backoff, and a hard
    error when the transferred byte count does not match what the server
    advertised. The caller verifies the checksum afterwards.
    """
    open_url = opener or _default_opener
    last_error = "no attempt made"
    for attempt in range(1, max(1, attempts) + 1):
        offset = dest.stat().st_size if dest.exists() else 0
        headers = {"User-Agent": "xpst-media-fetch", "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            response = open_url(url, headers, timeout)
            try:
                status = _status(response)
                if status == 416:
                    # Range not satisfiable: the file is already complete.
                    return dest
                if status not in (200, 206):
                    raise OSError(f"HTTP {status}")
                append = status == 206 and offset > 0
                expected = _content_length(response)
                mode = "ab" if append else "wb"
                written = offset if append else 0
                with open(dest, mode) as handle:
                    while True:
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        handle.write(chunk)
                        written += len(chunk)
                if expected is not None and written != (offset + expected if append else expected):
                    raise OSError(
                        f"partial transfer: got {written} bytes, expected "
                        f"{offset + expected if append else expected}"
                    )
                return dest
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except (OSError, urllib.error.URLError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            _emit(log, f"media fetch: attempt {attempt}/{attempts} failed ({last_error}) {url}")
            if attempt >= attempts:
                break
            time.sleep(retry_delay * attempt)
    raise OSError(last_error)


def _looks_executable(path: Path) -> tuple[bool, str]:
    """Cheap structural check: right magic, plausible size."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return False, f"missing ({exc})"
    if size < _MIN_BINARY_BYTES:
        return False, f"only {size} bytes"
    with open(path, "rb") as handle:
        head = handle.read(4)
    if not any(head.startswith(magic) for magic in _MAGIC):
        return False, f"unexpected file magic {head!r}"
    return True, ""


def _smoke_check(path: Path) -> tuple[bool, str]:
    """Run ``<binary> -version`` so a wrong-architecture build is caught."""
    try:
        proc = subprocess.run(  # noqa: S603 - our own verified download
            [str(path), "-version"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return False, f"`-version` exited {proc.returncode}"
    return True, ""


def _install_gz(source_path: Path, dest: Path) -> None:
    """Decompress ``source_path`` into ``dest`` atomically."""
    tmp = dest.with_name(dest.name + ".install")
    with gzip.open(source_path, "rb") as gz_in, open(tmp, "wb") as out:
        shutil.copyfileobj(gz_in, out, 1024 * 1024)
    os.chmod(tmp, 0o755)  # nosec B103 - executable installer binary, world-executable by design
    os.replace(tmp, dest)


def _install_raw(source_path: Path, dest: Path) -> None:
    tmp = dest.with_name(dest.name + ".install")
    shutil.copyfile(source_path, tmp)
    os.chmod(tmp, 0o755)  # nosec B103 - executable installer binary, world-executable by design
    os.replace(tmp, dest)


def _write_receipt(dest: Path, source: MediaSource, digest: str) -> None:
    receipt = dest.with_name(dest.name + ".receipt.json")
    payload = {
        "binary": source.binary,
        "source": source.url,
        "sha256": digest,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "platform": platform_key(),
    }
    try:
        receipt.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:  # pragma: no cover - receipt is advisory, never fatal
        pass


def fetch_media_binary(
    binary: str,
    *,
    dest_dir: Path | None = None,
    platform: str | None = None,
    attempts: int = DEFAULT_ATTEMPTS,
    timeout: float = DEFAULT_TIMEOUT,
    retry_delay: float = DEFAULT_RETRY_DELAY,
    opener: Opener | None = None,
    log: Logger | None = None,
    smoke: bool = True,
    sources: Sequence[MediaSource] | None = None,
) -> Path:
    """Download, verify and install ``binary``; returns the installed path.

    Raises :class:`MediaFetchError` when every pinned source fails for any
    reason (network, checksum mismatch, corrupt payload, wrong architecture).
    """
    name = media_binary_name(binary)
    target_dir = dest_dir if dest_dir is not None else media_bin_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / name
    attempted: list[str] = []

    candidates = tuple(sources) if sources is not None else media_binary_sources(binary, platform)
    if not candidates:
        raise MediaFetchError(binary, attempted, f"no pinned download source for platform {platform or platform_key()}")

    for source in candidates:
        attempted.append(source.url)
        # One partial file per source, so a resumed transfer can never splice
        # bytes from two different URLs together. Kept between runs (that is
        # what makes the download resumable) and dropped once the source is
        # rejected or installed.
        part = target_dir / f".{name}.{hashlib.sha256(source.url.encode()).hexdigest()[:12]}.part"
        _emit(log, f"media fetch: {binary} <- {source.url}")
        try:
            download_to(
                source.url,
                part,
                attempts=attempts,
                timeout=timeout,
                retry_delay=retry_delay,
                opener=opener,
                log=log,
            )
            digest = sha256_file(part)
            if digest != source.sha256:
                raise MediaFetchError(
                    binary,
                    attempted,
                    f"checksum mismatch for {source.url} (got {digest}, expected {source.sha256})",
                )
            if source.kind == "gz":
                _install_gz(part, dest)
            else:
                _install_raw(part, dest)
            part.unlink(missing_ok=True)
        except MediaFetchError:
            part.unlink(missing_ok=True)
            _emit(log, f"media fetch: rejected {source.url} (checksum mismatch)")
            continue
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            part.unlink(missing_ok=True)
            _emit(log, f"media fetch: rejected {source.url} ({type(exc).__name__}: {exc})")
            continue

        ok, why = _looks_executable(dest)
        if ok and smoke:
            ok, why = _smoke_check(dest)
        if not ok:
            dest.unlink(missing_ok=True)
            _emit(log, f"media fetch: rejected {source.url} ({why})")
            continue
        _write_receipt(dest, source, source.sha256)
        _emit(log, f"media fetch: {binary} ready at {dest}")
        return dest

    raise MediaFetchError(
        binary,
        attempted,
        "every pinned source failed verification or was unreachable",
    )


def media_binary_status(
    names: Iterable[str] = MEDIA_BINARIES,
    *,
    dest_dir: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Report where each binary resolves from (env / system / fetched / missing)."""
    from xpst.utils.platform import resolve_ffmpeg_path, resolve_ffprobe_path

    fetched_dir = dest_dir if dest_dir is not None else media_bin_dir()
    resolvers = {"ffmpeg": resolve_ffmpeg_path, "ffprobe": resolve_ffprobe_path}
    env_vars = {"ffmpeg": "XPST_FFMPEG_PATH", "ffprobe": "XPST_FFPROBE_PATH"}

    status: dict[str, dict[str, Any]] = {}
    for binary in names:
        resolved = resolvers.get(binary, lambda: None)()
        source = "missing"
        if resolved:
            env_value = os.environ.get(env_vars.get(binary, ""), "")
            try:
                if env_value and Path(env_value).expanduser() == Path(resolved):
                    source = "env"
                elif Path(resolved).parent == fetched_dir:
                    source = "fetched"
                else:
                    source = "system"
            except OSError:  # pragma: no cover - path comparison edge case
                source = "system"
        status[binary] = {
            "name": binary,
            "ok": bool(resolved),
            "path": resolved,
            "source": source,
        }
    return status


def auto_fetch_enabled() -> bool:
    """Whether the automatic first-use download may run (``XPST_MEDIA_AUTO_FETCH``)."""
    return os.environ.get("XPST_MEDIA_AUTO_FETCH", "1").strip().lower() not in {"0", "false", "no", "off"}


def ensure_media_binaries(
    *,
    names: Iterable[str] = MEDIA_BINARIES,
    fetch: bool = True,
    dest_dir: Path | None = None,
    platform: str | None = None,
    opener: Opener | None = None,
    log: Logger | None = None,
    smoke: bool = True,
) -> dict[str, dict[str, Any]]:
    """Resolve every binary, fetching the missing ones once when asked.

    Returns the same shape as :func:`media_binary_status`, extended with an
    ``error`` key for a binary that could not be fetched. Never raises: the
    caller decides whether a missing binary is fatal.
    """
    report = media_binary_status(names, dest_dir=dest_dir)

    for binary, info in report.items():
        if info["ok"]:
            _emit(log, f"media: {binary} -> {info['path']} ({info['source']})")
            continue
        if not fetch or not auto_fetch_enabled():
            _emit(
                log,
                f"media: {binary} is missing. Install it (macOS: `brew install ffmpeg`) or run "
                f"`xpst media fetch` to download a verified static build, or set "
                f"XPST_{binary.upper()}_PATH.",
            )
            info["error"] = "missing (fetch disabled)"
            continue
        _emit(
            log,
            f"media: {binary} is not installed — downloading a verified static build once "
            f"(~20-30 MB) into {dest_dir if dest_dir is not None else media_bin_dir()} ...",
        )
        try:
            path = fetch_media_binary(
                binary, dest_dir=dest_dir, platform=platform, opener=opener, log=log, smoke=smoke
            )
        except MediaFetchError as exc:
            info["error"] = str(exc)
            _emit(log, f"media: could not fetch {binary}: {exc}")
            continue
        info.update({"ok": True, "path": str(path), "source": "fetched", "error": None})

    return report


__all__ = [
    "MEDIA_BINARIES",
    "FFSTATIC_BASE",
    "FFSTATIC_TAG",
    "MediaFetchError",
    "MediaSource",
    "auto_fetch_enabled",
    "download_to",
    "ensure_media_binaries",
    "fetch_media_binary",
    "media_bin_dir",
    "media_binary_sources",
    "media_binary_status",
    "platform_key",
    "sha256_file",
]
