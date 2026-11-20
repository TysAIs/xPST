"""Twitch chat, read over IRC — no account, no OAuth, no app registration.

The anonymous connection
------------------------
Twitch's IRC gateway accepts a ``NICK justinfan<digits>`` login with no PASS.
An anonymous session can ``JOIN`` any public channel and receives its
``PRIVMSG`` traffic, and it cannot send, moderate or whisper — read-only is a
property of the connection type, not a policy xPST enforces. Verified live from
this repository 2026-11-16 against a public channel: connected, joined,
received a tagged ``PRIVMSG``.

Transport
---------
Twitch documents the same protocol on two transports:
``irc://irc.chat.twitch.tv:6697`` (TLS) and
``wss://irc-ws.chat.twitch.tv:443``. This module uses the IRC-over-TLS form
because it needs an 8-line asyncio client instead of a WebSocket one — the
WebSocket client in :mod:`xpst.chat.ws` exists for Kick, which offers no
alternative, and duplicating a transport for no behavioural gain would be
extra surface to keep correct.

Parsing is a pure function (:func:`parse_irc_line`, :func:`message_from_line`)
so the tag/prefix/escape handling is provable against literal IRC lines with no
socket, which is how the tests drive it.
"""

from __future__ import annotations

import asyncio
import random
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import unquote

from xpst.chat.model import (
    INVALID_RESPONSE,
    NETWORK_ERROR,
    NO_CHANNEL,
    ChatError,
    ChatMessage,
    ReadOutcome,
    iso_from_epoch_ms,
)

if TYPE_CHECKING:
    from asyncio import StreamReader, StreamWriter

DEFAULT_IRC_HOST = "irc.chat.twitch.tv"
DEFAULT_IRC_PORT = 6697
#: How long to wait for the server's 366 RPL_ENDOFNAMES before calling a JOIN
#: unconfirmed. Twitch silently discards a JOIN for a channel that does not
#: exist, so silence is the only signal available.
JOIN_CONFIRM_TIMEOUT_S = 6.0
_MAX_LINE_BYTES = 32_768

#: ``async def (host, port) -> (reader, writer)``. Injectable so the reader can
#: be exercised without a network.
ConnectFn = Callable[[str, int], Awaitable["tuple[StreamReader, StreamWriter]"]]


@dataclass(frozen=True)
class IrcLine:
    """One parsed IRCv3 line: tags, prefix, command, params and trailing part."""

    tags: dict[str, str]
    prefix: str
    command: str
    params: list[str]
    trailing: str


def _unescape_tag(value: str) -> str:
    """IRCv3 tag unescaping (``\\s`` space, ``\\:`` ``;``, ``\\\\`` ``\\``, CR/LF)."""
    if "\\" not in value:
        return value
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            out.append({"s": " ", ":": ";", "\\": "\\", "r": "\r", "n": "\n"}.get(nxt, nxt))
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def parse_irc_line(line: str) -> IrcLine | None:
    """Parse one IRC line. Returns ``None`` for a blank/keepalive-free line."""
    text = line.rstrip("\r\n")
    if not text.strip():
        return None
    tags: dict[str, str] = {}
    if text.startswith("@"):
        raw_tags, _, text = text[1:].partition(" ")
        for pair in raw_tags.split(";"):
            if not pair:
                continue
            name, sep, value = pair.partition("=")
            tags[name] = _unescape_tag(value) if sep else ""
        text = text.lstrip()
    prefix = ""
    if text.startswith(":"):
        prefix, _, text = text[1:].partition(" ")
    trailing = ""
    if " :" in text:
        text, _, trailing = text.partition(" :")
    parts = text.split()
    if not parts:
        return None
    return IrcLine(tags=tags, prefix=prefix, command=parts[0].upper(), params=parts[1:], trailing=trailing)


def _nick_from_prefix(prefix: str) -> str:
    return prefix.split("!", 1)[0] if prefix else ""


def message_from_line(line: IrcLine, channel: str = "") -> ChatMessage | None:
    """A ``PRIVMSG`` as a :class:`ChatMessage`; anything else is ``None``.

    Chat-visible text lives in the trailing parameter. ``display-name`` is the
    human spelling the platform shows and is preferred over the login in the
    prefix; ``id`` and ``tmi-sent-ts`` give the message its stable identity and
    its real time, which the merged timeline needs in order to sort fairly
    against YouTube and Kick.
    """
    if line.command != "PRIVMSG":
        return None
    if line.params:
        channel = line.params[0].lstrip("#") or channel
    author = line.tags.get("display-name") or _nick_from_prefix(line.prefix) or "unknown"
    return ChatMessage(
        platform="twitch",
        author=author,
        text=line.trailing,
        timestamp=iso_from_epoch_ms(line.tags.get("tmi-sent-ts")),
        message_id=line.tags.get("id", ""),
        channel=channel,
    )


