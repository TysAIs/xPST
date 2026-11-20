"""Read-only chat aggregation: transports, parsers, refusals and the merge.

Nothing here touches a network. Twitch is driven through an injected fake
socket, YouTube through injected ``list_*`` callables and Kick through injected
``http_get``/``http_post``/``ws_connect`` — which is the point of the design:
every branch is provable on a host with no chat credentials at all.

What these tests are guarding, beyond the happy path:

* one ordered, source-tagged timeline — not three panes, and not a merge that
  silently drops a platform;
* a platform that could not be read is reported with a code and a next step.
  "Nobody is streaming", "xPST has no credential" and "that channel does not
  exist" are three different answers and must not collapse into an empty list;
* read-only is structural: no reader has a send path, and the only frames the
  Kick client emits are connect/subscribe/heartbeat;
* the RFC 6455 client — which exists only because Kick offers no other
  transport — frames and unmasks correctly, including over a real loopback
  socket.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from xpst.chat import kick as kick_reader
from xpst.chat import twitch as twitch_reader
from xpst.chat import ws as ws_module
from xpst.chat import youtube as youtube_reader
from xpst.chat.aggregate import chat_readiness, collect_chat, resolve_channels
from xpst.chat.model import (
    CREDENTIALS_MISSING,
    NETWORK_ERROR,
    NO_CHANNEL,
    NOT_LIVE,
    NOT_SUPPORTED,
    UNVERIFIED_PATH,
    ChatError,
    ChatMessage,
    Timeline,
    iso_from_epoch_ms,
    iso_from_text,
)

# ── model ──────────────────────────────────────────────────────────────────


def test_epoch_ms_is_normalised_to_utc_iso() -> None:
    assert iso_from_epoch_ms("1700000000123") == "2023-11-14T22:13:20+00:00"
    assert iso_from_epoch_ms(1700000000123) == "2023-11-14T22:13:20+00:00"


@pytest.mark.parametrize("value", ["", None, "not-a-number", 0, -5, "0"])
def test_unparseable_epoch_is_empty_and_never_redated(value: Any) -> None:
    assert iso_from_epoch_ms(value) == ""


def test_iso_text_normalises_z_suffix_and_garbage() -> None:
    assert iso_from_text("2026-11-16T10:00:00Z") == "2026-11-16T10:00:00+00:00"
    assert iso_from_text("2026-11-16T10:00:00.123456Z") == "2026-11-16T10:00:00+00:00"
    assert iso_from_text("") == ""
    assert iso_from_text("yesterday") == ""


def test_timeline_deduplicates_by_platform_message_id() -> None:
    timeline = Timeline(limit=10)
    first = ChatMessage(platform="twitch", author="a", text="hi", message_id="1", timestamp="2026-11-16T10:00:00+00:00")
    duplicate = ChatMessage(platform="twitch", author="a", text="hi again", message_id="1")
    other_platform = ChatMessage(platform="kick", author="a", text="hi", message_id="1")
    assert timeline.add(first) is True
    assert timeline.add(duplicate) is False
    assert timeline.add(other_platform) is True
    assert len(timeline.messages) == 2


def test_timeline_drops_the_oldest_when_it_exceeds_the_limit() -> None:
    timeline = Timeline(limit=2)
    for index in range(3):
        timeline.add(
            ChatMessage(
                platform="twitch",
                author="a",
                text=str(index),
                message_id=str(index),
                timestamp=f"2026-11-16T10:00:0{index}+00:00",
            )
        )
    assert [message.text for message in timeline.ordered()] == ["1", "2"]
    assert timeline.dropped == 1


def test_timeline_orders_across_platforms_and_puts_unknown_times_last() -> None:
    timeline = Timeline(limit=10)
    timeline.add(ChatMessage(platform="kick", author="k", text="second", timestamp="2026-11-16T10:00:05+00:00"))
    timeline.add(ChatMessage(platform="twitch", author="t", text="first", timestamp="2026-11-16T10:00:01+00:00"))
    timeline.add(ChatMessage(platform="youtube", author="y", text="undated", timestamp=""))
    assert [message.text for message in timeline.ordered()] == ["first", "second", "undated"]
    assert timeline.counts()["total"] == 3


# ── WebSocket client (RFC 6455) ────────────────────────────────────────────


def test_accept_key_matches_the_rfc_example() -> None:
    # RFC 6455 §1.3: this exact pair is in the specification.
    assert ws_module.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_encode_frame_sets_fin_opcode_mask_and_length_width() -> None:
    for payload, width in ((b"hi", 2), (b"x" * 200, 126), (b"y" * 70000, 127)):
        frame = ws_module.encode_frame(payload, mask=b"\x01\x02\x03\x04")
        assert frame[0] == 0x81
        assert frame[1] & 0x80, "a client frame must be masked"
        assert frame[1] & 0x7F == width
        buffer = ws_module.FrameBuffer()
        buffer.feed(frame)
        assert buffer.next_frame() == (True, ws_module.OP_TEXT, payload)


def test_frame_buffer_handles_fragmentation_and_control_frames() -> None:
    buffer = ws_module.FrameBuffer()
    buffer.feed(ws_module.encode_frame(b"Hel", opcode=ws_module.OP_TEXT, fin=False, mask=b"\x00\x00\x00\x00"))
    assert buffer.next_frame() == (False, ws_module.OP_TEXT, b"Hel")
    buffer.feed(ws_module.encode_frame(b"", opcode=ws_module.OP_PING, mask=b"\x00\x00\x00\x00"))
    assert buffer.next_frame() == (True, ws_module.OP_PING, b"")
    buffer.feed(ws_module.encode_frame(b"lo", opcode=ws_module.OP_CONTINUATION, mask=b"\x00\x00\x00\x00"))
    assert buffer.next_frame() == (True, ws_module.OP_CONTINUATION, b"lo")


def test_frame_buffer_returns_none_until_a_frame_is_complete() -> None:
    buffer = ws_module.FrameBuffer()
    buffer.feed(b"\x81")
    assert buffer.next_frame() is None
    buffer.feed(b"\x05hel")
    assert buffer.next_frame() is None
    buffer.feed(b"lo")
    assert buffer.next_frame() == (True, ws_module.OP_TEXT, b"hello")


def test_frame_buffer_rejects_reserved_bits_and_oversized_frames() -> None:
    reserved = ws_module.FrameBuffer()
    reserved.feed(b"\xc1\x01x")  # RSV1 set with no extension negotiated
    with pytest.raises(ws_module.WebSocketError):
        reserved.next_frame()

    oversized = ws_module.FrameBuffer(max_frame_bytes=8)
    oversized.feed(bytes([0x81, 20]) + b"z" * 20)
    with pytest.raises(ws_module.WebSocketError):
        oversized.next_frame()


def test_handshake_response_parsing_rejects_nonsense() -> None:
    status, headers = ws_module.parse_handshake_response(
        b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: abc\r\n\r\n"
    )
    assert status == 101
    assert headers["sec-websocket-accept"] == "abc"
    with pytest.raises(ws_module.WebSocketError):
        ws_module.parse_handshake_response(b"garbage\r\n\r\n")


def _server_frame(payload: bytes, opcode: int = ws_module.OP_TEXT) -> bytes:
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    else:  # pragma: no cover - the loopback test sends short frames
        header.append(126)
        header.extend(length.to_bytes(2, "big"))
    return bytes(header) + payload


def _unmask_client_frame(data: bytes) -> tuple[int, bytes]:
    assert data[1] & 0x80, "the client must mask every frame it sends"
    length = data[1] & 0x7F
    offset = 2
    if length == 126:  # pragma: no cover - short frames only in this test
        length = int.from_bytes(data[2:4], "big")
        offset = 4
    mask = data[offset : offset + 4]
    payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data[offset + 4 : offset + 4 + length]))
    return data[0] & 0x0F, payload


@pytest.mark.asyncio
async def test_websocket_client_completes_a_handshake_and_exchanges_frames() -> None:
    seen: dict[str, Any] = {}

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw = b""
        while b"\r\n\r\n" not in raw:
            raw += await reader.read(4096)
        request = raw.decode("latin-1")
        seen["request"] = request
        key_line = next(line for line in request.split("\r\n") if line.lower().startswith("sec-websocket-key:"))
        client_key = key_line.split(":", 1)[1].strip()
        writer.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {ws_module.accept_key(client_key)}\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        writer.write(_server_frame(b'{"hello":"world"}'))
        await writer.drain()
        opcode, payload = _unmask_client_frame(await reader.read(4096))
        seen["client"] = (opcode, payload)
        writer.write(_server_frame(b"", opcode=ws_module.OP_PING))
        await writer.drain()
        opcode, payload = _unmask_client_frame(await reader.read(4096))
        seen["pong"] = (opcode, payload)
        writer.write(_server_frame(b"", opcode=ws_module.OP_CLOSE))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        socket = await ws_module.connect(f"ws://127.0.0.1:{port}/connection/websocket", timeout=10)
        assert await socket.recv(timeout=5) == '{"hello":"world"}'
        await socket.send_text("from the client")
        with pytest.raises(ws_module.WebSocketClosedError):
            await socket.recv(timeout=5)
        await socket.close()
    finally:
        server.close()
        await server.wait_closed()

    assert "GET /connection/websocket HTTP/1.1" in seen["request"]
    assert "Sec-WebSocket-Version: 13" in seen["request"]
    assert seen["client"] == (ws_module.OP_TEXT, b"from the client")
    assert seen["pong"] == (ws_module.OP_PONG, b"")


@pytest.mark.asyncio
async def test_websocket_client_refuses_a_mismatched_accept_key() -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw = b""
        while b"\r\n\r\n" not in raw:
            raw += await reader.read(4096)
        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: not-the-right-hash\r\n\r\n"
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(ws_module.WebSocketError):
            await ws_module.connect(f"ws://127.0.0.1:{port}/", timeout=10)
    finally:
        server.close()
        await server.wait_closed()


# ── Twitch IRC ─────────────────────────────────────────────────────────────

TWITCH_PRIVMSG = (
    "@badge-info=;badges=;client-nonce=abc;color=#1E90FF;display-name=FakeViewer;emotes=;"
    "first-msg=0;flags=;id=11111111-2222-3333-4444-555555555555;mod=0;room-id=42;"
    "subscriber=0;tmi-sent-ts=1700000000123;turbo=0;user-id=7;user-type= "
    ":fakeviewer!fakeviewer@fakeviewer.tmi.twitch.tv PRIVMSG #examplechannel :hello there"
)


def test_irc_tags_prefix_and_trailing_are_parsed() -> None:
    line = twitch_reader.parse_irc_line(TWITCH_PRIVMSG)
    assert line is not None
    assert line.command == "PRIVMSG"
    assert line.tags["display-name"] == "FakeViewer"
    assert line.tags["tmi-sent-ts"] == "1700000000123"
    assert line.prefix.startswith("fakeviewer!")
    assert line.params == ["#examplechannel"]
    assert line.trailing == "hello there"


def test_irc_tag_escapes_are_decoded() -> None:
    line = twitch_reader.parse_irc_line(r"@display-name=two\sswords;x=a\:b :n!n@n PRIVMSG #c :hi")
    assert line is not None
    assert line.tags["display-name"] == "two swords"
    assert line.tags["x"] == "a;b"


def test_privmsg_becomes_a_chat_message_and_other_commands_do_not() -> None:
    parsed = twitch_reader.parse_irc_line(TWITCH_PRIVMSG)
    assert parsed is not None
    message = twitch_reader.message_from_line(parsed)
    assert message is not None
    assert message.platform == "twitch"
    assert message.author == "FakeViewer"
    assert message.text == "hello there"
    assert message.channel == "examplechannel"
    assert message.message_id == "11111111-2222-3333-4444-555555555555"
    assert message.timestamp == "2023-11-14T22:13:20+00:00"

    join = twitch_reader.parse_irc_line(":fakeviewer!fakeviewer@fakeviewer.tmi.twitch.tv JOIN #examplechannel")
    assert join is not None
    assert twitch_reader.message_from_line(join) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("examplechannel", "examplechannel"),
        ("#examplechannel", "examplechannel"),
        ("https://www.twitch.tv/examplechannel", "examplechannel"),
        ("twitch.tv/examplechannel/videos", "examplechannel"),
        ("  ExampleChannel  ", "examplechannel"),
        ("", ""),
    ],
)
def test_channel_names_are_normalised(raw: str, expected: str) -> None:
    assert twitch_reader._normalise_channel(raw) == expected


class FakeWriter:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    @property
    def text(self) -> str:
        return b"".join(self.written).decode("utf-8", "replace")


class FakeReader:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    async def read(self, size: int = -1) -> bytes:  # pragma: no cover - unused
        return b""


async def _twitch_connect_returning(lines: list[bytes]) -> Any:
    writer = FakeWriter()

    async def _connect(host: str, port: int) -> Any:
        return FakeReader(lines), writer

    _connect.writer = writer  # type: ignore[attr-defined]
    return _connect


@pytest.mark.asyncio
async def test_twitch_reader_collects_messages_answers_ping_and_confirms_the_join() -> None:
    lines = [
        b":tmi.twitch.tv 001 justinfan12345 :Welcome, GLHF!\r\n",
        b"PING :tmi.twitch.tv\r\n",
        b":justinfan12345.tmi.twitch.tv 366 justinfan12345 #examplechannel :End of /NAMES list\r\n",
        TWITCH_PRIVMSG.encode() + b"\r\n",
    ]
    factory = await _twitch_connect_returning(lines)
    outcome = await twitch_reader.read_twitch_chat(["examplechannel"], 5.0, connect=factory)
    assert [message.text for message in outcome.messages] == ["hello there"]
    assert outcome.unconfirmed == []
    assert "PONG" in factory.writer.text  # type: ignore[attr-defined]
    assert "JOIN #examplechannel" in factory.writer.text  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_twitch_reader_calls_a_silent_channel_unconfirmed_not_quiet() -> None:
    factory = await _twitch_connect_returning([b":tmi.twitch.tv 001 justinfan12345 :Welcome\r\n"])
    outcome = await twitch_reader.read_twitch_chat(["nosuchchannel"], 5.0, connect=factory)
    assert outcome.messages == []
    assert outcome.unconfirmed == ["nosuchchannel"]
    assert "silently drops" in outcome.note


@pytest.mark.asyncio
async def test_twitch_reader_refuses_without_a_channel_and_on_connect_failure() -> None:
    with pytest.raises(ChatError) as no_channel:
        await twitch_reader.read_twitch_chat([], 5.0)
    assert no_channel.value.code == NO_CHANNEL

    async def broken(host: str, port: int) -> Any:
        raise OSError("connection refused")

    with pytest.raises(ChatError) as refused:
        await twitch_reader.read_twitch_chat(["examplechannel"], 5.0, connect=broken)
    assert refused.value.code == NETWORK_ERROR


# ── YouTube ────────────────────────────────────────────────────────────────


def test_resolve_chat_id_from_a_live_video() -> None:
    video_id, chat_id = youtube_reader.resolve_live_chat_id(
        video_id="vid1",
        list_videos=lambda _vid: {"items": [{"liveStreamingDetails": {"activeLiveChatId": "chat-1"}}]},
    )
    assert (video_id, chat_id) == ("vid1", "chat-1")


def test_a_video_without_an_active_chat_is_not_live_not_empty() -> None:
    with pytest.raises(ChatError) as excinfo:
        youtube_reader.resolve_live_chat_id(
            video_id="vid1", list_videos=lambda _vid: {"items": [{"liveStreamingDetails": {}}]}
        )
    assert excinfo.value.code == NOT_LIVE


def test_no_active_broadcast_is_not_live() -> None:
    with pytest.raises(ChatError) as excinfo:
        youtube_reader.resolve_live_chat_id(list_broadcasts=lambda: {"items": []})
    assert excinfo.value.code == NOT_LIVE
    assert "Go live" in excinfo.value.action


def test_active_broadcast_yields_its_chat_id() -> None:
    video_id, chat_id = youtube_reader.resolve_live_chat_id(
        list_broadcasts=lambda: {
            "items": [
                {"id": "vid-done", "snippet": {"liveBroadcastContent": "none"}},
                {"id": "vid9", "snippet": {"liveBroadcastContent": "live", "liveChatId": "chat-9"}},
            ]
        }
    )
    assert (video_id, chat_id) == ("vid9", "chat-9")


def test_a_known_but_offline_broadcast_is_not_live() -> None:
    with pytest.raises(ChatError) as excinfo:
        youtube_reader.resolve_live_chat_id(
            list_broadcasts=lambda: {"items": [{"id": "vid1", "snippet": {"liveBroadcastContent": "none"}}]}
        )
    assert excinfo.value.code == NOT_LIVE
    assert "none of the channel's 1 broadcast" in str(excinfo.value)


def test_broadcasts_reader_never_combines_mine_with_broadcast_status() -> None:
    """Pinned: ``mine`` + ``broadcastStatus`` is HTTP 400 'incompatible' (measured live)."""

    class FakeRequest:
        def __init__(self, recorded: dict[str, Any]) -> None:
            self.recorded = recorded

        def execute(self) -> dict[str, Any]:
            return {"items": []}

    class FakeBroadcasts:
        def __init__(self, recorded: dict[str, Any]) -> None:
            self.recorded = recorded

        def list(self, **kwargs: Any) -> FakeRequest:
            self.recorded.update(kwargs)
            return FakeRequest(self.recorded)

    class FakeService:
        def __init__(self, recorded: dict[str, Any]) -> None:
            self._recorded = recorded

        def liveBroadcasts(self) -> FakeBroadcasts:  # noqa: N802 - mirrors the vendor API
            return FakeBroadcasts(self._recorded)

    recorded: dict[str, Any] = {}
    youtube_reader.broadcasts_reader(FakeService(recorded))()
    assert recorded["mine"] is True
    assert "broadcastStatus" not in recorded


def test_a_400_is_reported_as_an_xpst_defect_not_an_auth_problem() -> None:
    class HttpError(Exception):
        def __init__(self, status: int) -> None:
            super().__init__(f"<HttpError {status} returned 'Incompatible parameters'>")
            self.resp = type("Resp", (), {"status": status})()

    def boom() -> dict[str, Any]:
        raise HttpError(400)

    with pytest.raises(ChatError) as excinfo:
        youtube_reader.resolve_live_chat_id(list_broadcasts=boom)
    assert excinfo.value.status == 400
    assert "defect" in excinfo.value.action
    assert "Re-authorise" not in excinfo.value.action


def test_a_channel_that_is_not_live_enabled_is_its_own_code() -> None:
    """The live verdict measured on this host: 403 'not enabled for live streaming'."""

    class HttpError(Exception):
        def __init__(self) -> None:
            super().__init__(
                "<HttpError 403 when requesting .../liveBroadcasts?part=id,snippet&mine=true "
                'returned "The user is not enabled for live streaming.">'
            )
            self.resp = type("Resp", (), {"status": 403})()

    def boom() -> dict[str, Any]:
        raise HttpError()

    with pytest.raises(ChatError) as excinfo:
        youtube_reader.resolve_live_chat_id(list_broadcasts=boom)
    assert excinfo.value.code == youtube_reader.LIVE_NOT_ENABLED
    assert "youtube.com/features" in excinfo.value.action
    assert "not an OAuth problem" in excinfo.value.action


def test_messages_without_display_text_are_not_rows() -> None:
    payload = {
        "items": [
            {"id": "a", "snippet": {"displayMessage": "hello", "publishedAt": "2026-11-16T10:00:00Z"},
             "authorDetails": {"displayName": "FakeUser"}},
            {"id": "b", "snippet": {"displayMessage": ""}, "authorDetails": {"displayName": "Deleted"}},
        ]
    }
    messages = youtube_reader.messages_from_response(payload, "vid1")
    assert [message.text for message in messages] == ["hello"]
    assert messages[0].author == "FakeUser"
    assert messages[0].channel == "vid1"


@pytest.mark.asyncio
async def test_youtube_reader_carries_the_page_token_and_stops_when_offline() -> None:
    calls: list[str | None] = []

    def list_messages(token: str | None) -> dict[str, Any]:
        calls.append(token)
        if token is None:
            return {
                "items": [
                    {"id": "1", "snippet": {"displayMessage": "first", "publishedAt": "2026-11-16T10:00:00Z"},
                     "authorDetails": {"displayName": "a"}}
                ],
                "nextPageToken": "token-2",
                "pollingIntervalMillis": 10,
            }
        return {"items": [], "offlineAt": "2026-11-16T10:05:00Z"}

    outcome = await youtube_reader.read_youtube_chat(
        list_messages=list_messages, video_id="vid1", duration_s=5.0, poll_interval_s=0.01, min_poll_interval_s=0.0
    )
    assert [message.text for message in outcome.messages] == ["first"]
    assert calls == [None, "token-2"]
    assert "quota unit" in outcome.note
    assert "went offline" in outcome.note


@pytest.mark.asyncio
async def test_youtube_reader_turns_a_vendor_failure_into_a_coded_error() -> None:
    class BoomError(Exception):
        pass

    def list_messages(_token: str | None) -> dict[str, Any]:
        raise BoomError("403 quota exceeded")

    with pytest.raises(ChatError) as excinfo:
        await youtube_reader.read_youtube_chat(list_messages=list_messages, video_id="vid1", duration_s=1.0)
    assert excinfo.value.code == "platform_error"
    assert "quota" in excinfo.value.action


def test_missing_youtube_service_refuses_with_the_connect_step() -> None:
    with pytest.raises(ChatError) as excinfo:
        youtube_reader.require_service(None)
    assert excinfo.value.code == CREDENTIALS_MISSING
    assert "xpst auth youtube" in excinfo.value.action


# ── Kick ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("somechannel", "somechannel"),
        ("@somechannel", "somechannel"),
        ("https://kick.com/somechannel", "somechannel"),
        ("  SomeChannel  ", "somechannel"),
        ("", ""),
    ],
)
def test_kick_slugs_are_normalised(raw: str, expected: str) -> None:
    assert kick_reader.channel_slug(raw) == expected


def test_kick_chatroom_id_is_read_from_the_channel_payload() -> None:
    assert kick_reader.chatroom_id_from_payload({"chatroom": {"id": 12345}}) == 12345
    with pytest.raises(ChatError) as excinfo:
        kick_reader.chatroom_id_from_payload({"chatroom": {}})
    assert excinfo.value.code == UNVERIFIED_PATH
    assert "docs.kick.com" in excinfo.value.docs_url


def test_kick_publication_becomes_a_message_and_ignores_non_chat_frames() -> None:
    frame = {
        "push": {
            "channel": "chatrooms.999.v2",
            "pub": {
                "data": {
                    "id": "msg-1",
                    "content": "gg",
                    "created_at": "2026-11-16T10:00:00Z",
                    "sender": {"username": "FakeKickUser", "slug": "fakekickuser"},
                }
            },
        }
    }
    message = kick_reader.message_from_publication(frame, slug_by_room={999: "examplechannel"})
    assert message is not None
    assert message.platform == "kick"
    assert message.author == "FakeKickUser"
    assert message.text == "gg"
    assert message.channel == "examplechannel"

    assert kick_reader.message_from_publication({"id": 2, "subscribe": {}}) is None


class FakeSocket:
    """A scripted realtime socket: yields frames, records what the client sent."""

    def __init__(self, frames: list[str]) -> None:
        self.frames = list(frames)
        self.sent: list[str] = []
        self.closed = False

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def recv(self, timeout: float | None = None) -> str:
        if not self.frames:
            await asyncio.sleep(0.01)
            raise asyncio.TimeoutError
        return self.frames.pop(0)

    async def close(self) -> None:
        self.closed = True


async def _kick_fakes(
    *,
    chatroom_id: int = 999,
    frames: list[str] | None = None,
    channel_status: int = 200,
) -> tuple[Any, Any, Any, FakeSocket]:
    async def http_get(_url: str) -> tuple[int, Any]:
        if channel_status != 200:
            return channel_status, {}
        return 200, {"chatroom": {"id": chatroom_id}}

    async def http_post(_url: str, _body: dict[str, Any]) -> tuple[int, Any]:
        return 200, {"data": {"token": "fake-guest-token"}, "message": "success"}

    socket = FakeSocket(
        frames
        if frames is not None
        else [
            json.dumps({"id": 1, "connect": {"client": "x", "ttl": 2700}}),
            json.dumps({"id": 2, "subscribe": {}}),
        ]
    )

    async def ws_connect(_url: str) -> FakeSocket:
        return socket

    return http_get, http_post, ws_connect, socket


@pytest.mark.asyncio
async def test_kick_reader_connects_subscribes_and_answers_the_heartbeat() -> None:
    http_get, http_post, ws_connect, socket = await _kick_fakes(
        frames=[
            json.dumps({"id": 1, "connect": {"client": "x"}}),
            json.dumps({"id": 2, "subscribe": {}}),
            "{}",
            json.dumps(
                {
                    "push": {
                        "channel": "chatrooms.999.v2",
                        "pub": {
                            "data": {
                                "id": "m1",
                                "content": "hello kick",
                                "created_at": "2026-11-16T10:00:00Z",
                                "sender": {"username": "FakeKickUser"},
                            }
                        },
                    }
                }
            ),
        ]
    )
    outcome = await kick_reader.read_kick_chat(
        ["examplechannel"], 0.5, http_get=http_get, http_post=http_post, ws_connect=ws_connect
    )
    assert [message.text for message in outcome.messages] == ["hello kick"]
    assert outcome.channels == ["examplechannel"]
    assert "{}" in socket.sent, "the Centrifugo heartbeat must be answered"
    assert json.loads(socket.sent[0])["connect"]["name"] == "js"
    assert "chatrooms.999.v2" in socket.sent[1]
    assert socket.closed is True
    assert outcome.note and "unverified" in outcome.note


@pytest.mark.asyncio
async def test_kick_reader_reports_an_unknown_channel_as_unconfirmed() -> None:
    http_get, http_post, ws_connect, _socket = await _kick_fakes(channel_status=404)
    with pytest.raises(ChatError) as excinfo:
        await kick_reader.read_kick_chat(
            ["nosuchchannel"], 0.5, http_get=http_get, http_post=http_post, ws_connect=ws_connect
        )
    assert excinfo.value.code == NO_CHANNEL


@pytest.mark.asyncio
async def test_kick_reader_reports_a_refused_connection_as_the_unverified_path() -> None:
    async def http_get(_url: str) -> tuple[int, Any]:
        return 200, {"chatroom": {"id": 999}}

    async def http_post(_url: str, _body: dict[str, Any]) -> tuple[int, Any]:
        return 200, {"data": {"token": "fake"}}

    socket = FakeSocket([json.dumps({"id": 1, "connect": {"error": {"code": 4001, "message": "bad token"}}})])

    async def ws_connect(_url: str) -> FakeSocket:
        return socket

    with pytest.raises(ChatError) as excinfo:
        await kick_reader.read_kick_chat(
            ["examplechannel"], 0.5, http_get=http_get, http_post=http_post, ws_connect=ws_connect
        )
    assert excinfo.value.code == UNVERIFIED_PATH
    assert "bad token" in str(excinfo.value)
    assert "webhook" in excinfo.value.action


@pytest.mark.asyncio
async def test_kick_reader_refuses_when_the_subscription_is_never_acknowledged() -> None:
    http_get, http_post, ws_connect, _socket = await _kick_fakes(frames=["{}", "{}", "{}"])
    with pytest.raises(ChatError) as excinfo:
        await kick_reader.read_kick_chat(
            ["examplechannel"],
            0.5,
            http_get=http_get,
            http_post=http_post,
            ws_connect=ws_connect,
            subscribe_timeout_s=0.2,
        )
    assert excinfo.value.code == UNVERIFIED_PATH


@pytest.mark.asyncio
async def test_kick_guest_token_refusal_is_coded() -> None:
    async def http_post(_url: str, _body: dict[str, Any]) -> tuple[int, Any]:
        return 503, {}

    with pytest.raises(ChatError) as excinfo:
        await kick_reader.fetch_guest_token(http_post=http_post)
    assert excinfo.value.code == UNVERIFIED_PATH
    assert excinfo.value.status == 503


# ── aggregate ──────────────────────────────────────────────────────────────


def _twitch_line(author: str, text: str, message_id: str, epoch_ms: int) -> bytes:
    return (
        f"@display-name={author};id={message_id};tmi-sent-ts={epoch_ms} "
        f":{author.lower()}!{author.lower()}@x.tmi.twitch.tv PRIVMSG #examplechannel :{text}\r\n"
    ).encode()


@pytest.mark.asyncio
async def test_one_merged_timeline_across_platforms_with_per_platform_refusals() -> None:
    lines = [
        b":justinfan1.tmi.twitch.tv 366 justinfan1 #examplechannel :End of /NAMES list\r\n",
        _twitch_line("FakeViewer", "twitch second", "t2", 1700000005000),
        _twitch_line("FakeViewer", "twitch first", "t1", 1700000001000),
    ]
    twitch_connect = await _twitch_connect_returning(lines)
    http_get, http_post, ws_connect, _socket = await _kick_fakes(
        frames=[
            json.dumps({"id": 1, "connect": {"client": "x"}}),
            json.dumps({"id": 2, "subscribe": {}}),
            json.dumps(
                {
                    "push": {
                        "channel": "chatrooms.999.v2",
                        "pub": {
                            "data": {
                                "id": "k1",
                                "content": "kick between",
                                "created_at": "2023-11-14T22:13:23Z",
                                "sender": {"username": "FakeKickUser"},
                            }
                        },
                    }
                }
            ),
        ]
    )
    payload = await collect_chat(
        None,
        config_dir="/nonexistent",
        platforms=["twitch", "kick", "youtube"],
        duration_s=1.0,
        twitch_channels=["examplechannel"],
        kick_channels=["examplechannel"],
        twitch_connect=twitch_connect,
        kick_http_get=http_get,
        kick_http_post=http_post,
        kick_ws_connect=ws_connect,
    )
    assert payload["ok"] is True, "two platforms answered, so the read succeeded"
    assert [message["text"] for message in payload["messages"]] == ["twitch first", "kick between", "twitch second"]
    assert [message["platform"] for message in payload["messages"]] == ["twitch", "kick", "twitch"]
    assert payload["counts"] == {"twitch": 2, "youtube": 0, "kick": 1, "total": 3}
    assert {row["platform"] for row in payload["unavailable"]} == {"youtube"}
    assert payload["unavailable"][0]["code"] == CREDENTIALS_MISSING
    assert payload["read_only"] is True
    assert payload["window"]["served_s"] == 1.0


@pytest.mark.asyncio
async def test_every_platform_unreadable_is_a_failed_read_not_an_empty_feed() -> None:
    async def broken_twitch(host: str, port: int) -> Any:
        raise OSError("refused")

    payload = await collect_chat(
        None,
        config_dir="/nonexistent",
        platforms=["twitch"],
        duration_s=1.0,
        twitch_channels=["examplechannel"],
        twitch_connect=broken_twitch,
    )
    assert payload["ok"] is False
    assert payload["messages"] == []
    assert payload["code"] == NETWORK_ERROR
    assert payload["unavailable"][0]["platform"] == "twitch"


@pytest.mark.asyncio
async def test_a_platform_that_was_not_asked_about_is_not_reported() -> None:
    lines = [b":justinfan1.tmi.twitch.tv 366 justinfan1 #examplechannel :End of /NAMES list\r\n"]
    twitch_connect = await _twitch_connect_returning(lines)
    payload = await collect_chat(
        None,
        platforms=["twitch"],
        duration_s=1.0,
        twitch_channels=["examplechannel"],
        twitch_connect=twitch_connect,
    )
    assert payload["ok"] is True
    assert [row["platform"] for row in payload["platforms"]] == ["twitch"]
    assert payload["unavailable"] == []


@pytest.mark.asyncio
async def test_an_unknown_platform_is_refused_by_name() -> None:
    payload = await collect_chat(None, platforms=["myspace"], duration_s=1.0)
    assert payload["ok"] is False
    assert payload["unavailable"][0]["code"] == NOT_SUPPORTED
    assert "twitch" in payload["unavailable"][0]["action"]


@pytest.mark.asyncio
async def test_the_timeline_is_bounded_and_reports_truncation() -> None:
    lines = [b":justinfan1.tmi.twitch.tv 366 justinfan1 #examplechannel :End of /NAMES list\r\n"] + [
        _twitch_line("FakeViewer", f"m{index}", f"id{index}", 1700000000000 + index * 1000) for index in range(5)
    ]
    twitch_connect = await _twitch_connect_returning(lines)
    payload = await collect_chat(
        None,
        platforms=["twitch"],
        duration_s=1.0,
        limit=3,
        twitch_channels=["examplechannel"],
        twitch_connect=twitch_connect,
    )
    assert len(payload["messages"]) == 3
    assert payload["truncated"] is True
    assert payload["dropped"] == 2
    assert [message["text"] for message in payload["messages"]] == ["m2", "m3", "m4"]


def test_readiness_separates_a_missing_channel_from_a_missing_credential(tmp_path: Any) -> None:
    readiness = chat_readiness(str(tmp_path))
    by_platform = {row["platform"]: row for row in readiness["platforms"]}
    assert by_platform["twitch"]["missing"] == ["channel"]
    assert by_platform["kick"]["missing"] == ["channel"]
    assert by_platform["youtube"]["missing"] == ["oauth_token"]
    assert by_platform["youtube"]["status"] == "unavailable"
    codes = {row["platform"]: row["code"] for row in readiness["unavailable"]}
    assert codes["twitch"] == NO_CHANNEL
    assert codes["youtube"] == CREDENTIALS_MISSING
    assert readiness["read_only"] is True


def test_configured_channels_are_read_from_config_and_arguments_win() -> None:
    class FakeChat:
        twitch_channels = ["from-config"]
        kick_channels = ["kick-config"]
        youtube_video_id = "vid-config"

    class FakeConfig:
        chat = FakeChat()

    assert resolve_channels(FakeConfig()) == (["from-config"], ["kick-config"], "vid-config")
    assert resolve_channels(FakeConfig(), twitch_channels=["explicit"])[0] == ["explicit"]
    assert resolve_channels(FakeConfig(), twitch_channels=[])[0] == []
    assert resolve_channels(None) == ([], [], "")


def test_registry_rows_never_claim_a_verified_path_that_is_not(tmp_path: Any) -> None:
    readiness = chat_readiness(str(tmp_path))
    rows = {row["platform"]: row for row in readiness["registry"]}
    assert rows["twitch"]["verified"] is True
    assert rows["kick"]["verified"] is False
    assert rows["youtube"]["verified"] is False
    assert "realtime" in rows["kick"]["endpoint"]
    assert rows["kick"]["transport"] == "realtime_ws"
    assert rows["twitch"]["transport"] == "irc_tls"


# ── surfaces: CLI, MCP, HTTP all serve the one payload ─────────────────────


def _config_dir(tmp_path: Any) -> Any:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text("accounts:\n  youtube:\n    enabled: false\n", encoding="utf-8")
    return config_dir


def _chat_channels_config(tmp_path: Any) -> Any:
    config_dir = tmp_path / "cfgchat"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(
        "chat:\n  twitch_channels: ['examplechannel']\n  kick_channels: []\n  max_messages: 250\n",
        encoding="utf-8",
    )
    return config_dir


def test_cli_chat_platforms_serves_the_readiness_payload(tmp_path: Any) -> None:
    from click.testing import CliRunner

    from xpst.cli import main

    config_dir = _config_dir(tmp_path)
    result = CliRunner().invoke(
        main, ["--config", str(config_dir / "config.yaml"), "chat", "platforms", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["read_only"] is True
    assert {row["platform"] for row in payload["platforms"]} == {"twitch", "youtube", "kick"}
    assert payload["unavailable"]


def test_cli_chat_timeline_prints_the_merged_feed_as_json(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    import xpst.chat as chat_package
    from xpst.cli import main

    captured: dict[str, Any] = {}

    def fake_timeline(config: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "ok": True,
            "contract_version": 1,
            "read_only": True,
            "window": {"requested_s": kwargs["duration_s"], "served_s": 5.0, "started_at": "", "ended_at": ""},
            "platforms": [],
            "unavailable": [],
            "messages": [
                {
                    "platform": "twitch",
                    "author": "FakeViewer",
                    "text": "hello",
                    "timestamp": "2026-11-16T10:00:00+00:00",
                    "message_id": "1",
                    "channel": "examplechannel",
                }
            ],
            "counts": {"twitch": 1, "youtube": 0, "kick": 0, "total": 1},
            "dropped": 0,
            "truncated": False,
            "limit": kwargs["limit"],
        }

    monkeypatch.setattr(chat_package, "chat_timeline", fake_timeline)
    config_dir = _config_dir(tmp_path)
    result = CliRunner().invoke(
        main,
        [
            "--config",
            str(config_dir / "config.yaml"),
            "chat",
            "timeline",
            "--twitch-channel",
            "examplechannel",
            "--duration",
            "5",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["messages"][0]["text"] == "hello"
    assert captured["twitch_channels"] == ["examplechannel"]
    assert captured["duration_s"] == 5.0


def test_cli_chat_timeline_exits_non_zero_when_nothing_is_readable(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    import xpst.chat as chat_package
    from xpst.cli import EXIT_CONFIG_ERROR, main

    monkeypatch.setattr(
        chat_package,
        "chat_timeline",
        lambda config, **kwargs: {
            "ok": False,
            "contract_version": 1,
            "read_only": True,
            "window": {"requested_s": 1.0, "served_s": 1.0, "started_at": "", "ended_at": ""},
            "platforms": [],
            "unavailable": [{"platform": "twitch", "code": NO_CHANNEL, "message": "no channel", "action": ""}],
            "messages": [],
            "counts": {"twitch": 0, "youtube": 0, "kick": 0, "total": 0},
            "dropped": 0,
            "truncated": False,
            "limit": 500,
            "code": NO_CHANNEL,
        },
    )
    config_dir = _config_dir(tmp_path)
    result = CliRunner().invoke(
        main, ["--config", str(config_dir / "config.yaml"), "chat", "timeline", "--json"]
    )
    assert result.exit_code == EXIT_CONFIG_ERROR


def test_cli_chat_timeline_renders_hostile_chat_text_literally(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chat text is written by strangers: Rich markup must never be interpreted."""
    from click.testing import CliRunner

    import xpst.chat as chat_package
    from xpst.cli import main

    monkeypatch.setattr(
        chat_package,
        "chat_timeline",
        lambda config, **kwargs: {
            "ok": True,
            "contract_version": 1,
            "read_only": True,
            "window": {"requested_s": 1.0, "served_s": 1.0, "started_at": "", "ended_at": ""},
            "platforms": [],
            "unavailable": [],
            "messages": [
                {
                    "platform": "twitch",
                    "author": "[bold]Fake[/bold]",
                    "text": "closing tag [/] and [notatag]",
                    "timestamp": "2026-11-16T10:00:00+00:00",
                    "message_id": "1",
                    "channel": "examplechannel",
                }
            ],
            "counts": {"twitch": 1, "youtube": 0, "kick": 0, "total": 1},
            "dropped": 0,
            "truncated": False,
            "limit": 500,
        },
    )
    config_dir = _config_dir(tmp_path)
    result = CliRunner().invoke(main, ["--config", str(config_dir / "config.yaml"), "chat", "timeline"])
    assert result.exit_code == 0, result.output
    assert "closing tag [/] and [notatag]" in result.output
    assert "MarkupError" not in result.output


