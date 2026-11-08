"""The one place xPST pins Meta's Graph API version.

Meta removes a Graph API version roughly two years after it lands — v19.0 on
2026-05-21, v20.0 on 2026-09-24, v21.0 on 2027-01-21 (version table:
https://developers.facebook.com/docs/graph-api/changelog). A version literal
copied into each platform module outlives that date silently: the calls keep
working until the removal day, then every Instagram/Facebook/Messenger path
answers a version error at once. So every Graph call site builds its URL here,
and ``tests/test_graph_api_version.py`` fails on a new ``graph.facebook.com/vNN``
literal anywhere else in ``src/``.

Operators and tests can retarget the whole family:

* ``XPST_GRAPH_API_BASE`` — the complete base, host **and** version
  (e.g. ``https://graph.facebook.com/v26.0``), read at call time so a test can
  monkeypatch it. It names one base, so an explicit per-call ``version``
  argument bypasses it.
* ``XPST_GRAPH_API_VERSION`` — only the version segment.

The resumable-upload host and the OAuth consent dialog are different hosts with
the same version segment; they are derived here so a bump is one edit.
"""

from __future__ import annotations

import os

#: Pinned Graph API version. v26.0 was released 2026-07-29 and is the latest in
#: Meta's table; it must stay >= the oldest version Meta still serves. Re-read
#: the version table before bumping, and keep this the only literal.
GRAPH_API_VERSION = "v26.0"

#: Plain Graph read/write host.
GRAPH_API_HOST = "https://graph.facebook.com"
#: Video upload host (separate host, same ``/<version>/`` segment).
GRAPH_VIDEO_API_HOST = "https://graph-video.facebook.com"
#: Instagram resumable-upload host; path shape is ``/ig-api-upload/<version>/<container>``.
GRAPH_UPLOAD_HOST = "https://rupload.facebook.com"
#: OAuth consent dialog host; path shape is ``/<version>/dialog/oauth``.
GRAPH_LOGIN_HOST = "https://www.facebook.com"

_ENV_BASE = "XPST_GRAPH_API_BASE"
_ENV_VERSION = "XPST_GRAPH_API_VERSION"


def graph_api_version() -> str:
    """The version segment to use, honouring ``XPST_GRAPH_API_VERSION``."""
    override = os.environ.get(_ENV_VERSION, "").strip()
    return override or GRAPH_API_VERSION


def graph_api_base(version: str | None = None) -> str:
    """Graph API base URL without a trailing slash.

    ``XPST_GRAPH_API_BASE`` wins when it is set and no explicit ``version`` was
    requested — it may point at a mock server, so it is returned verbatim
    (minus a trailing slash). An explicit ``version`` other than the pinned one
    is built from :data:`GRAPH_API_HOST` instead, because a single override
    cannot describe two versions at once.
    """
    pinned = graph_api_version()
    if version and version != pinned:
        return f"{GRAPH_API_HOST}/{version.lstrip('/')}"
    override = os.environ.get(_ENV_BASE, "").strip()
    if override:
        return override.rstrip("/")
    return f"{GRAPH_API_HOST}/{pinned}"


def graph_api_url(path: str, *, version: str | None = None) -> str:
    """Absolute URL for a Graph API ``path`` (leading slash optional)."""
    return f"{graph_api_base(version)}/{path.lstrip('/')}"


def graph_video_api_url(path: str, *, version: str | None = None) -> str:
    """Absolute URL on the video-upload host (same version segment)."""
    segment = version or graph_api_version()
    return f"{GRAPH_VIDEO_API_HOST}/{segment}/{path.lstrip('/')}"


def graph_upload_url(path: str, *, version: str | None = None) -> str:
    """Absolute URL on the Instagram resumable-upload host."""
    segment = version or graph_api_version()
    return f"{GRAPH_UPLOAD_HOST}/ig-api-upload/{segment}/{path.lstrip('/')}"


def graph_login_dialog_url(*, version: str | None = None) -> str:
    """OAuth consent dialog URL (caller appends ``?<query>``)."""
    segment = version or graph_api_version()
    return f"{GRAPH_LOGIN_HOST}/{segment}/dialog/oauth"


__all__ = [
    "GRAPH_API_HOST",
    "GRAPH_API_VERSION",
    "GRAPH_LOGIN_HOST",
    "GRAPH_UPLOAD_HOST",
    "GRAPH_VIDEO_API_HOST",
    "graph_api_base",
    "graph_api_url",
    "graph_api_version",
    "graph_login_dialog_url",
    "graph_upload_url",
    "graph_video_api_url",
]