async def _default_connect(host: str, port: int) -> tuple[StreamReader, StreamWriter]:
    context = ssl.create_default_context()
    return await asyncio.open_connection(host, port, ssl=context)


def _anonymous_nick() -> str:
    return f"justinfan{random.randint(10000, 99999)}"  # noqa: S311 - not cryptography


async def read_twitch_chat(
    channels: list[str],
    duration_s: float,
    *,
    connect: ConnectFn | None = None,
    host: str = DEFAULT_IRC_HOST,
    port: int = DEFAULT_IRC_PORT,
    join_timeout_s: float = JOIN_CONFIRM_TIMEOUT_S,
) -> ReadOutcome:
    """Read one or more public channels for ``duration_s`` and return the messages.

    Raises :class:`ChatError` when the connection itself fails; a channel that
    the server never confirms ends up in ``unconfirmed`` instead, because that
    is a per-channel fact and the other channels may still be readable.
    """
    wanted = [_normalise_channel(name) for name in channels if _normalise_channel(name)]
    if not wanted:
        raise ChatError(NO_CHANNEL, "twitch: no channel login was given", platform="twitch")
    connect_fn = connect or _default_connect

    try:
        reader, writer = await asyncio.wait_for(connect_fn(host, port), timeout=join_timeout_s)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise ChatError(
            NETWORK_ERROR,
            f"twitch: could not reach {host}:{port} within {join_timeout_s:.0f}s",
            platform="twitch",
        ) from exc
    except OSError as exc:
        raise ChatError(NETWORK_ERROR, f"twitch: could not reach {host}:{port} ({exc})", platform="twitch") from exc

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(1.0, duration_s)

    confirmed: set[str] = set()
    messages: list[ChatMessage] = []
    notice = ""
    try:
        writer.write(b"CAP REQ :twitch.tv/tags twitch.tv/commands\r\n")
        writer.write(f"NICK {_anonymous_nick()}\r\n".encode())
        writer.write(("JOIN " + ",".join(f"#{channel}" for channel in wanted) + "\r\n").encode())
        await writer.drain()

        while loop.time() < deadline:
            remaining = max(0.05, deadline - loop.time())
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=remaining)
            except (asyncio.TimeoutError, TimeoutError):
                break
            if not raw:
                break
            if len(raw) > _MAX_LINE_BYTES:
                raise ChatError(INVALID_RESPONSE, "twitch: the server sent an oversized IRC line", platform="twitch")
            line = parse_irc_line(raw.decode("utf-8", "replace"))
            if line is None:
                continue
            if line.command == "PING":
                writer.write(f"PONG :{line.trailing or 'tmi.twitch.tv'}\r\n".encode())
                await writer.drain()
                continue
            if line.command == "366":
                # ":tmi.twitch.tv 366 <nick> #<channel> :End of /NAMES list"
                if line.params:
                    confirmed.add(line.params[-1].lstrip("#").lower())
                continue
            if line.command == "NOTICE":
                notice = line.trailing or notice
                continue
            message = message_from_line(line)
            if message is not None:
                messages.append(message)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, RuntimeError):  # pragma: no cover - transport already gone
            pass

    unconfirmed = [channel for channel in wanted if channel.lower() not in confirmed]
    if unconfirmed and messages and not confirmed:
        # Messages arrived and the server never sent 366: the NAMES reply is a
        # convenience, not a gate on readability. Do not call that unconfirmed.
        unconfirmed = []
    note = ""
    if unconfirmed:
        note = (
            "Twitch silently drops a JOIN for a channel that does not exist, so a channel with no "
            "RPL_ENDOFNAMES is reported unconfirmed rather than as a quiet chat."
        )
    if notice:
        note = (note + " " if note else "") + f"server notice: {notice}"
    return ReadOutcome(messages=messages, channels=list(confirmed) or wanted, unconfirmed=unconfirmed, note=note)


def _normalise_channel(name: str) -> str:
    """Accept ``chan``, ``#chan`` and a full channel URL; return the login."""
    text = unquote((name or "").strip())
    if not text:
        return ""
    if "twitch.tv/" in text:
        text = text.split("twitch.tv/", 1)[1]
    text = text.lstrip("#").split("/", 1)[0].split("?", 1)[0]
    return text.strip().lower()


#: Kept for symmetry with the other readers' public surface.
TWITCH_IRC_ENDPOINT = f"irc://{DEFAULT_IRC_HOST}:{DEFAULT_IRC_PORT}"
