"""YouTube live chat through the Data API, within a stated quota budget.

Two calls, in this order:

1. find the chat — ``videos.list(part="liveStreamingDetails", id=<video>)`` for a
   named video, else ``liveBroadcasts.list(part="id,snippet", mine=True,
   broadcastStatus="active")`` for "whatever this channel has live right now".
   Both cost 1 quota unit and both only answer while a broadcast is live: an
   ``activeLiveChatId`` exists for a live video and for nothing else, which is
   why "not live" is a refusal code here rather than an empty message list.
2. poll ``liveChatMessages.list`` (5 quota units per call against a 10,000-unit
   daily default) with the ``pollingIntervalMillis`` the vendor hands back.
   ``nextPageToken`` is carried so a poll returns only what arrived since the
   last one; without it every poll would re-send the same messages.

Quota is treated as a real budget, not a footnote: the poll interval is floored
(``min_poll_interval_s``) and every read reports how many polls it spent, so an
operator tailing YouTube chat can see what a window cost before it empties a
day's allowance.

Injection: ``list_videos`` / ``list_broadcasts`` / ``list_messages`` are
callables taking the vendor's arguments and returning the vendor's JSON shape,
so every branch — live, offline, forbidden, empty, a page of messages — is
provable without an account.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from collections.abc import Callable

from xpst.chat.model import (
    CREDENTIALS_MISSING,
    INVALID_RESPONSE,
    NOT_LIVE,
    PLATFORM_ERROR,
    ChatError,
    ChatMessage,
    ReadOutcome,
    iso_from_text,
)
from xpst.stream.ingest import LIVE_NOT_ENABLED

LIVE_CHAT_QUOTA_UNITS = 5
#: The vendor's own floor is ~1-2s; a tight loop on a 10,000-unit day is what
#: burns the budget, so the reader refuses to poll faster than this.
MIN_POLL_INTERVAL_S = 2.0
DEFAULT_POLL_INTERVAL_S = 2.0
_MAX_POLLS = 600


def _chat_error(code: str, message: str, action: str = "", status: int | None = None) -> ChatError:
    return ChatError(code, message, platform="youtube", action=action, status=status)


def _vendor_detail(exc: Exception) -> str:
    """Google's HttpError carries the API message in ``str()``; keep it short."""
    detail = str(exc)
    return detail[:300]


def _status_of(exc: Exception) -> int | None:
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    return status if isinstance(status, int) else None


def _list_error_action(status: int | None) -> str:
    """The next step for a failed list call, from the vendor's own status.

    A 400 is not an auth problem: it means the request was malformed, which is
    an xPST defect and must not send an operator to re-authorise a working
    account. (Measured live 2026-11-16: ``mine`` + ``broadcastStatus`` was
    exactly that, and it was reported as an auth failure until this existed.)
    """
    if status in (401, 403):
        return "Re-authorise the channel with `xpst auth youtube`."
    if status == 400:
        return (
            "This is an xPST request-shape defect, not an account problem: YouTube rejected the "
            "call as malformed. Report it with the vendor message above."
        )
    if status == 429:
        return "The YouTube quota is exhausted; it resets at midnight Pacific Time."
    return ""


#: The one action that fixes "this channel is not enabled for live streaming".
LIVE_NOT_ENABLED_ACTION = (
    "Enable live streaming on this channel: verify the phone number at "
    "https://www.youtube.com/features and wait up to 24 hours for live to activate. "
    "This is a channel setting, not an OAuth problem."
)


def _raise_list_error(what: str, exc: Exception) -> NoReturn:
    """Turn a failed list call into a coded refusal, with the right next step.

    The account-level verdict is separated from the request-level one: a 403
    whose body says the user is not enabled for live streaming is a channel
    setting (``live_not_enabled``), while a malformed request is an xPST defect
    and a 401 is a real auth problem. Collapsing those three into
    ``platform_error`` is what sends operators to re-authorise a working token.
    """
    detail = _vendor_detail(exc)
    status = _status_of(exc)
    gated = "not enabled for live streaming" in detail.lower()
    raise _chat_error(
        LIVE_NOT_ENABLED if gated else PLATFORM_ERROR,
        f"youtube: {what} failed ({type(exc).__name__}: {detail})",
        action=LIVE_NOT_ENABLED_ACTION if gated else _list_error_action(status),
        status=status,
    )


