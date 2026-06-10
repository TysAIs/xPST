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


def _download(url: str) -> Path:
    import yt_dlp  # core dep
    out_dir = Path(tempfile.mkdtemp(prefix="xpst_kb_"))
    opts = {"outtmpl": str(out_dir / "%(id)s.%(ext)s"), "quiet": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info))
