"""Media ingest: accept a file the engine host does not already have.

Every other engine surface (``/api/post``, MCP ``xpst_post``, the CLI) takes
``media_paths`` that resolve on the engine host. That is correct for a desktop
workflow and impossible for a phone: the bytes live in the camera roll. This
module is the one place that turns an uploaded byte stream into a *staged local
file* with honest metadata, so the rest of the engine keeps its existing
path-based contract.

Rules this module enforces (docs/MVP-CUT.md §1 M0):

* **Size cap before anything else.** A stream is truncated at
  :func:`max_upload_bytes` (default 512 MiB, ``XPST_MAX_UPLOAD_BYTES``) and the
  partial file is removed — an over-limit upload never reaches ffprobe and never
  half-lands in the staging directory.
* **Container sniff from the bytes, never the filename.** A phone sends
  ``video.mp4`` for an MOV and ``.mov`` for an MP4; the extension is a claim,
  the magic bytes are evidence. An unrecognised container is refused with a
  typed code instead of being handed to a probe that would fail obscurely.
* **Content-addressed ids.** ``media_id`` is derived from the SHA-256 of the
  bytes, so re-uploading the same file is idempotent by construction and the
  digest the plan later freezes is the digest of what was actually received.
* **Idempotency keys are honoured, conflicts are refused.** Replaying a key with
  the same bytes returns the original record; replaying it with different bytes
  is a conflict, not a silent overwrite.
* **Probing degrades honestly.** A missing/failed ffprobe returns
  ``probed: false`` plus the reason; it never fabricates a duration, and it never
  rejects a file that might be fine.

Nothing here touches the network, the state file or the scheduler queue.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO

from xpst.media.image_header import HEADER_READABLE_SUFFIXES, read_image_dimensions
from xpst.utils.atomic import write_text_atomic

#: Default per-file cap for a phone upload. 512 MiB is ~10 minutes of 1080p
#: high-bitrate video; the phone clients cap the picker far lower in practice.
DEFAULT_MAX_UPLOAD_BYTES = 512 * 1024 * 1024

#: Environment override for the cap (bytes). A malformed value is ignored.
MAX_UPLOAD_BYTES_ENV = "XPST_MAX_UPLOAD_BYTES"

#: Staging directory, relative to the config dir.
STAGING_DIRNAME = "media/uploads"

#: Index file (idempotency keys + records), relative to the staging dir.
INDEX_FILENAME = "uploads.json"

#: Read this many bytes to identify a container.
SNIFF_BYTES = 64

#: Read/write chunk for streaming an upload to disk.
CHUNK_BYTES = 1024 * 1024

#: Typed failure codes. These are the strings the API/UI render.
ERROR_TOO_LARGE = "MEDIA_TOO_LARGE"
ERROR_EMPTY = "MEDIA_EMPTY"
ERROR_UNSUPPORTED_CONTAINER = "MEDIA_UNSUPPORTED_CONTAINER"
ERROR_IDEMPOTENCY_CONFLICT = "MEDIA_IDEMPOTENCY_CONFLICT"
ERROR_STAGE_FAILED = "MEDIA_STAGE_FAILED"
ERROR_UNKNOWN_MEDIA = "MEDIA_UNKNOWN"

#: Video containers xPST can hand to a destination. ``kind`` is the engine's
#: modality vocabulary (video/image); the container is what the bytes are.
_VIDEO_BRANDS: dict[bytes, str] = {
    b"isom": "mp4",
    b"iso2": "mp4",
    b"iso4": "mp4",
    b"iso5": "mp4",
    b"iso6": "mp4",
    b"mp41": "mp4",
    b"mp42": "mp4",
    b"m4v": "m4v",
    b"avc1": "mp4",
    b"dash": "mp4",
    b"M4V ": "m4v",
    b"qt  ": "mov",
    b"3gp4": "3gp",
    b"3gp5": "3gp",
    b"3g2a": "3gp",
}

_MIME_BY_CONTAINER: dict[str, str] = {
    "mp4": "video/mp4",
    "m4v": "video/x-m4v",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
    "avi": "video/x-msvideo",
    "ts": "video/mp2t",
    "3gp": "video/3gpp",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}

_CONTAINER_EXTENSION: dict[str, str] = {
    "mp4": ".mp4",
    "m4v": ".m4v",
    "mov": ".mov",
    "webm": ".webm",
    "mkv": ".mkv",
    "avi": ".avi",
    "ts": ".ts",
    "3gp": ".3gp",
    "jpeg": ".jpg",
    "png": ".png",
    "gif": ".gif",
    "webp": ".webp",
}

_IMAGE_CONTAINERS = frozenset({"jpeg", "png", "gif", "webp"})

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


class MediaIngestError(Exception):
    """A typed ingest refusal the API can render verbatim.

    ``code`` is stable and machine-readable (see the ``ERROR_*`` constants);
    ``message`` is written for a person and is safe to show in the UI.
    """

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


def max_upload_bytes() -> int:
    """Return the effective per-file upload cap in bytes.

    ``XPST_MAX_UPLOAD_BYTES`` overrides the default; a malformed or
    non-positive value falls back to the default rather than disabling the cap.
    """
    raw = (os.environ.get(MAX_UPLOAD_BYTES_ENV) or "").strip()
    if not raw:
        return DEFAULT_MAX_UPLOAD_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_UPLOAD_BYTES
    return value if value > 0 else DEFAULT_MAX_UPLOAD_BYTES


def sniff_container(head: bytes) -> dict[str, Any] | None:
    """Identify a video/image container from its leading bytes.

    Returns ``{"container", "kind", "mime"}`` or ``None`` when the bytes are not
    a container xPST recognises. Pure function: no I/O, no probe, no guessing
    from a filename.
    """
    data = bytes(head or b"")
    if len(data) < 12:
        return None

    if data[4:8] == b"ftyp":
        brand = data[8:12]
        container = _VIDEO_BRANDS.get(brand)
        if container is None:
            # An ISO base-media brand we do not list by name is still an MP4
            # family file; refuse only when the box structure is absent (above).
            container = "mp4"
        return _container_record(container)

    if data[:4] == b"\x1aE\xdf\xa3":  # EBML
        # WebM and Matroska share the EBML header; the DocType string sits in
        # the first ~64 bytes of the header element.
        window = data[: max(SNIFF_BYTES, 64)].lower()
        return _container_record("webm" if b"webm" in window else "mkv")

    if data[:4] == b"RIFF" and data[8:12] == b"AVI ":
        return _container_record("avi")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _container_record("webp")

    if data[:3] == b"\xff\xd8\xff":
        return _container_record("jpeg")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _container_record("png")
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return _container_record("gif")

    # MPEG-TS: sync byte 0x47 at the start of a 188-byte packet.
    if data[:1] == b"\x47" and len(data) >= 1:
        if len(data) < 189 or data[188:189] == b"\x47":
            return _container_record("ts")

    return None


def _container_record(container: str) -> dict[str, Any]:
    return {
        "container": container,
        "kind": "image" if container in _IMAGE_CONTAINERS else "video",
        "mime": _MIME_BY_CONTAINER.get(container, "application/octet-stream"),
    }


def safe_filename(name: str, *, fallback: str = "upload") -> str:
    """Reduce a client-supplied filename to a safe leaf name.

    The staged path must never escape the media directory, and a phone will send
    unicode names, spaces and occasionally a full path.
    """
    leaf = Path(str(name or "")).name
    leaf = _SAFE_NAME.sub("_", leaf).strip(" ._")
    if not leaf:
        leaf = fallback
    return leaf[:120]


def staging_root(config_dir: str | Path) -> Path:
    """Return the upload staging directory for a config dir (not created)."""
    return Path(config_dir).expanduser() / STAGING_DIRNAME


def _index_path(config_dir: str | Path) -> Path:
    return staging_root(config_dir) / INDEX_FILENAME


def _load_index(config_dir: str | Path) -> dict[str, Any]:
    """Read the upload index, tolerating a missing/corrupt file.

    A corrupt index must not make every future upload fail: the staged files are
    still on disk and the next write rewrites the index.
    """
    path = _index_path(config_dir)
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return {"version": 1, "media": {}, "idempotency": {}}
    if not isinstance(data, dict):
        return {"version": 1, "media": {}, "idempotency": {}}
    data.setdefault("version", 1)
    if not isinstance(data.get("media"), dict):
        data["media"] = {}
    if not isinstance(data.get("idempotency"), dict):
        data["idempotency"] = {}
    return data


def _save_index(config_dir: str | Path, index: dict[str, Any]) -> None:
    root = staging_root(config_dir)
    root.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        _index_path(config_dir),
        json.dumps(index, indent=2, sort_keys=True) + "\n",
        mode=0o600,
    )


def _probe(path: Path, container: str, kind: str) -> dict[str, Any]:
    """Probe a staged file without ever raising.

    Returns the metadata a compose screen needs (duration, dimensions, codec,
    audio) plus an explicit ``probed`` flag and the reason when it is false.
    """
    result: dict[str, Any] = {
        "probed": False,
        "error": None,
        "container": container,
        "kind": kind,
        "duration_s": None,
        "width": None,
        "height": None,
        "codec": None,
        "fps": None,
        "bitrate": None,
        "has_audio": None,
    }
    if kind == "image":
        if container in {item.lstrip(".") for item in HEADER_READABLE_SUFFIXES}:
            try:
                dimensions = read_image_dimensions(path)
            except Exception as exc:  # noqa: BLE001 - a probe never rejects a file
                result["error"] = f"image header unreadable: {exc}"[:200]
                return result
            if dimensions:
                result["width"], result["height"] = dimensions
                result["probed"] = True
            else:
                result["error"] = "image header did not yield dimensions"
            return result
        result["error"] = f"no header reader for {container}"
        return result

    try:
        from xpst.utils.video import get_video_info_standalone

        info = get_video_info_standalone(path)
    except Exception as exc:  # noqa: BLE001 - a missing ffprobe is not a bad file
        result["error"] = str(exc)[:200] or exc.__class__.__name__
        return result

    try:
        streams = [item for item in (info.get("streams") or []) if item.get("codec_type") == "video"]
        stream = streams[0] if streams else {}
        fmt = info.get("format") or {}
        duration = stream.get("duration") or fmt.get("duration")
        result["duration_s"] = round(float(duration), 3) if duration else None
        result["width"] = int(stream["width"]) if stream.get("width") else None
        result["height"] = int(stream["height"]) if stream.get("height") else None
        result["codec"] = stream.get("codec_name")
        rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
        if rate and "/" in str(rate):
            numerator, _, denominator = str(rate).partition("/")
            try:
                if float(denominator):
                    result["fps"] = round(float(numerator) / float(denominator), 3)
            except ValueError:
                result["fps"] = None
        if fmt.get("bit_rate"):
            result["bitrate"] = int(fmt["bit_rate"])
        result["has_audio"] = any(item.get("codec_type") == "audio" for item in info.get("streams") or [])
        result["probed"] = bool(stream)
        if not stream:
            result["error"] = "no video stream found"
    except Exception as exc:  # noqa: BLE001 - malformed probe output
        result["error"] = f"probe output unreadable: {exc}"[:200]
    return result


def _write_stream(
    stream: BinaryIO,
    target: Path,
    *,
    cap: int,
) -> tuple[int, str, bytes]:
    """Stream ``stream`` to ``target``; return (bytes_written, sha256, head).

    The cap is enforced while writing, so an oversized upload costs disk only up
    to the cap. The partial file is removed before the typed error is raised.
    """
    digest = hashlib.sha256()
    head = b""
    written = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("wb") as handle:
            while True:
                chunk = stream.read(CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > cap:
                    raise MediaIngestError(
                        ERROR_TOO_LARGE,
                        (
                            f"That file is larger than the {cap // (1024 * 1024)} MiB upload cap. "
                            "Trim it, or raise XPST_MAX_UPLOAD_BYTES on the engine."
                        ),
                        status_code=413,
                    )
                if len(head) < SNIFF_BYTES:
                    head += chunk[: SNIFF_BYTES - len(head)]
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    if written == 0:
        target.unlink(missing_ok=True)
        raise MediaIngestError(ERROR_EMPTY, "That upload was empty (0 bytes).", status_code=400)
    return written, digest.hexdigest(), head


def stage_upload(
    config_dir: str | Path,
    *,
    filename: str,
    stream: BinaryIO,
    idempotency_key: str = "",
    label: str = "",
) -> dict[str, Any]:
    """Stage one uploaded file and return its media record.

    Args:
        config_dir: engine config dir; files land under ``media/uploads``.
        filename: client-supplied name (sanitised, never trusted for type).
        stream: binary file-like object positioned at the start.
        idempotency_key: optional caller key. A replay returns the original
            record; the same key with different bytes raises a conflict.
        label: optional human label recorded with the media.

    Raises:
        MediaIngestError: typed refusal (too large, empty, unknown container,
            idempotency conflict, staging failure).
    """
    root = staging_root(config_dir)
    index = _load_index(config_dir)
    key = str(idempotency_key or "").strip()

    incoming = root / f".incoming-{uuid.uuid4().hex}"
    try:
        written, digest, head = _write_stream(stream, incoming, cap=max_upload_bytes())
    except MediaIngestError:
        raise
    except OSError as exc:
        incoming.unlink(missing_ok=True)
        raise MediaIngestError(
            ERROR_STAGE_FAILED,
            f"The file could not be written to {root}: {exc}"[:300],
            status_code=500,
        ) from exc

    sniffed = sniff_container(head)
    if sniffed is None:
        incoming.unlink(missing_ok=True)
        raise MediaIngestError(
            ERROR_UNSUPPORTED_CONTAINER,
            (
                "That file is not a video or image container xPST recognises "
                "(expected MP4/MOV/WebM/MKV/AVI/TS/3GP or JPEG/PNG/GIF/WebP). "
                "The file extension is not used to decide this."
            ),
            status_code=415,
        )

    media_id = f"med_{digest[:16]}"

    if key:
        existing_id = index["idempotency"].get(key)
        if existing_id:
            existing = index["media"].get(existing_id)
            incoming.unlink(missing_ok=True)
            if not existing:
                # The index names a record that is gone: forget the key and
                # continue with this upload rather than return a ghost.
                index["idempotency"].pop(key, None)
            elif existing.get("sha256") != digest:
                raise MediaIngestError(
                    ERROR_IDEMPOTENCY_CONFLICT,
                    (
                        "That idempotency key was already used for a different file. "
                        "Use a new key for a new upload."
                    ),
                    status_code=409,
                )
            else:
                return {**existing, "replayed": True}

    record_path = root / media_id
    stored_name = safe_filename(filename, fallback=f"upload{_CONTAINER_EXTENSION.get(sniffed['container'], '')}")
    if not Path(stored_name).suffix:
        stored_name += _CONTAINER_EXTENSION.get(sniffed["container"], "")
    target = record_path / stored_name

    existing_record = index["media"].get(media_id)
    if existing_record and Path(str(existing_record.get("path") or "")).is_file():
        # Content-addressed: the identical bytes are already staged. Keep the
        # existing file (its probe result is already recorded) and just bind the
        # idempotency key to it.
        incoming.unlink(missing_ok=True)
        record = {**existing_record, "replayed": True}
        if key:
            index["idempotency"][key] = media_id
            _save_index(config_dir, index)
        return record

    try:
        if record_path.exists():
            shutil.rmtree(record_path, ignore_errors=True)
        record_path.mkdir(parents=True, exist_ok=True)
        os.replace(incoming, target)
    except OSError as exc:
        incoming.unlink(missing_ok=True)
        raise MediaIngestError(
            ERROR_STAGE_FAILED,
            f"The upload could not be moved into {record_path}: {exc}"[:300],
            status_code=500,
        ) from exc

    probe = _probe(target, sniffed["container"], sniffed["kind"])
    record = {
        "media_id": media_id,
        "filename": stored_name,
        "label": str(label or "")[:80],
        "path": str(target),
        "bytes": written,
        "sha256": digest,
        "kind": sniffed["kind"],
        "container": sniffed["container"],
        "mime": sniffed["mime"],
        "staged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "probe": probe,
        "probed": bool(probe.get("probed")),
        "probe_error": probe.get("error"),
        "idempotency_key": key or None,
        "replayed": False,
    }
    index["media"][media_id] = record
    if key:
        index["idempotency"][key] = media_id
    _save_index(config_dir, index)
    return record


def get_media(config_dir: str | Path, media_id: str) -> dict[str, Any] | None:
    """Return a staged media record, or ``None`` when it is unknown/removed."""
    record = _load_index(config_dir)["media"].get(str(media_id or "").strip())
    if not record:
        return None
    if not Path(str(record.get("path") or "")).is_file():
        return None
    return record


def require_media(config_dir: str | Path, media_id: str) -> dict[str, Any]:
    """Return a staged media record or raise :class:`MediaIngestError`."""
    record = get_media(config_dir, media_id)
    if record is None:
        raise MediaIngestError(
            ERROR_UNKNOWN_MEDIA,
            (
                "That media id is not staged on this engine (it may have expired, "
                "or the upload never completed). Upload the file again."
            ),
            status_code=404,
        )
    return record


def resolve_media_paths(config_dir: str | Path, media_ids: list[str]) -> list[str]:
    """Resolve staged media ids to absolute host paths, in order.

    This is how a phone-originated id reaches the path-based engine contract:
    the plan freezes the *resolved paths and digests*, so a later plan cannot
    silently point at a file that changed under it.
    """
    return [require_media(config_dir, media_id)["path"] for media_id in media_ids]


__all__ = [
    "DEFAULT_MAX_UPLOAD_BYTES",
    "ERROR_EMPTY",
    "ERROR_IDEMPOTENCY_CONFLICT",
    "ERROR_STAGE_FAILED",
    "ERROR_TOO_LARGE",
    "ERROR_UNKNOWN_MEDIA",
    "ERROR_UNSUPPORTED_CONTAINER",
    "MediaIngestError",
    "get_media",
    "max_upload_bytes",
    "require_media",
    "resolve_media_paths",
    "safe_filename",
    "sniff_container",
    "stage_upload",
    "staging_root",
]
