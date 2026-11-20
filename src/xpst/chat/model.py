"""One chat message, one merged timeline, and the codes for "cannot read this".

The chat pillar is read-only by decision: xPST reads what the platform shows to
every viewer and never sends, moderates, or auto-replies. That is why there is
no ``send`` anywhere in this package and why :class:`ChatMessage` has no
outbound fields — a posting capability is a separate decision with its own
consent story (a bot that can answer in someone's live chat is a different
product from one that watches it).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

#: Platforms with a chat read path in this slice. Order is presentation order.
CHAT_PLATFORMS: tuple[str, ...] = ("twitch", "youtube", "kick")

#: Stable machine-readable reasons a platform could not be read. Agents branch
#: on these, never on prose. They are deliberately distinct: "nobody is
#: streaming" and "xPST has no credential" lead to completely different next
#: steps, and collapsing them into one "unavailable" is the bug this avoids.
CREDENTIALS_MISSING = "credentials_missing"
NO_CHANNEL = "no_channel"
NOT_LIVE = "not_live"
PLATFORM_ERROR = "platform_error"
NETWORK_ERROR = "network_error"
INVALID_RESPONSE = "invalid_response"
#: The vendor does not document this read path; it was found and verified by
#: observation only. Reported so an operator can tell it apart from a
#: documented call that is merely uncredentialled.
UNVERIFIED_PATH = "unverified_path"
#: The platform has no chat readable by an app at all.
NOT_SUPPORTED = "not_supported"
#: The requested window ended with the caller asking for more than was served.
TRUNCATED = "truncated"


def normalize_channel(value: Any) -> str:
    """A channel name as the platform spells it, without chat's decorators.

    Operators paste whatever the site shows them — ``#examplechannel``,
    ``@examplechannel``, or the bare login — and every one of those is the same
    channel. Stripping the prefix here means the value stored in config is the
    name the readers actually use, instead of a decorated string that silently
    fails a ``JOIN`` (Twitch drops an unknown channel without an error, so a
    stored ``#name`` would read as an empty chat rather than a mistake).

    Returns ``""`` for an empty value so a caller can tell "nothing supplied"
    from a real name; raises ``ValueError`` for something that cannot be a
    channel at all, because dropping that silently is how a typo becomes
    "nobody is talking".
    """
    text = str(value if value is not None else "").strip()
    while text[:1] in ("#", "@"):
        text = text[1:].strip()
    if not text:
        return ""
    if any(char.isspace() for char in text) or "/" in text:
        raise ValueError(f"{text!r} is not a channel name (no spaces or slashes)")
    return text


def normalize_video_id(value: Any) -> str:
    """A YouTube video id, accepting the watch/shorts/live URL an operator has.

    The chat API needs the *id*, but a non-CLI operator copies the address bar.
    Accepting both is not a second truth: the URL is reduced to its id at the
    boundary, so nothing downstream ever stores a URL where an id belongs.
    """
    text = str(value if value is not None else "").strip()
    if not text:
        return ""
    if any(char.isspace() for char in text):
        raise ValueError(f"{text!r} is not a video id or a YouTube URL")
    from urllib.parse import parse_qs, urlparse

    candidate = text if "://" in text else f"//{text.lstrip('/')}"
    parsed = urlparse(candidate)
    host = (parsed.netloc or "").lower()
    if "youtube.com" in host or "youtu.be" in host:
        query_id = (parse_qs(parsed.query).get("v") or [""])[0].strip()
        if query_id:
            return query_id
        segments = [part for part in (parsed.path or "").split("/") if part]
        if host.endswith("youtu.be") and segments:
            return segments[0]
        if len(segments) >= 2 and segments[0] in ("shorts", "live", "embed", "v"):
            return segments[1]
        raise ValueError(f"{text!r} is a YouTube URL with no video id in it")
    if "/" in text:
        raise ValueError(f"{text!r} is not a video id or a YouTube URL")
    return text


class ChatError(RuntimeError):
    """A platform's chat could not be read, with a code and the next step."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        platform: str = "",
        action: str = "",
        docs_url: str = "",
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.platform = platform
        self.action = action
        self.docs_url = docs_url
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "code": self.code,
            "message": str(self),
            "action": self.action,
            "docs_url": self.docs_url,
            "status": self.status,
        }


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def iso_from_epoch_ms(value: int | float | str | None) -> str:
    """Twitch's ``tmi-sent-ts`` is epoch milliseconds; normalise to ISO-8601 UTC.

    An unparseable or absent stamp returns ``""`` rather than "now": a message
    whose time we do not know must not be silently redated to the moment xPST
    happened to read it, because that would corrupt the merged ordering.
    """
    if value in (None, ""):
        return ""
    try:
        seconds = float(value) / 1000.0
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=0).isoformat()


