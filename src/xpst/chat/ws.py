"""A minimal RFC 6455 WebSocket client — the only transport Kick's chat offers.

Why this exists instead of a dependency
---------------------------------------
Reading Kick's live chat means subscribing to a public Pusher channel over
WebSocket. Twitch is readable over plain IRC-over-TLS (see
:mod:`xpst.chat.twitch`), so WebSocket is needed for exactly one platform, and
adding a general WebSocket library to the runtime for that is a licensing and
supply-chain decision this slice deliberately did not take. What a *client*
needs is small and fully specified: an opening handshake (RFC 6455 §4.1) and
the frame format (§5.2–5.7). No extensions are offered, so a server cannot
switch on ``permessage-deflate`` and the implementation never has to speak it.

Deliberately out of scope: compression, subprotocol negotiation, redirects,
and client-side masking of outgoing frames is implemented because the spec
REQUIRES a client to mask every frame it sends (§5.3) — a server that is
strict about that rejects unmasked frames.

Testability: :class:`FrameBuffer` and :func:`encode_frame` are pure, so the
framing is proven against hand-built byte fixtures without a socket. The
socket layer is injectable at :func:`connect`, which is what the chat readers
use to run without a network.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import ssl
import struct
from typing import Any
from urllib.parse import urlsplit

#: Handshake magic string, RFC 6455 §1.3.
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

#: A chat frame is JSON, so anything near this is a malformed stream, not a
#: message. The cap exists so a hostile or broken peer cannot exhaust memory.
MAX_FRAME_BYTES = 1_048_576
#: Read size for the underlying socket; frames arrive in far smaller pieces.
_READ_CHUNK = 4096


class WebSocketError(RuntimeError):
    """The WebSocket connection failed, or the peer spoke nonsense."""


class WebSocketClosedError(WebSocketError):
    """The peer closed the connection (a clean ``CLOSE`` frame)."""


def accept_key(client_key: str) -> str:
    """The ``Sec-WebSocket-Accept`` value a compliant server must answer."""
    digest = hashlib.sha1(  # noqa: S324 - RFC 6455 mandates SHA-1 for this handshake digest
        (client_key + _GUID).encode("ascii"), usedforsecurity=False
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def _unmask(payload: bytes, mask: bytes) -> bytes:
    if not mask:
        return payload
    return bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))


def encode_frame(
    payload: bytes,
    *,
    opcode: int = OP_TEXT,
    fin: bool = True,
    mask: bytes | None = None,
) -> bytes:
    """Serialise one client frame.

    A client MUST mask every frame it sends (§5.3); ``mask`` defaults to fresh
    random bytes so a caller cannot accidentally emit an unmasked frame.
    """
    mask = os.urandom(4) if mask is None else mask
    header = bytearray()
    header.append((0x80 if fin else 0x00) | (opcode & 0x0F))
    length = len(payload)
    mask_bit = 0x80
    if length < 126:
        header.append(mask_bit | length)
    elif length < (1 << 16):
        header.append(mask_bit | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(mask_bit | 127)
        header.extend(struct.pack("!Q", length))
    header.extend(mask)
    header.extend(_unmask(payload, mask))
    return bytes(header)


class FrameBuffer:
    """Incremental frame decoder: feed bytes, pull complete frames.

    ``next_frame`` returns ``(fin, opcode, payload)`` or ``None`` when the
    buffer does not hold a whole frame yet. Masking is honoured in both
    directions — a server is not supposed to mask, but a proxy in front of one
    has been seen to, and rejecting that would be a pointless incompatibility.
    """

    def __init__(self, max_frame_bytes: int = MAX_FRAME_BYTES) -> None:
        self._buf = bytearray()
        self.max_frame_bytes = max_frame_bytes

    def feed(self, data: bytes) -> None:
        self._buf.extend(data)

    def next_frame(self) -> tuple[bool, int, bytes] | None:
        buf = self._buf
        if len(buf) < 2:
            return None
        first, second = buf[0], buf[1]
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        rsv = first & 0x70
        if rsv:
            raise WebSocketError(f"server set reserved frame bits 0x{rsv:02x} with no extension negotiated")
        masked = bool(second & 0x80)
        length = second & 0x7F
        offset = 2
        if length == 126:
            if len(buf) < 4:
                return None
            length = int.from_bytes(buf[2:4], "big")
            offset = 4
        elif length == 127:
            if len(buf) < 10:
                return None
            length = int.from_bytes(buf[2:10], "big")
            offset = 10
        if length > self.max_frame_bytes:
            raise WebSocketError(f"frame of {length} bytes exceeds the {self.max_frame_bytes}-byte cap")
        mask = b""
        if masked:
            if len(buf) < offset + 4:
                return None
            mask = bytes(buf[offset : offset + 4])
            offset += 4
        if len(buf) < offset + length:
            return None
        payload = bytes(buf[offset : offset + length])
        del buf[: offset + length]
        return fin, opcode, _unmask(payload, mask)


class WebSocket:
    """One open connection, with the frame layer handled.

    ``recv`` returns whole text messages: continuation frames are joined, pings
    are answered in place, and a close frame raises :class:`WebSocketClosedError`.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._frames = FrameBuffer()
        self._closed = False
        self._fragments: list[bytes] = []

    def prefeed(self, data: bytes) -> None:
        """Bytes the handshake already read off the socket, before frame parsing."""
        if data:
            self._frames.feed(data)

    async def send_text(self, text: str) -> None:
        if self._closed:
            raise WebSocketClosedError("cannot send on a closed WebSocket")
        self._writer.write(encode_frame(text.encode("utf-8"), opcode=OP_TEXT))
        await self._writer.drain()

    async def _send_control(self, opcode: int, payload: bytes = b"") -> None:
        if self._closed:
            return
        self._writer.write(encode_frame(payload, opcode=opcode))
        try:
            await self._writer.drain()
        except (ConnectionError, RuntimeError):  # pragma: no cover - peer already gone
            self._closed = True

    async def recv(self, timeout: float | None = None) -> str:
        """The next text message. Raises :class:`WebSocketClosedError` on close."""
        coro = self._recv_message()
        if timeout is None:
            return await coro
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise WebSocketError(f"no message within {timeout:.1f}s") from exc

    async def _recv_message(self) -> str:
        while True:
            frame = self._frames.next_frame()
            if frame is None:
                chunk = await self._reader.read(_READ_CHUNK)
                if not chunk:
                    self._closed = True
                    raise WebSocketClosedError("the peer closed the TCP stream without a close frame")
                self._frames.feed(chunk)
                continue
            fin, opcode, payload = frame
            if opcode == OP_PING:
                await self._send_control(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self._closed = True
                await self._send_control(OP_CLOSE, payload[:2])
                raise WebSocketClosedError("the peer sent a close frame")
            if opcode in (OP_TEXT, OP_BINARY):
                self._fragments = [payload]
            elif opcode == OP_CONTINUATION:
                if not self._fragments:
                    raise WebSocketError("continuation frame with no message in progress")
                self._fragments.append(payload)
            else:
                raise WebSocketError(f"unknown frame opcode 0x{opcode:02x}")
            if fin:
                message = b"".join(self._fragments)
                self._fragments = []
                return message.decode("utf-8", "replace")
            if sum(len(part) for part in self._fragments) > MAX_FRAME_BYTES:
                raise WebSocketError("fragmented message exceeded the frame cap")

    async def close(self) -> None:
        if not self._closed:
            await self._send_control(OP_CLOSE, struct.pack("!H", 1000))
            self._closed = True
        try:
            self._writer.close()
        except (ConnectionError, RuntimeError):  # pragma: no cover - already gone
            pass

    async def __aenter__(self) -> WebSocket:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()


async def connect(
    url: str,
    *,
    timeout: float = 15.0,
    extra_headers: dict[str, str] | None = None,
) -> WebSocket:
    """Open ``ws://``/``wss://`` ``url`` and verify the handshake (§4.1)."""
    parts = urlsplit(url)
    if parts.scheme not in ("ws", "wss"):
        raise WebSocketError(f"not a ws/wss URL: {parts.scheme or 'no scheme'}")
    secure = parts.scheme == "wss"
    host = parts.hostname or ""
    if not host:
        raise WebSocketError("the WebSocket URL carries no host")
    port = parts.port or (443 if secure else 80)
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"

    context = ssl.create_default_context() if secure else None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=context), timeout=timeout
        )
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise WebSocketError(f"could not open a TCP connection to {host}:{port} within {timeout:.0f}s") from exc
    except OSError as exc:
        raise WebSocketError(f"could not connect to {host}:{port}: {exc}") from exc

    key = base64.b64encode(os.urandom(16)).decode("ascii")
    host_header = host if port in (443, 80) else f"{host}:{port}"
    lines = [
        f"GET {target} HTTP/1.1",
        f"Host: {host_header}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
        "User-Agent: xpst-chat/1",
    ]
    for name, value in (extra_headers or {}).items():
        lines.append(f"{name}: {value}")
    request = "\r\n".join(lines) + "\r\n\r\n"
    try:
        writer.write(request.encode("ascii"))
        await writer.drain()
        header_bytes, leftover = await asyncio.wait_for(_read_headers(reader), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        writer.close()
        raise WebSocketError(f"handshake with {host} timed out after {timeout:.0f}s") from exc
    except OSError as exc:  # pragma: no cover - transport already broken
        writer.close()
        raise WebSocketError(f"handshake write failed: {exc}") from exc

    status, headers = parse_handshake_response(header_bytes)
    if status != 101:
        writer.close()
        raise WebSocketError(f"the server refused the WebSocket upgrade with HTTP {status}")
    expected = accept_key(key)
    if headers.get("sec-websocket-accept") != expected:
        writer.close()
        raise WebSocketError("the server's Sec-WebSocket-Accept does not match the RFC 6455 hash")
    socket = WebSocket(reader, writer)
    if leftover:
        # A fast peer sends its first frame in the same segment as the
        # handshake response. Those bytes are already off the socket, so
        # dropping them would lose the first message — this is exactly the bug
        # the loopback test caught.
        socket.prefeed(leftover)
    return socket


async def _read_headers(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    """The header block, plus any payload bytes that arrived with it."""
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = await reader.read(_READ_CHUNK)
        if not chunk:
            raise WebSocketError("the server closed the connection during the handshake")
        buffer += chunk
        if len(buffer) > 65_536:
            raise WebSocketError("handshake response headers exceeded 64 KiB")
    head, _, rest = buffer.partition(b"\r\n\r\n")
    return head, rest


def parse_handshake_response(raw: bytes) -> tuple[int, dict[str, str]]:
    """Status code and lowercased headers from an HTTP upgrade response."""
    text = raw.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    lines = text.split("\r\n")
    status_line = lines[0] if lines else ""
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/"):
        raise WebSocketError(f"malformed handshake status line: {status_line[:120]!r}")
    try:
        status = int(parts[1])
    except ValueError as exc:
        raise WebSocketError(f"malformed handshake status code: {parts[1]!r}") from exc
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return status, headers
