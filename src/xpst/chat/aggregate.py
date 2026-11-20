"""One merged, source-tagged chat timeline — and an honest word for silence.

The feature is deliberately narrow: read what each platform shows every viewer,
tag every message with the platform it came from, sort them into one
chronological feed, and say exactly why any requested platform contributed
nothing. It never sends, never moderates, never replies.

Why "unavailable" is not "empty"
--------------------------------
Every one of these platforms can fail in a way that looks like a quiet chat:
Twitch silently drops a ``JOIN`` for a channel that does not exist, Kick needs a
channel slug that may 404, and YouTube has no chat id at all unless a broadcast
is live. Reporting ``messages: []`` for those would send an operator looking for
a problem in the wrong place, so each reader raises a coded
:class:`~xpst.chat.model.ChatError` and the aggregate reports the code, the
message and the next step beside the platforms that did answer.

Reuse, not a second implementation
----------------------------------
Credential presence for YouTube comes from
:func:`xpst.stream.ingest.fetch_readiness` and the authenticated client from
:func:`xpst.stream.ingest.build_youtube_live_service`, both of which the stream
relay already maintains. A chat reader with its own token check would be a
second place for "is this channel connected?" to be wrong.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from xpst.chat import kick as kick_reader
from xpst.chat import twitch as twitch_reader
from xpst.chat import youtube as youtube_reader
from xpst.chat.model import (
    CHAT_PLATFORMS,
    CREDENTIALS_MISSING,
    NO_CHANNEL,
    NOT_SUPPORTED,
    ChatError,
    PlatformRead,
    ReadOutcome,
    Timeline,
)
from xpst.chat.registry import CHAT_READ_PATHS, registry_snapshot

if TYPE_CHECKING:
    from xpst.config import XPSTConfig

#: A bounded default: long enough to see traffic on a busy channel, short enough
#: that an MCP call or an HTTP request does not look hung.
DEFAULT_DURATION_S = 20.0
MAX_DURATION_S = 300.0
DEFAULT_LIMIT = 500
MAX_LIMIT = 5000


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def known_platforms() -> list[str]:
    return [name for name in CHAT_PLATFORMS if name in CHAT_READ_PATHS]


def resolve_channels(
    config: Any,
    *,
    twitch_channels: list[str] | None = None,
    kick_channels: list[str] | None = None,
    youtube_video_id: str = "",
) -> tuple[list[str], list[str], str]:
    """Explicit arguments win; the config file is the fallback, never the reverse.

    An explicit empty list means "none", not "use the config" — that is the only
    reading that lets a caller ask about one platform without inheriting a
    channel list it did not want.
    """
    chat = getattr(config, "chat", None) if config is not None else None
    twitch = list(twitch_channels) if twitch_channels is not None else list(getattr(chat, "twitch_channels", []) or [])
    kick = list(kick_channels) if kick_channels is not None else list(getattr(chat, "kick_channels", []) or [])
    video = youtube_video_id or str(getattr(chat, "youtube_video_id", "") or "")
    return twitch, kick, video


def chat_readiness(
    config_dir: Any,
    *,
    platforms: list[str] | None = None,
    config: XPSTConfig | None = None,
) -> dict[str, Any]:
    """What xPST can read right now, and what stands in the way. No network.

    This is the payload that answers "why does chat say nothing?" without
    spending a request: a platform with no channel configured and one with no
    credential are different answers and are reported as such.
    """
    from xpst.stream.ingest import fetch_readiness

    wanted = platforms or known_platforms()
    twitch_channels, kick_channels, video_id = resolve_channels(config)
    rows: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for name in wanted:
        path = CHAT_READ_PATHS.get(name)
        if path is None:
            rows.append(
                {
                    "platform": name,
                    "status": "unavailable",
                    "unavailable": {
                        "platform": name,
                        "code": NOT_SUPPORTED,
                        "message": f"{name!r} has no chat read path in xPST",
                        "action": f"Known chat platforms: {', '.join(known_platforms())}",
                        "docs_url": "",
                        "status": None,
                    },
                }
            )
            continue
        row: dict[str, Any] = {
            "platform": name,
            "display_name": path.display_name,
            "transport": path.transport,
            "endpoint": path.endpoint,
            "verified": path.verified,
            "read_only": path.read_only,
            "source_url": path.source_url,
            "note": path.note,
        }
        if name == "twitch":
            row["channels"] = list(twitch_channels)
            absent = [] if twitch_channels else ["channel"]
            action = path.operator_action
        elif name == "kick":
            row["channels"] = list(kick_channels)
            absent = [] if kick_channels else ["channel"]
            action = path.operator_action
        else:
            try:
                readiness = fetch_readiness(config_dir, "youtube", config=config)
                absent = list(readiness.missing)
                action = readiness.action
            except Exception as exc:  # noqa: BLE001 - an unusable config dir must not crash a read-only status call
                absent = ["oauth_token"]
                action = (
                    f"xPST could not read the credential store at {config_dir} "
                    f"({type(exc).__name__}); fix the config directory, then `xpst auth youtube`."
                )
            row["channels"] = [video_id] if video_id else []
        row["credentials_present"] = not absent
        row["missing"] = absent
        row["action"] = action
        row["status"] = "ready" if not absent else "unavailable"
        if absent:
            row["unavailable"] = {
                "platform": name,
                "code": CREDENTIALS_MISSING if name == "youtube" else NO_CHANNEL,
                "message": (
                    f"{name}: missing {', '.join(absent)}"
                    if name == "youtube"
                    else f"{name}: no channel is configured, so there is no chat to read"
                ),
                "action": action,
                "docs_url": path.source_url,
                "status": None,
            }
            missing.append(row["unavailable"])
        rows.append(row)
    return {
        "ok": True,
        "contract_version": 1,
        "read_only": True,
        "checked_at": _now_iso(),
        "platforms": rows,
        "unavailable": missing,
        "registry": registry_snapshot(),
    }


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _platform_row(outcome: PlatformRead) -> dict[str, Any]:
    return outcome.to_dict()


def _youtube_readers(config: XPSTConfig | None) -> Any:
    """Build the authenticated YouTube client, or ``None`` when not connected.

    Runs in a worker thread because the stream module's builder drives its own
    event loop (the credential store's token refresh is async), and because the
    Google client is blocking anyway.
    """
    if config is None:
        return None
    try:
        from xpst.stream.ingest import build_youtube_live_service

        return build_youtube_live_service(config)
    except Exception:  # noqa: BLE001 - "not connected" is reported by readiness
        return None


async def _read_twitch(
    channels: list[str],
    duration_s: float,
    *,
    connect: Any = None,
) -> ReadOutcome:
    return await twitch_reader.read_twitch_chat(channels, duration_s, connect=connect)


async def _read_kick(
    channels: list[str],
    duration_s: float,
    *,
    http_get: Any = None,
    http_post: Any = None,
    ws_connect: Any = None,
) -> ReadOutcome:
    return await kick_reader.read_kick_chat(
        channels, duration_s, http_get=http_get, http_post=http_post, ws_connect=ws_connect
    )


async def _read_youtube(
    *,
    video_id: str,
    duration_s: float,
    service: Any,
    poll_interval_s: float,
    limit: int,
) -> ReadOutcome:
    live_video, chat_id = await asyncio.to_thread(
        youtube_reader.resolve_live_chat_id,
        video_id=video_id,
        list_videos=youtube_reader.videos_reader(service) if video_id else None,
        list_broadcasts=None if video_id else youtube_reader.broadcasts_reader(service),
    )
    return await youtube_reader.read_youtube_chat(
        list_messages=youtube_reader.bind_live_chat_id(service, chat_id),
        video_id=live_video or video_id,
        duration_s=duration_s,
        poll_interval_s=poll_interval_s,
        limit=limit,
    )


async def collect_chat(
    config: XPSTConfig | None,
    *,
    config_dir: Any = None,
    platforms: list[str] | None = None,
    duration_s: float = DEFAULT_DURATION_S,
    twitch_channels: list[str] | None = None,
    kick_channels: list[str] | None = None,
    youtube_video_id: str = "",
    limit: int = DEFAULT_LIMIT,
    youtube_poll_interval_s: float = youtube_reader.DEFAULT_POLL_INTERVAL_S,
    twitch_connect: Any = None,
    kick_http_get: Any = None,
    kick_http_post: Any = None,
    kick_ws_connect: Any = None,
    youtube_service: Any = None,
) -> dict[str, Any]:
    """Read every requested platform's chat for one bounded window, and merge it.

    ``ok`` is false only when *nothing* was readable: at least one platform
    answered, or the request asked for none. A partially readable request is
    ``ok: true`` with the refusals listed under ``unavailable``, because that is
    the truthful shape — one dead platform must not hide the live one.
    """
    wanted = [name for name in (platforms or known_platforms()) if name]
    unknown = [name for name in wanted if name not in CHAT_READ_PATHS]
    window = _clamp(duration_s, 1.0, MAX_DURATION_S)
    cap = int(_clamp(limit, 1, MAX_LIMIT))
    twitch_channels, kick_channels, video_id = resolve_channels(
        config,
        twitch_channels=twitch_channels,
        kick_channels=kick_channels,
        youtube_video_id=youtube_video_id,
    )
    directory = config_dir if config_dir is not None else getattr(config, "config_dir", None)

    started_at = _now_iso()
    reads: dict[str, PlatformRead] = {}
    service = youtube_service
    if "youtube" in wanted and service is None:
        if directory is None:
            reads["youtube"] = PlatformRead(
                platform="youtube",
                status="unavailable",
                error=ChatError(
                    CREDENTIALS_MISSING,
                    "youtube: no config directory is available to look for an OAuth token",
                    platform="youtube",
                    action=CHAT_READ_PATHS["youtube"].operator_action,
                ),
            )
        else:
            service = await asyncio.to_thread(_youtube_readers, config)

    async def _guarded(name: str, coro: Any) -> PlatformRead:
        try:
            outcome: ReadOutcome = await coro
        except ChatError as exc:
            return PlatformRead(platform=name, status="unavailable", error=exc)
        except Exception as exc:  # noqa: BLE001 - a reader bug must not sink the merge
            return PlatformRead(
                platform=name,
                status="unavailable",
                error=ChatError(
                    "invalid_response",
                    f"{name}: the reader failed unexpectedly ({type(exc).__name__}: {exc})",
                    platform=name,
                    action="This is an xPST defect; report it with the platform and the message.",
                ),
            )
        channels = ", ".join(outcome.channels)
        error = None
        if outcome.unconfirmed and not outcome.messages:
            path = CHAT_READ_PATHS.get(name)
            error = ChatError(
                NO_CHANNEL,
                f"{name}: could not confirm chat for {', '.join(outcome.unconfirmed)}",
                platform=name,
                action=outcome.note or (path.operator_action if path is not None else ""),
            )
        return PlatformRead(
            platform=name,
            status="unavailable" if error else ("read" if outcome.messages else "empty"),
            channel=channels,
            messages=len(outcome.messages),
            error=error,
            connected_at=_now_iso(),
            note=outcome.note,
            outcome=outcome,
        )

    tasks: list[Any] = []
    names: list[str] = []
    if "twitch" in wanted:
        names.append("twitch")
        tasks.append(_guarded("twitch", _read_twitch(twitch_channels, window, connect=twitch_connect)))
    if "kick" in wanted:
        names.append("kick")
        tasks.append(
            _guarded(
                "kick",
                _read_kick(
                    kick_channels,
                    window,
                    http_get=kick_http_get,
                    http_post=kick_http_post,
                    ws_connect=kick_ws_connect,
                ),
            )
        )
    if "youtube" in wanted:
        names.append("youtube")
        if service is None:
            coro: Any = _raise_missing_youtube()
        else:
            coro = _read_youtube(
                video_id=video_id,
                duration_s=window,
                service=service,
                poll_interval_s=youtube_poll_interval_s,
                limit=cap,
            )
        tasks.append(_guarded("youtube", coro))

    if tasks:
        results = await asyncio.gather(*tasks)
        for name, result in zip(names, results, strict=False):
            reads[name] = result
    for name in unknown:
        reads[name] = PlatformRead(
            platform=name,
            status="unavailable",
            error=ChatError(
                NOT_SUPPORTED,
                f"{name!r} has no chat read path in xPST",
                platform=name,
                action=f"Known chat platforms: {', '.join(known_platforms())}",
            ),
        )

    timeline = Timeline(limit=cap)
    unavailable: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for name in wanted:
        read = reads.get(name)
        if read is None:
            continue
        if read.outcome is not None:
            timeline.extend(read.outcome.messages)
        row = _platform_row(read)
        row["transport"] = CHAT_READ_PATHS[name].transport if name in CHAT_READ_PATHS else ""
        row["verified"] = CHAT_READ_PATHS[name].verified if name in CHAT_READ_PATHS else False
        rows.append(row)
        if read.error is not None:
            unavailable.append(read.error.to_dict())

    readable = [row for row in rows if row["status"] in ("read", "empty")]
    payload: dict[str, Any] = {
        "ok": bool(readable),
        "contract_version": 1,
        "read_only": True,
        "window": {
            "requested_s": float(duration_s),
            "served_s": window,
            "started_at": started_at,
            "ended_at": _now_iso(),
        },
        "platforms": rows,
        "unavailable": unavailable,
        "messages": timeline.to_list(),
        "counts": timeline.counts(),
        "dropped": timeline.dropped,
        "truncated": len(timeline.messages) >= cap,
        "limit": cap,
    }
    if not readable and unavailable:
        payload["code"] = unavailable[0].get("code", "")
        payload["message"] = "no requested platform could be read; see `unavailable` for the reason per platform"
    return payload


async def _raise_missing_youtube() -> ReadOutcome:
    raise ChatError(
        CREDENTIALS_MISSING,
        "youtube: no OAuth token is stored, so live chat cannot be read",
        platform="youtube",
        action=CHAT_READ_PATHS["youtube"].operator_action,
        docs_url=CHAT_READ_PATHS["youtube"].source_url,
    )


def chat_timeline(
    config: XPSTConfig | None,
    *,
    config_dir: Any = None,
    platforms: list[str] | None = None,
    duration_s: float = DEFAULT_DURATION_S,
    twitch_channels: list[str] | None = None,
    kick_channels: list[str] | None = None,
    youtube_video_id: str = "",
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Synchronous entry point for the CLI and the HTTP route."""
    return asyncio.run(
        collect_chat(
            config,
            config_dir=config_dir,
            platforms=platforms,
            duration_s=duration_s,
            twitch_channels=twitch_channels,
            kick_channels=kick_channels,
            youtube_video_id=youtube_video_id,
            limit=limit,
        )
    )
