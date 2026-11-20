"""Which platform's live chat an app can read, over what, and under which caveat.

This is data, not behaviour, for the same reason
:mod:`xpst.stream.destinations` is: the CLI, the MCP tool, the HTTP API and the
documentation must answer "can xPST read this platform's chat?" identically
instead of each inventing a verdict.

The three vendors answer that question very differently:

``twitch``
    IRC over TLS (``irc://irc.chat.twitch.tv:6697``) or WebSocket
    (``wss://irc-ws.chat.twitch.tv:443``) — the same protocol on two
    transports. An *anonymous* connection (``NICK justinfan<digits>``, no PASS)
    reads public chat with no account, no OAuth and no app registration.
    Verified live from this repository 2026-11-16: connected, joined a public
    channel and received a real ``PRIVMSG`` carrying ``display-name``, ``id``
    and ``tmi-sent-ts`` tags.
``youtube``
    ``GET /youtube/v3/liveChatMessages?liveChatId=…``, 5 quota units per poll
    against a 10,000-unit daily default, on an OAuth token the operator already
    has. Reading chat requires the channel to be *live* — there is no chat id
    for a video that has never broadcast.
``kick``
    Kick's published API has **no chat read endpoint**: ``/public/v1/chat`` is
    POST (send) and DELETE (moderate) only, and the documented push alternative
    is the ``chat.message.sent`` *webhook* event, which needs a registered app
    and a publicly reachable endpoint. What Kick's own website uses is a public
    Pusher channel (``chatrooms.<chatroom id>.v2`` on ``ws-us2.pusher.com``)
    that anyone can subscribe to without authorisation. That is the path this
    module describes, and it is marked ``verified=False`` /
    ``UNVERIFIED_PATH`` until a message read is recorded: it is undocumented,
    it can change without notice, and pretending otherwise would be the same
    class of error as the registry row that once named a Kick stream-key
    endpoint Kick does not publish.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Transport spellings. Never add a fourth without teaching every surface.
IRC_TLS = "irc_tls"
HTTP_API = "http_api"
#: Kick's own realtime service. Named for what it is rather than for Pusher:
#: the Pusher app key community libraries still document is retired.
REALTIME_WS = "realtime_ws"


@dataclass(frozen=True)
class ChatReadPath:
    """One platform's chat-read story.

    ``verified`` records whether *this repository* has ever completed a live
    read. It is evidence about the path, not a TODO: a documented-but-unproven
    call and a proven one are different claims and the payload must not blur
    them.
    """

    platform: str
    display_name: str
    transport: str
    endpoint: str
    requires: str
    operator_action: str
    source_url: str
    verified: bool
    note: str = ""
    read_only: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "display_name": self.display_name,
            "transport": self.transport,
            "endpoint": self.endpoint,
            "requires": self.requires,
            "operator_action": self.operator_action,
            "source_url": self.source_url,
            "verified": self.verified,
            "read_only": self.read_only,
            "note": self.note,
        }


CHAT_READ_PATHS: dict[str, ChatReadPath] = {
    "twitch": ChatReadPath(
        platform="twitch",
        display_name="Twitch",
        transport=IRC_TLS,
        endpoint="irc://irc.chat.twitch.tv:6697 (TLS; wss://irc-ws.chat.twitch.tv:443 is the same protocol)",
        requires="nothing — an anonymous justinfan connection reads public chat",
        operator_action="name a channel: `--twitch-channel <login>` or chat.twitch_channels in config",
        source_url="https://dev.twitch.tv/docs/chat/irc/",
        verified=True,
        note=(
            "Anonymous connections cannot send, moderate or whisper; xPST never asks for a "
            "chat token, so the connection is read-only by construction rather than by policy."
        ),
    ),
    "youtube": ChatReadPath(
        platform="youtube",
        display_name="YouTube Live",
        transport=HTTP_API,
        endpoint="GET https://www.googleapis.com/youtube/v3/liveChatMessages?liveChatId=<id>",
        requires=(
            "OAuth token with youtube.readonly or youtube.force-ssl, and a broadcast that is live "
            "now (the activeLiveChatId only exists while a video is live)"
        ),
        operator_action="connect the channel with `xpst auth youtube`, then go live once",
        source_url="https://developers.google.com/youtube/v3/live/docs/liveChatMessages/list",
        verified=False,
        note=(
            "5 quota units per poll against a 10,000-unit daily default, so 2,000 polls is the whole "
            "day. Never exercised live from this repository: the connected channel answers 403 "
            "'not enabled for live streaming', which is why a read refuses with live_not_enabled "
            "rather than an empty feed."
        ),
    ),
    "kick": ChatReadPath(
        platform="kick",
        display_name="Kick",
        transport=REALTIME_WS,
        endpoint="wss://realtime.<region>.platform.kick.com/connection/websocket (channel chatrooms.<id>.v2)",
        requires="nothing — an anonymous guest token and a public channel subscription",
        operator_action="name a channel: `--kick-channel <slug>` or chat.kick_channels in config",
        source_url="https://docs.kick.com/apis/chat",
        verified=False,
        note=(
            "UNDOCUMENTED PATH. Kick publishes no chat read endpoint: /public/v1/chat is send and "
            "delete only, and the documented alternative is the chat.message.sent webhook event, "
            "which needs a registered app and a public callback. Community libraries still point at "
            "a public Pusher app key that Kick has retired (Pusher answers 4001 on every cluster); "
            "kick.com's own page now mints an anonymous guest token at "
            "web.kick.com/api/v1/realtime/auth/connection and connects to its own Centrifugo "
            "service. Connection, subscription and heartbeat are proven from this repository; no "
            "chat publication has been observed yet, so the row stays unverified."
        ),
    ),
}


def read_path(platform: str) -> ChatReadPath | None:
    return CHAT_READ_PATHS.get(platform.strip().lower())


def registry_snapshot() -> list[dict[str, object]]:
    """Every chat read path as a serialisable row, in presentation order."""
    return [CHAT_READ_PATHS[name].to_dict() for name in ("twitch", "youtube", "kick") if name in CHAT_READ_PATHS]
