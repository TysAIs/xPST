"""Resolve a user-supplied post reference to a platform-side post id.

Users paste either a full post URL (``https://youtube.com/shorts/RZ6i-0HM5dM``)
or a bare platform id (``RZ6i-0HM5dM``) into commands like ``xpst delete``.
This module normalizes both to the platform-side id so the delete path can
resolve it to xPST's internal record. It performs no I/O and no network calls.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

# Hosts we can safely extract a trailing id from when no pattern matches.
_KNOWN_HOSTS = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "x.com",
    "twitter.com",
    "tiktok.com",
    "instagram.com",
    "threads.net",
    "threads.com",
)

# Path segments that are NOT ids (structure words between host and the id).
_PATH_NOISE = frozenset(
    {"shorts", "watch", "embed", "live", "v", "status", "video", "reel", "reels", "p", "post"}
)


def is_post_url(ref: str) -> bool:
    """Whether ``ref`` looks like an http(s) URL."""
    if not isinstance(ref, str):
        return False
    return ref.strip().lower().startswith(("http://", "https://"))


def extract_post_id(ref: str) -> str | None:
    """Extract the platform-side post id from a URL, or return ``ref`` verbatim.

    Returns ``None`` for an empty reference. For a bare id (not a URL) the
    trimmed value is returned unchanged, so callers can pass either form.
    """
    if not isinstance(ref, str):
        return None
    ref = ref.strip()
    if not ref:
        return None
    if not is_post_url(ref):
        return ref

    parsed = urlparse(ref)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    # YouTube: /watch?v=ID, /shorts/ID, /embed/ID, /live/ID, youtu.be/ID
    if host == "youtu.be":
        segment = parsed.path.strip("/").split("/")[0]
        return segment or None
    if host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        query_id = parse_qs(parsed.query).get("v", [None])[0]
        if query_id:
            return query_id
        segments = [s for s in parsed.path.strip("/").split("/") if s]
        if not segments:
            return None
        # /shorts/ID, /embed/ID, /live/ID, /v/ID all end in the id.
        return segments[-1] or None

    if host not in _KNOWN_HOSTS:
        return None

    segments = [s for s in parsed.path.strip("/").split("/") if s]
    if not segments:
        return None
    # Walk backwards to the last segment that is not structural noise.
    for segment in reversed(segments):
        if segment.lower() not in _PATH_NOISE:
            return segment
    return None
