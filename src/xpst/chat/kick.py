"""Kick chat — its private realtime service, used exactly as a public viewer does.

What this cost to learn
-----------------------
Kick's published API documents chat only as *writes*: ``POST /public/v1/chat``
to send and ``DELETE /public/v1/chat/{id}`` to moderate
(``docs.kick.com/apis/chat``, checked 2026-11-16). There is no read endpoint.
The documented push alternative is the ``chat.message.sent`` webhook event,
which needs a registered app and a publicly reachable callback.

Community libraries pointed at a public Pusher app key
(``32cbd69e4b950bf97679``). That path is **dead**: Pusher answers
``4001 App key … not in this cluster`` on every cluster, for every key we could
find. What kick.com itself does now, captured from the live page on 2026-11-16,
is:

1. ``POST https://web.kick.com/api/v1/realtime/auth/connection`` with
   ``{"client_id": "<uuid>"}`` and ``x-app-platform: web`` returns an
   *anonymous guest* token (``sub: guest:<uuid>``, ~45 minute TTL). No account,
   no registration, no secret.
2. Connect ``wss://realtime.<region>.platform.kick.com/connection/websocket``
   (Centrifugo) and send ``{"connect": {"token": …}, "id": 1}``.
3. ``{"subscribe": {"channel": "chatrooms.<chatroom id>.v2"}, "id": N}`` — a
   public channel, no authorisation.
4. The server sends ``{}`` as a heartbeat and expects ``{}`` back; ignoring it
   gets the connection closed.

Evidence status, stated plainly
-------------------------------
Proven live from this repository on 2026-11-16: the guest token is issued, the
socket connects, ``connect`` and ``subscribe`` are both accepted, and the
heartbeat is answered. **No chat publication has been observed yet**, so the
message payload shape is parsed defensively (see :func:`message_from_publication`)
and the registry row keeps ``verified=False``. That is the honest state: a
connection is not a read.

Everything sent on the wire is ``connect``, ``subscribe`` and the empty
heartbeat — this module cannot send a message, a moderation action or a reply,
and does not attempt to.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from xpst.chat.model import (
    NETWORK_ERROR,
    NO_CHANNEL,
    UNVERIFIED_PATH,
    ChatError,
    ChatMessage,
    ReadOutcome,
    iso_from_text,
)

KICK_CHANNEL_API = "https://kick.com/api/v2"
#: The anonymous guest-token mint. Observed as a POST from kick.com's own page.
KICK_REALTIME_AUTH = "https://web.kick.com/api/v1/realtime/auth/connection"
#: Centrifugo endpoints Kick's page connected to, in the order we try them.
#: The region is chosen per page load, so neither is "the" endpoint.
KICK_REALTIME_HOSTS: tuple[str, ...] = (
    "wss://realtime.us-east-1.platform.kick.com/connection/websocket",
    "wss://realtime.us-west-2.platform.kick.com/connection/websocket",
)
SUBSCRIBE_TIMEOUT_S = 10.0

_DOCUMENTED_ALTERNATIVE = (
    "Kick publishes no chat read endpoint. The documented alternative is the chat.message.sent "
    "webhook event: register a Kick app (https://docs.kick.com/getting-started/kick-apps-setup) and "
    "subscribe with a publicly reachable callback. The realtime service used here is what "
    "kick.com's own page connects to and can change without notice."
)

#: ``async def (url) -> websocket``-shaped object with send_text/recv/close.
WsConnectFn = Callable[[str], Awaitable[Any]]
#: ``async def (url) -> (status_code, parsed_json)``.
HttpGetFn = Callable[[str], Awaitable[tuple[int, Any]]]
#: ``async def (url, body) -> (status_code, parsed_json)``.
HttpPostFn = Callable[[str, dict[str, Any]], Awaitable[tuple[int, Any]]]


def channel_slug(value: str) -> str:
    """Normalise ``slug``, ``@slug``, a kick.com URL, or a bare name."""
    text = unquote((value or "").strip())
    if not text:
        return ""
    text = text.lstrip("@")
    if "kick.com/" in text:
        text = urlsplit(text if "//" in text else "https://" + text).path
        text = text.strip("/").split("/", 1)[0]
    return text.strip().lower()


def chatroom_id_from_payload(payload: Any, slug: str = "") -> int:
    """The ``chatroom.id`` inside a Kick channel payload, or a coded refusal."""
    if not isinstance(payload, dict):
        raise ChatError(
            UNVERIFIED_PATH,
            f"kick: the channel page for {slug or 'this channel'} returned {type(payload).__name__}, not an object",
            platform="kick",
            action=_DOCUMENTED_ALTERNATIVE,
            docs_url="https://docs.kick.com/events/subscribe-to-events",
        )
    chatroom = payload.get("chatroom")
    room: dict[str, Any] = chatroom if isinstance(chatroom, dict) else {}
    raw = room.get("id")
    room_id = 0
    if isinstance(raw, (int, str)) and not isinstance(raw, bool):
        try:
            room_id = int(raw)
        except ValueError:
            room_id = 0
    if room_id <= 0:
        raise ChatError(
            UNVERIFIED_PATH,
            f"kick: no chatroom id in the channel payload for {slug or 'this channel'}",
            platform="kick",
            action=_DOCUMENTED_ALTERNATIVE,
            docs_url="https://docs.kick.com/events/subscribe-to-events",
        )
    return room_id


def _room_from_channel(channel: str) -> int:
    """``chatrooms.71696853.v2`` → ``71696853`` (0 when the shape is not that)."""
    parts = (channel or "").split(".")
    if len(parts) >= 2 and parts[0] == "chatrooms":
        try:
            return int(parts[1])
        except ValueError:
            return 0
    return 0


def _find_message_object(node: Any, depth: int = 0) -> dict[str, Any] | None:
    """The chat-message object inside a publication, wherever the vendor nests it.

    Kick has already moved this payload once (Pusher ``App\\Events\\ChatMessageEvent``
    → a Centrifugo publication) and no message has been captured yet on the new
    transport, so this walks the frame instead of indexing a fixed path. A chat
    message is identified by *both* non-empty ``content`` and a ``sender``
    object, which distinguishes it from pinned-message and subscription events
    that also carry text.
    """
    if depth > 6:
        return None
    if isinstance(node, dict):
        content = node.get("content")
        sender = node.get("sender")
        if isinstance(content, str) and content and isinstance(sender, dict):
            return node
        for value in node.values():
            found = _find_message_object(value, depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(node, list):
        for item in node:
            found = _find_message_object(item, depth + 1)
            if found is not None:
                return found
    return None


def message_from_publication(
    frame: dict[str, Any], *, slug_by_room: dict[int, str] | None = None
) -> ChatMessage | None:
    """A realtime frame as a :class:`ChatMessage`, or ``None`` if it is not chat."""
    if not isinstance(frame, dict):
        return None
    payload = frame.get("push", frame)
    message_object = _find_message_object(payload)
    if message_object is None:
        return None
    sender_raw = message_object.get("sender")
    sender: dict[str, Any] = sender_raw if isinstance(sender_raw, dict) else {}
    channel = ""
    if isinstance(payload, dict):
        channel = str(payload.get("channel", "") or "")
    slug = (slug_by_room or {}).get(_room_from_channel(channel), "")
    return ChatMessage(
        platform="kick",
        author=str(sender.get("username", "") or sender.get("slug", "") or "unknown"),
        text=str(message_object.get("content", "")),
        timestamp=iso_from_text(str(message_object.get("created_at", "") or "")),
        message_id=str(message_object.get("id", "") or ""),
        channel=slug or channel,
    )


def _decode(frame_text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(frame_text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def _default_http_get(url: str) -> tuple[int, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        response = await client.get(
            url,
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0 (compatible; xpst-chat/1)"},
        )
    try:
        payload: Any = response.json()
    except ValueError:
        payload = None
    return response.status_code, payload


async def _default_http_post(url: str, body: dict[str, Any]) -> tuple[int, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        response = await client.post(
            url,
            json=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "x-app-platform": "web",
                "Referer": "https://kick.com",
                "User-Agent": "Mozilla/5.0 (compatible; xpst-chat/1)",
            },
        )
    try:
        payload: Any = response.json()
    except ValueError:
        payload = None
    return response.status_code, payload


async def _default_ws_connect(url: str) -> Any:
    from xpst.chat import ws as ws_module

    return await ws_module.connect(url, timeout=15.0)


async def fetch_guest_token(*, http_post: HttpPostFn | None = None, client_id: str = "") -> str:
    """Mint an anonymous guest token the way kick.com's own page does.

    ``client_id`` is a locally generated UUID — it is not tied to an account,
    and the token it returns is scoped to ``guest:<uuid>``. Any value works, so
    xPST makes a fresh one rather than borrowing a user identifier.
    """
    poster = http_post or _default_http_post
    identifier = client_id or str(uuid.uuid4())
    status, payload = await poster(KICK_REALTIME_AUTH, {"client_id": identifier})
    if status >= 400:
        raise ChatError(
            UNVERIFIED_PATH,
            f"kick: the realtime guest-token endpoint answered HTTP {status}",
            platform="kick",
            action=_DOCUMENTED_ALTERNATIVE,
            status=status,
        )
    data = payload.get("data") if isinstance(payload, dict) else None
    token = str((data or {}).get("token", "") if isinstance(data, dict) else "").strip()
    if not token:
        raise ChatError(
            UNVERIFIED_PATH,
            "kick: the realtime guest-token endpoint returned no token",
            platform="kick",
            action=_DOCUMENTED_ALTERNATIVE,
        )
    return token


async def resolve_chatroom(slug: str, *, http_get: HttpGetFn | None = None) -> int:
    """Look up the public chatroom id for one channel slug."""
    fetcher = http_get or _default_http_get
    url = f"{KICK_CHANNEL_API}/channels/{quote(slug)}"
    status, payload = await fetcher(url)
    if status == 404:
        raise ChatError(
            NO_CHANNEL,
            f"kick: no channel named {slug!r} (kick.com answered 404)",
            platform="kick",
            action="Check the channel slug, e.g. `--kick-channel <slug>`.",
        )
    if status >= 400:
        raise ChatError(
            UNVERIFIED_PATH if status in (403, 429) else NETWORK_ERROR,
            f"kick: the channel lookup answered HTTP {status}",
            platform="kick",
            action=_DOCUMENTED_ALTERNATIVE,
        )
    return chatroom_id_from_payload(payload, slug)


def _subscription_error(frame: dict[str, Any]) -> str:
    """The error text inside a Centrifugo reply, or ``""`` when it succeeded."""
    for key in ("connect", "subscribe", "error"):
        block = frame.get(key)
        if isinstance(block, dict):
            error = block.get("error")
            if isinstance(error, dict):
                return str(error.get("message") or error.get("code") or error)
            if isinstance(error, str) and error:
                return error
    return ""


async def _open_session(
    token: str,
    rooms: dict[str, int],
    *,
    ws_connect: WsConnectFn,
    hosts: tuple[str, ...],
    subscribe_timeout_s: float,
) -> tuple[Any, str]:
    """Connect and subscribe, trying each known region until one accepts."""
    last_error = ""
    for host in hosts:
        try:
            socket = await asyncio.wait_for(ws_connect(host), timeout=subscribe_timeout_s)
        except (asyncio.TimeoutError, TimeoutError):
            last_error = f"timed out connecting to {host}"
            continue
        except Exception as exc:  # noqa: BLE001 - transport raises its own type
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        try:
            await socket.send_text(json.dumps({"connect": {"token": token, "name": "js"}, "id": 1}))
            acknowledged = await _await_id(socket, 1, subscribe_timeout_s)
            error = _subscription_error(acknowledged)
            if error:
                raise ChatError(
                    UNVERIFIED_PATH,
                    f"kick: the realtime service refused the guest connection ({error})",
                    platform="kick",
                    action=_DOCUMENTED_ALTERNATIVE,
                    docs_url="https://docs.kick.com/events/subscribe-to-events",
                )
            wanted = {index: room_id for index, room_id in enumerate(rooms.values(), start=2)}
            for index, room_id in wanted.items():
                await socket.send_text(
                    json.dumps({"subscribe": {"channel": f"chatrooms.{room_id}.v2", "flag": 1}, "id": index})
                )
            for index in wanted:
                reply = await _await_id(socket, index, subscribe_timeout_s)
                error = _subscription_error(reply)
                if error:
                    raise ChatError(
                        UNVERIFIED_PATH,
                        f"kick: the chat channel refused the subscription ({error})",
                        platform="kick",
                        action=_DOCUMENTED_ALTERNATIVE,
                        docs_url="https://docs.kick.com/events/subscribe-to-events",
                    )
            return socket, host
        except ChatError:
            closer = getattr(socket, "close", None)
            if closer is not None:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            closer = getattr(socket, "close", None)
            if closer is not None:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
            continue
    raise ChatError(
        NETWORK_ERROR,
        f"kick: no realtime region accepted the connection ({last_error or 'no hosts tried'})",
        platform="kick",
        action=_DOCUMENTED_ALTERNATIVE,
    )


async def _await_id(socket: Any, wanted_id: int, timeout_s: float) -> dict[str, Any]:
    """Read frames until the reply carrying ``wanted_id`` arrives.

    The server interleaves heartbeats and publications with replies, so a plain
    "next frame is my answer" read is wrong here.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        try:
            raw = await socket.recv(timeout=max(0.05, deadline - loop.time()))
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise ChatError(
                UNVERIFIED_PATH,
                "kick: the realtime service never acknowledged the subscription",
                platform="kick",
                action=_DOCUMENTED_ALTERNATIVE,
            ) from exc
        frame = _decode(raw)
        if frame is None:
            continue
        if not frame:
            # Centrifugo heartbeat: the peer expects the same empty object back
            # or it closes the connection.
            await socket.send_text("{}")
            continue
        if frame.get("id") == wanted_id:
            return frame
    raise ChatError(
        UNVERIFIED_PATH,
        "kick: the realtime service never acknowledged the subscription",
        platform="kick",
        action=_DOCUMENTED_ALTERNATIVE,
    )


