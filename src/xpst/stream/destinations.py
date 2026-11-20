"""Which platforms can hand this app a live-stream ingest key, and which cannot.

This module is data, not behaviour. It exists so that every surface (CLI, MCP,
HTTP API, docs) answers the stream question the same way instead of inventing
its own verdict, exactly like :mod:`xpst.content` does for posting envelopes.

Three classes, and the class is a property of the *vendor*, not of xPST:

``api``
    The platform documents an endpoint that returns an ingest URL and a stream
    key for an authenticated account. xPST may fetch the key.
``manual``
    The platform issues a per-session key in a web console with no endpoint
    behind it. xPST can prepare everything except the key, and must say so:
    "open this page and paste" is the honest instruction, never a fake success.
``unsupported``
    No live ingest for the account type at all. Never presented as a
    destination.

Sources were checked 2026-11-16 against the destination-rules research
(``xpst-platform-rules-analytics-streaming-2026-11-16.md`` §5) and the vendor
documentation it cites. A row that says "not yet exercised" means the path is
documented by the vendor but this repository has never completed a live fetch —
that is a fact about the evidence, not a TODO to hide.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: Ingest classes. See the module docstring; never add a fourth spelling.
API = "api"
MANUAL = "manual"
UNSUPPORTED = "unsupported"

StreamIngestClass = Literal["api", "manual", "unsupported"]


@dataclass(frozen=True)
class StreamIngestPath:
    """One platform's live-ingest story.

    ``endpoint`` is the documented call that yields a key (``api`` rows only);
    ``operator_action`` is what a human must do when xPST cannot do it; ``note``
    carries the caveat that would otherwise become a support ticket.
    """

    platform: str
    display_name: str
    ingest_class: StreamIngestClass
    endpoint: str | None = None
    requires: str = ""
    operator_action: str = ""
    source_url: str | None = None
    note: str = ""

    @property
    def api_available(self) -> bool:
        return self.ingest_class == API

    def to_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "display_name": self.display_name,
            "ingest_class": self.ingest_class,
            "api_available": self.api_available,
            "endpoint": self.endpoint,
            "requires": self.requires,
            "operator_action": self.operator_action,
            "source_url": self.source_url,
            "note": self.note,
        }


#: The registry. Order is the order surfaces present them in: keys-via-API
#: first, then manual handoffs, then the refusals.
STREAM_INGEST_PATHS: dict[str, StreamIngestPath] = {
    "youtube": StreamIngestPath(
        platform="youtube",
        display_name="YouTube Live",
        ingest_class=API,
        endpoint="POST https://www.googleapis.com/youtube/v3/liveStreams",
        requires="OAuth token with the youtube scope on the destination channel",
        operator_action="sign in with the channel that will receive the stream",
        source_url="https://developers.google.com/youtube/v3/live/docs/liveStreams/insert",
        note=(
            "The API returns cdn.ingestionInfo.ingestionAddress + streamName. "
            "Creating a liveStream resource does not start or announce a broadcast."
        ),
    ),
    "twitch": StreamIngestPath(
        platform="twitch",
        display_name="Twitch",
        ingest_class=API,
        endpoint="GET https://api.twitch.tv/helix/streams/key?broadcaster_id=<id>",
        requires="Twitch app client id + a user access token with the channel:read:stream_key scope, and the broadcaster id",
        operator_action="register a Twitch application (free) and authorise it as the broadcaster",
        source_url="https://dev.twitch.tv/docs/api/reference/#get-stream-key",
        note=(
            "The endpoint requires a *user* access token; an app access token cannot read a "
            "stream key. Twitch answers 403 for accounts not permitted to stream."
        ),
    ),
    "kick": StreamIngestPath(
        platform="kick",
        display_name="Kick",
        ingest_class=API,
        endpoint="GET https://api.kick.com/public/v1/channels?broadcaster_user_id=<id>",
        requires="Kick user token carrying the streamkey:read scope",
        operator_action="register a Kick application (free) and authorise the channel",
        source_url="https://docs.kick.com/apis/channels",
        note=(
            "The key and its ingest URL arrive as data[].stream.key / .url; Kick publishes no "
            "standalone stream-key endpoint. Documented but never exercised by this repo."
        ),
    ),
    "facebook": StreamIngestPath(
        platform="facebook",
        display_name="Facebook Page Live",
        ingest_class=API,
        endpoint="POST https://graph.facebook.com/<version>/{page-id}/live_videos",
        requires="Page access token with the live-video permission on that Page",
        operator_action="connect the Page (a personal profile cannot go live through the API)",
        source_url="https://developers.facebook.com/docs/live-video-api",
        note=(
            "Page destinations only; the Page token is the credential. xPST creates the stream "
            "with published=false, so fetching a key does not put anything on the Page."
        ),
    ),
    "instagram": StreamIngestPath(
        platform="instagram",
        display_name="Instagram Live",
        ingest_class=MANUAL,
        requires="a public account with at least 1,000 followers",
        operator_action=(
            "open instagram.com → Create → Live video (Live Producer) and copy the "
            "server URL plus the per-session stream key"
        ),
        source_url="https://help.instagram.com/292478487812558",
        note=(
            "Keys are per session by design, so a relay cannot be configured once and "
            "forgotten. An official Live API for eligible accounts is reported but "
            "unconfirmed for this repository; until it is proven, this row stays manual."
        ),
    ),
    "tiktok": StreamIngestPath(
        platform="tiktok",
        display_name="TikTok LIVE",
        ingest_class=MANUAL,
        requires="LIVE access on the account (18+, 1,000 followers)",
        operator_action="open TikTok LIVE Center, start a live setup, copy the RTMP URL and key",
        source_url="https://support.tiktok.com/en/live-gifts-wallet/tiktok-live/what-is-tiktok-live",
        note="Per-session key with no documented endpoint for handing one to an app.",
    ),
    "rumble": StreamIngestPath(
        platform="rumble",
        display_name="Rumble Live",
        ingest_class=MANUAL,
        requires="live access on the account",
        operator_action="copy the RTMP ingest URL and stream key from the Rumble studio",
        source_url="https://rumble.support/help/how-to-setup-a-livestream",
        note="No public ingest-key endpoint.",
    ),
    "x": StreamIngestPath(
        platform="x",
        display_name="X",
        ingest_class=UNSUPPORTED,
        note="No live-video ingest API for third-party apps.",
    ),
    "threads": StreamIngestPath(
        platform="threads",
        display_name="Threads",
        ingest_class=UNSUPPORTED,
        note="No live broadcast surface.",
    ),
    "bluesky": StreamIngestPath(
        platform="bluesky",
        display_name="Bluesky",
        ingest_class=UNSUPPORTED,
        note="No live ingest; AT Protocol carries posts, not broadcast.",
    ),
}

#: Simulcast prohibitions that make a destination a *refusal*, not a warning.
#: Kept beside the registry so a surface cannot offer a combination the vendor
#: forbids. These are policy facts, quoted from the destination-rules research.
SIMULCAST_REFUSALS: dict[str, str] = {
    "tiktok_shop": "TikTok Shop prohibits simulcasting the same live to other platforms",
    "twitch_redirect": "Twitch forbids redirecting viewers to another platform from a stream",
}


def ingest_path(platform: str) -> StreamIngestPath:
    """Return the registry row for ``platform`` or raise ``KeyError``.

    A caller that asks about a platform this registry has never heard of is a
    bug, not a user error: silently returning ``unsupported`` would let a new
    platform ship with no live story and no complaint.
    """
    try:
        return STREAM_INGEST_PATHS[platform]
    except KeyError as exc:  # pragma: no cover - defensive
        raise KeyError(f"no stream ingest path registered for {platform!r}") from exc


def api_destinations() -> list[StreamIngestPath]:
    """Rows where the vendor documents a key endpoint."""
    return [row for row in STREAM_INGEST_PATHS.values() if row.api_available]


def manual_destinations() -> list[StreamIngestPath]:
    """Rows that end in a copy/paste by the operator."""
    return [row for row in STREAM_INGEST_PATHS.values() if row.ingest_class == MANUAL]


def unsupported_destinations() -> list[StreamIngestPath]:
    """Rows that must never appear as a destination."""
    return [row for row in STREAM_INGEST_PATHS.values() if row.ingest_class == UNSUPPORTED]


def refusal_reason(platform: str) -> str | None:
    """Return the documented prohibition for a refused combination, else ``None``."""
    return SIMULCAST_REFUSALS.get(platform)


def registry_snapshot() -> dict[str, object]:
    """Serialisable view used by CLI/JSON output and documentation tables."""
    return {
        "api": [row.to_dict() for row in api_destinations()],
        "manual": [row.to_dict() for row in manual_destinations()],
        "unsupported": [row.to_dict() for row in unsupported_destinations()],
        "refusals": dict(SIMULCAST_REFUSALS),
    }