def test_cli_chat_commands_exist_in_the_help_surface() -> None:
    from click.testing import CliRunner

    from xpst.cli import main

    group = CliRunner().invoke(main, ["chat", "--help"])
    assert group.exit_code == 0
    assert "timeline" in group.output
    assert "platforms" in group.output


@pytest.mark.asyncio
async def test_mcp_chat_tool_serves_the_readiness_payload(tmp_path: Any) -> None:
    from xpst.mcp import server as mcp_server

    config_dir = _config_dir(tmp_path)
    config = mcp_server.XPSTConfig.load(str(config_dir / "config.yaml"))
    result = await mcp_server._handle_chat(config, {})
    assert mcp_server.result_is_error(result) is False
    payload = json.loads(result.content[0].text)
    assert payload["read_only"] is True
    assert {row["platform"] for row in payload["platforms"]} == {"twitch", "youtube", "kick"}


def test_http_chat_route_serves_the_readiness_payload(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from xpst.dashboard import api as api_module

    config_dir = _config_dir(tmp_path)
    app = FastAPI()
    app.include_router(api_module.create_api_router(str(config_dir)))
    with TestClient(app) as client:
        response = client.get("/api/chat")
        filtered = client.get("/api/chat", params={"platforms": "twitch"})
    assert response.status_code == 200
    assert response.json()["read_only"] is True
    assert [row["platform"] for row in filtered.json()["platforms"]] == ["twitch"]


def test_chat_config_round_trips_through_the_config_file(tmp_path: Any) -> None:
    from xpst.config import XPSTConfig

    config_dir = _chat_channels_config(tmp_path)
    config = XPSTConfig.load(str(config_dir / "config.yaml"))
    assert config.chat.twitch_channels == ["examplechannel"]
    assert config.chat.max_messages == 250
    saved = config.to_dict() if hasattr(config, "to_dict") else None
    if saved is not None:  # pragma: no cover - attribute name differs across surfaces
        assert saved["chat"]["twitch_channels"] == ["examplechannel"]