async def read_kick_chat(
    slugs: list[str],
    duration_s: float,
    *,
    http_get: HttpGetFn | None = None,
    http_post: HttpPostFn | None = None,
    ws_connect: WsConnectFn | None = None,
    hosts: tuple[str, ...] = KICK_REALTIME_HOSTS,
    subscribe_timeout_s: float = SUBSCRIBE_TIMEOUT_S,
) -> ReadOutcome:
    """Subscribe to each channel's public chat and collect messages for the window.

    A channel that cannot be resolved lands in ``unconfirmed`` (that channel's
    chat is unreadable, which is a fact about the channel); a refused
    connection or subscription raises :class:`ChatError` with
    ``unverified_path``, because that means Kick's undocumented path has moved
    again.
    """
    wanted = [channel_slug(slug) for slug in slugs if channel_slug(slug)]
    if not wanted:
        raise ChatError(NO_CHANNEL, "kick: no channel slug was given", platform="kick")

    rooms: dict[str, int] = {}
    unconfirmed: list[str] = []
    for slug in wanted:
        try:
            rooms[slug] = await resolve_chatroom(slug, http_get=http_get)
        except ChatError as exc:
            if exc.code == NO_CHANNEL:
                unconfirmed.append(slug)
                continue
            raise
    if not rooms:
        raise ChatError(
            NO_CHANNEL,
            f"kick: none of the requested channels exist ({', '.join(unconfirmed)})",
            platform="kick",
            action="Check the channel slug, e.g. `--kick-channel <slug>`.",
        )
    slug_by_room = {room_id: slug for slug, room_id in rooms.items()}

    token = await fetch_guest_token(http_post=http_post)
    socket, host = await _open_session(
        token,
        rooms,
        ws_connect=ws_connect or _default_ws_connect,
        hosts=hosts,
        subscribe_timeout_s=subscribe_timeout_s,
    )

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(1.0, duration_s)
    messages: list[ChatMessage] = []
    try:
        while loop.time() < deadline:
            try:
                raw = await socket.recv(timeout=max(0.05, deadline - loop.time()))
            except (asyncio.TimeoutError, TimeoutError):
                break
            except Exception:  # noqa: BLE001 - a close frame ends the window cleanly
                break
            frame = _decode(raw) if raw else None
            if frame is None:
                continue
            if not frame:
                await socket.send_text("{}")
                continue
            message = message_from_publication(frame, slug_by_room=slug_by_room)
            if message is not None:
                messages.append(message)
    finally:
        closer = getattr(socket, "close", None)
        if closer is not None:
            result = closer()
            if asyncio.iscoroutine(result):
                await result

    note = (
        f"Kick realtime ({host}) — an undocumented path. Connection, subscription and heartbeat "
        "are proven; no chat publication has been observed from this repository yet, so the "
        "message payload is parsed defensively and the registry row stays unverified."
    )
    return ReadOutcome(messages=messages, channels=list(rooms), unconfirmed=unconfirmed, note=note)


def endpoint_for(room_id: int) -> str:
    """The subscription address an operator/agent can recognise."""
    return f"{KICK_REALTIME_HOSTS[0]} → channel chatrooms.{room_id}.v2"


__all__ = [
    "KICK_REALTIME_AUTH",
    "KICK_REALTIME_HOSTS",
    "channel_slug",
    "chatroom_id_from_payload",
    "endpoint_for",
    "fetch_guest_token",
    "message_from_publication",
    "read_kick_chat",
    "resolve_chatroom",
]