def iso_from_text(value: str) -> str:
    """Normalise a vendor ISO-8601 timestamp to UTC with a ``Z``-free offset.

    Returns ``""`` for anything unparseable, for the same reason
    :func:`iso_from_epoch_ms` does.
    """
    text = (value or "").strip()
    if not text:
        return ""
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class ChatMessage:
    """One message, tagged with the platform and channel it came from.

    ``timestamp`` is ISO-8601 UTC when the platform gave one, else ``""``. An
    empty stamp sorts last within the timeline rather than pretending the
    message arrived at read time.
    """

    platform: str
    author: str
    text: str
    timestamp: str = ""
    message_id: str = ""
    channel: str = ""

    @property
    def identity(self) -> str:
        """Deduplication key: the platform's own id when it has one."""
        if self.message_id:
            return f"{self.platform}:{self.message_id}"
        return f"{self.platform}:{self.channel}:{self.author}:{self.timestamp}:{self.text}"

    def sort_key(self) -> tuple[str, str, str]:
        return (self.timestamp or "9999", self.platform, self.identity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "author": self.author,
            "text": self.text,
            "timestamp": self.timestamp,
            "message_id": self.message_id,
            "channel": self.channel,
        }


@dataclass
class ReadOutcome:
    """What one reader returned: messages plus what it could not confirm.

    ``unconfirmed`` exists because two of these vendors fail *silently* when a
    channel is wrong: Twitch quietly drops a ``JOIN`` for a channel that does
    not exist, and Kick answers a 404 for an unknown slug. Without this field
    those cases would look identical to "the chat is simply quiet", which is
    the fail-dishonest answer this pillar is built to avoid.
    """

    messages: list[ChatMessage] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    unconfirmed: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class PlatformRead:
    """What happened when one platform was asked for its chat.

    ``status`` is ``"read"`` (the connection was established and the window
    elapsed), ``"unavailable"`` (a coded refusal) or ``"empty"`` (read, but no
    message arrived — a real answer that is not an error).
    """

    platform: str
    status: str
    channel: str = ""
    messages: int = 0
    error: ChatError | None = None
    connected_at: str = ""
    note: str = ""
    #: The raw messages this read produced. Carried so the aggregate does not
    #: have to re-run a reader (or keep a second dict) to build the timeline.
    outcome: ReadOutcome | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "platform": self.platform,
            "status": self.status,
            "channel": self.channel,
            "messages": self.messages,
            "connected_at": self.connected_at,
        }
        if self.note:
            payload["note"] = self.note
        if self.error is not None:
            payload["unavailable"] = self.error.to_dict()
        return payload


@dataclass
class Timeline:
    """A bounded, de-duplicated, chronologically ordered set of messages."""

    messages: list[ChatMessage] = field(default_factory=list)
    limit: int = 500
    seen: set[str] = field(default_factory=set)
    dropped: int = 0

    def add(self, message: ChatMessage) -> bool:
        """Append one message unless it is a duplicate. Returns whether it landed."""
        identity = message.identity
        if identity in self.seen:
            return False
        self.seen.add(identity)
        self.messages.append(message)
        if len(self.messages) > self.limit:
            # Oldest out. `seen` keeps the identity so a platform that re-sends
            # a dropped message cannot resurrect it out of order.
            self.messages.pop(0)
            self.dropped += 1
        return True

    def extend(self, messages: list[ChatMessage]) -> int:
        return sum(1 for message in messages if self.add(message))

    def ordered(self) -> list[ChatMessage]:
        return sorted(self.messages, key=lambda message: message.sort_key())

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {platform: 0 for platform in CHAT_PLATFORMS}
        for message in self.messages:
            tally[message.platform] = tally.get(message.platform, 0) + 1
        tally["total"] = len(self.messages)
        return tally

    def to_list(self) -> list[dict[str, Any]]:
        return [message.to_dict() for message in self.ordered()]
