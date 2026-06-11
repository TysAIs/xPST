"""Resolve a source string (local path or URL) to a local media file.
yt-dlp is a core xPST dependency, so URL download adds no new dependency."""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path


def source_id(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


def _is_url(source: str) -> bool:
    return source.startswith(("http://", "https://"))


def resolve_source(source: str) -> Path:
    if _is_url(source):
        return _download(source)
    p = Path(source).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"Source not found: {source}")
    return p


def content_fingerprint(path: Path) -> str:
    """Fingerprint of the media BYTES (size + first/last 64KB), independent
    of filename or source URL — the dedup identity for G33."""
    h = hashlib.sha256()
    size = path.stat().st_size
    h.update(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(65536))
        if size > 131072:
            f.seek(-65536, 2)
            h.update(f.read(65536))
    return "content:" + h.hexdigest()[:16]


def _download(url: str) -> Path:
    import shutil

    import yt_dlp  # core dep
    out_dir = Path(tempfile.mkdtemp(prefix="xpst_kb_"))
    opts = {"outtmpl": str(out_dir / "%(id)s.%(ext)s"), "quiet": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return Path(ydl.prepare_filename(info))
    except Exception:
        # Failed download must not leak its temp dir.
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