def _first_object(rows: Any) -> dict[str, Any]:
    """The first element of a vendor ``items`` list, when it is an object."""
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return rows[0]
    return {}


def _dict_field(container: dict[str, Any], name: str) -> dict[str, Any]:
    """A nested object field, or ``{}`` when it is absent or another type."""
    value = container.get(name)
    return value if isinstance(value, dict) else {}


def resolve_live_chat_id(
    *,
    video_id: str = "",
    list_videos: Callable[[str], dict[str, Any]] | None = None,
    list_broadcasts: Callable[[], dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """``(video_id, live_chat_id)`` for a named video or the channel's live one.

    An empty result is ``NOT_LIVE``, not an empty chat: the connected channel may
    simply not be streaming, and saying "no messages" there would be a lie that
    an operator would chase for an hour.
    """
    if video_id:
        if list_videos is None:
            raise _chat_error(INVALID_RESPONSE, "youtube: no videos.list reader was supplied")
        try:
            payload = list_videos(video_id)
        except Exception as exc:  # noqa: BLE001 - googleapiclient raises HttpError subclasses
            _raise_list_error("videos.list", exc)
        items = payload.get("items") if isinstance(payload, dict) else None
        first = _first_object(items)
        details = _dict_field(first, "liveStreamingDetails")
        chat_id = str(details.get("activeLiveChatId", "") or "").strip()
        if not chat_id:
            raise _chat_error(
                NOT_LIVE,
                f"youtube: video {video_id} has no activeLiveChatId, so it is not live right now",
                action="YouTube only exposes a live chat while a broadcast is running.",
            )
        return video_id, chat_id

    if list_broadcasts is None:
        raise _chat_error(INVALID_RESPONSE, "youtube: no liveBroadcasts.list reader was supplied")
    try:
        payload = list_broadcasts()
    except Exception as exc:  # noqa: BLE001 - googleapiclient raises HttpError subclasses
        _raise_list_error("liveBroadcasts.list", exc)
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        raise _chat_error(
            NOT_LIVE,
            "youtube: the channel has no broadcasts recorded, so there is no live chat to read",
            action="Go live on the connected channel, or name a live video with --youtube-video.",
        )
    live_row = next(
        (
            item
            for item in items
            if isinstance(item, dict)
            and _dict_field(item, "snippet").get("liveBroadcastContent") == "live"
        ),
        None,
    )
    if live_row is None:
        raise _chat_error(
            NOT_LIVE,
            f"youtube: none of the channel's {len(items)} broadcast(s) is live right now",
            action="Go live on the connected channel, or name a live video with --youtube-video.",
        )
    snippet = _dict_field(live_row, "snippet")
    chat_id = str(snippet.get("liveChatId", "") or "").strip()
    if not chat_id:
        raise _chat_error(
            NOT_LIVE,
            "youtube: the live broadcast carries no liveChatId",
            action="YouTube enables chat per broadcast; this one has chat off.",
        )
    return str(live_row.get("id", "") or ""), chat_id


def messages_from_response(payload: dict[str, Any], video_id: str = "") -> list[ChatMessage]:
    """The ``items`` of a ``liveChatMessages.list`` answer as chat messages."""
    items = payload.get("items") if isinstance(payload, dict) else None
    if items is None:
        return []
    if not isinstance(items, list):
        raise _chat_error(INVALID_RESPONSE, "youtube: liveChatMessages answered with a non-list items field")
    messages: list[ChatMessage] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        snippet = _dict_field(item, "snippet")
        author_details = _dict_field(item, "authorDetails")
        text = str(snippet.get("displayMessage", "") or "")
        if not text:
            # Deleted messages, sponsor-only placeholders and super-chat stubs
            # carry no displayMessage; they are not chat text and must not be
            # rendered as blank rows in the merged feed.
            continue
        messages.append(
            ChatMessage(
                platform="youtube",
                author=str(author_details.get("displayName", "") or "unknown"),
                text=text,
                timestamp=iso_from_text(str(snippet.get("publishedAt", "") or "")),
                message_id=str(item.get("id", "") or ""),
                # YouTube has no per-channel name in this payload for the
                # operator's own broadcast; the video id is the honest label.
                channel=video_id,
            )
        )
    return messages


async def read_youtube_chat(
    *,
    list_messages: Callable[[str | None], dict[str, Any]],
    video_id: str,
    duration_s: float,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    min_poll_interval_s: float = MIN_POLL_INTERVAL_S,
    limit: int = 500,
) -> ReadOutcome:
    """Poll the live chat for ``duration_s`` and return what arrived.

    ``list_messages(next_page_token)`` is the vendor call. It is called in a
    worker thread because the Google client is blocking, so a poll cannot stall
    the event loop that the Twitch/Kick readers are sharing.
    """
    interval = max(min_poll_interval_s, float(poll_interval_s or DEFAULT_POLL_INTERVAL_S))
    deadline = time.monotonic() + max(0.0, duration_s)
    token: str | None = None
    messages: list[ChatMessage] = []
    polls = 0
    offline_at = ""
    while polls < _MAX_POLLS:
        try:
            payload = await asyncio.to_thread(list_messages, token)
        except ChatError:
            raise
        except Exception as exc:  # noqa: BLE001 - googleapiclient raises HttpError subclasses
            raise _chat_error(
                PLATFORM_ERROR,
                f"youtube: liveChatMessages.list failed ({type(exc).__name__}: {_vendor_detail(exc)})",
                action=(
                    "A 403 here is usually the daily quota (5 units per poll) or a missing "
                    "youtube.readonly scope on the channel's token."
                ),
                status=_status_of(exc),
            ) from exc
        polls += 1
        if not isinstance(payload, dict):
            raise _chat_error(INVALID_RESPONSE, "youtube: liveChatMessages answered with a non-object body")
        messages.extend(messages_from_response(payload, video_id))
        token = payload.get("nextPageToken") or token
        offline_at = str(payload.get("offlineAt", "") or "")
        if offline_at:
            break
        server_interval = payload.get("pollingIntervalMillis")
        if isinstance(server_interval, int) and server_interval > 0:
            interval = max(min_poll_interval_s, server_interval / 1000.0)
        if time.monotonic() + interval >= deadline:
            break
        await asyncio.sleep(interval)
    if len(messages) > limit:
        messages = messages[-limit:]
    note = f"{polls} poll(s), {polls * LIVE_CHAT_QUOTA_UNITS} quota unit(s)"
    if offline_at:
        note += f"; the broadcast went offline at {iso_from_text(offline_at) or offline_at}"
    return ReadOutcome(messages=messages, channels=[video_id] if video_id else [], note=note)


def bind_live_chat_id(service: Any, live_chat_id: str) -> Callable[[str | None], dict[str, Any]]:
    """``service_reader`` with the chat id bound, as the CLI/MCP path uses it."""

    def _fetch(page_token: str | None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "liveChatId": live_chat_id,
            "part": "snippet,authorDetails",
            "maxResults": 200,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        return service.liveChatMessages().list(**kwargs).execute()

    return _fetch


def videos_reader(service: Any) -> Callable[[str], dict[str, Any]]:
    def _fetch(video_id: str) -> dict[str, Any]:
        return service.videos().list(part="liveStreamingDetails", id=video_id).execute()

    return _fetch


def broadcasts_reader(service: Any) -> Callable[[], dict[str, Any]]:
    """The authenticated channel's own broadcasts — no ``broadcastStatus``.

    Measured live 2026-11-16: ``mine=true`` together with
    ``broadcastStatus=active`` is rejected with HTTP 400 "Incompatible
    parameters specified in the request: mine, broadcastStatus". So the list is
    fetched unfiltered and the live one is selected from
    ``snippet.liveBroadcastContent``, which is the field YouTube sets to
    ``live`` exactly while a broadcast is running.
    """

    def _fetch() -> dict[str, Any]:
        return service.liveBroadcasts().list(part="id,snippet", mine=True).execute()

    return _fetch


def require_service(service: Any | None) -> Any:
    """Refuse rather than crash when the channel is not connected."""
    if service is None:
        raise _chat_error(
            CREDENTIALS_MISSING,
            "youtube: no OAuth token is stored, so live chat cannot be read",
            action=(
                "Connect the destination channel with `xpst auth youtube` (scopes youtube.readonly "
                "or youtube.force-ssl)."
            ),
        )
    return service
