"""Live chat, read-only, from every platform that lets an app watch it.

One merged timeline instead of three panes: `xpst chat timeline` (and the
``xpst_chat`` MCP tool and ``GET /api/chat``) read Twitch, YouTube and Kick
concurrently for a bounded window and return a single feed of
``platform``/``author``/``text``/``timestamp`` rows plus an honest per-platform
verdict.

This package is read-only on purpose. Nothing here sends a message, deletes
one, or answers anyone: an anonymous Twitch connection cannot, and the YouTube
and Kick readers only call list/subscribe operations. Posting into a live chat
is a separate decision with its own consent story.

No new runtime dependency: Twitch is IRC over TLS, YouTube is the Data API the
project already depends on, and Kick's Pusher socket is spoken by the small
RFC 6455 client in :mod:`xpst.chat.ws`.

See ``docs/CHAT-MIXING.md`` for the per-platform matrix and its evidence.
"""

from __future__ import annotations

from xpst.chat.aggregate import (
    DEFAULT_DURATION_S,
    DEFAULT_LIMIT,
    MAX_DURATION_S,
    MAX_LIMIT,
    chat_readiness,
    chat_timeline,
    collect_chat,
    known_platforms,
    resolve_channels,
)
from xpst.chat.model import (
    CHAT_PLATFORMS,
    CREDENTIALS_MISSING,
    INVALID_RESPONSE,
    NETWORK_ERROR,
    NO_CHANNEL,
    NOT_LIVE,
    NOT_SUPPORTED,
    PLATFORM_ERROR,
    UNVERIFIED_PATH,
    ChatError,
    ChatMessage,
    PlatformRead,
    ReadOutcome,
    Timeline,
)
from xpst.chat.registry import (
    CHAT_READ_PATHS,
    HTTP_API,
    IRC_TLS,
    REALTIME_WS,
    ChatReadPath,
    read_path,
    registry_snapshot,
)

__all__ = [
    "CHAT_PLATFORMS",
    "CHAT_READ_PATHS",
    "CREDENTIALS_MISSING",
    "ChatError",
    "ChatMessage",
    "ChatReadPath",
    "DEFAULT_DURATION_S",
    "DEFAULT_LIMIT",
    "HTTP_API",
    "INVALID_RESPONSE",
    "IRC_TLS",
    "MAX_DURATION_S",
    "MAX_LIMIT",
    "NETWORK_ERROR",
    "NO_CHANNEL",
    "NOT_LIVE",
    "NOT_SUPPORTED",
    "PLATFORM_ERROR",
    "REALTIME_WS",
    "PlatformRead",
    "ReadOutcome",
    "Timeline",
    "UNVERIFIED_PATH",
    "chat_readiness",
    "chat_timeline",
    "collect_chat",
    "known_platforms",
    "read_path",
    "registry_snapshot",
    "resolve_channels",
]
