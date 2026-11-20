"""One ingest, many destinations: build the relay's forward list.

xPST does not re-encode or proxy video in Python. The relay is MediaMTX (MIT),
which accepts one RTMP/RTMPS/SRT/WHIP publisher and forwards the same bytes to
every configured destination without transcoding. This module owns exactly one
thing: turning a set of destination credentials into a MediaMTX configuration
that is correct, minimal and secret-safe.

Design rules, all of them consequences of past incidents elsewhere in this repo:

* **A key never appears in logs, JSON or an exception message.** ``RelayDestination``
  keeps the key out of ``repr`` and every serialisable view goes through
  :meth:`RelayDestination.masked`.
* **Refuse, never repair.** An ingest URL that is not ``rtmp``/``rtmps``, a key
  carrying whitespace or a ``#``, a platform the registry does not know, or two
  destinations for the same platform are all errors. A relay that silently
  drops a destination is worse than one that refuses to start: the operator
  believes they are live somewhere they are not.
* **Generate the documented shape.** MediaMTX expresses a forward destination as
  ``{dest: rtmp(s)://host:port/path#streamKey}`` (``docs/5-forward/5-rtmp.md``);
  the older bare-string list is not emitted.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import yaml

from xpst.stream.destinations import STREAM_INGEST_PATHS, refusal_reason

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

#: MediaMTX paths cannot be empty and are used in URLs, so keep them boring.
_PATH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ALLOWED_SCHEMES = ("rtmp", "rtmps")

DEFAULT_PATH_NAME = "xpst"
DEFAULT_RTMP_PORT = 1935


class RelayConfigError(ValueError):
    """A destination cannot be relayed as configured."""


@dataclass(frozen=True)
class RelayDestination:
    """One destination the relay should forward to.

    ``stream_key`` is deliberately excluded from ``repr`` so an unexpected
    traceback or a debugger session cannot leak it.
    """

    platform: str
    ingest_url: str
    stream_key: str = field(repr=False)

    def __post_init__(self) -> None:
        platform = (self.platform or "").strip().lower()
        if platform not in STREAM_INGEST_PATHS:
            raise RelayConfigError(
                f"{self.platform!r} is not a registered stream destination "
                f"(known: {', '.join(sorted(STREAM_INGEST_PATHS))})"
            )
        path = STREAM_INGEST_PATHS[platform]
        if path.ingest_class == "unsupported":
            raise RelayConfigError(f"{platform} has no live ingest path ({path.note})")
        reason = refusal_reason(platform)
        if reason:
            raise RelayConfigError(f"{platform} cannot be a relay destination: {reason}")

        ingest_url = (self.ingest_url or "").strip()
        parts = urlsplit(ingest_url)
        if parts.scheme not in _ALLOWED_SCHEMES:
            raise RelayConfigError(
                f"{platform}: ingest URL must start with rtmp:// or rtmps:// "
                f"(got {parts.scheme or 'no scheme'})"
            )
        if not parts.hostname:
            raise RelayConfigError(f"{platform}: ingest URL has no host")

        key = (self.stream_key or "").strip()
        if not key:
            raise RelayConfigError(f"{platform}: stream key is empty")
        if any(ch.isspace() for ch in key):
            raise RelayConfigError(f"{platform}: stream key contains whitespace")
        if "#" in key:
            raise RelayConfigError(
                f"{platform}: stream key contains '#', which MediaMTX uses to separate "
                "the URL from the key — paste the key without it"
            )

        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "ingest_url", ingest_url)
        object.__setattr__(self, "stream_key", key)

    @property
    def forward_url(self) -> str:
        """The ``dest`` value MediaMTX connects to.

        MediaMTX appends everything after ``#`` to the ingest path, which is
        exactly how the platforms document a key (``rtmps://a.rtmps.youtube.com/live2#key``).
        """
        return f"{self.ingest_url}#{self.stream_key}"

    def masked(self) -> dict[str, str]:
        """Serialisable view with the key reduced to a presence marker."""
        masked_key = f"{self.stream_key[:2]}…{self.stream_key[-2:]}" if len(self.stream_key) > 6 else "…"
        return {
            "platform": self.platform,
            "ingest_url": self.ingest_url,
            "stream_key": masked_key,
        }


def _validate_path_name(path_name: str) -> str:
    if not _PATH_NAME_RE.match(path_name or ""):
        raise RelayConfigError(
            f"path name {path_name!r} must match {_PATH_NAME_RE.pattern} "
            "(it is part of the relay URL the encoder publishes to)"
        )
    return path_name


def build_relay_config(
    destinations: Sequence[RelayDestination],
    *,
    path_name: str = DEFAULT_PATH_NAME,
    rtmp_port: int = DEFAULT_RTMP_PORT,
) -> dict[str, Any]:
    """Return a MediaMTX configuration dict for these destinations.

    Only the surfaces the relay needs are enabled: RTMP ingest and the forward
    list. Everything else (RTSP, HLS, WebRTC, the HTTP API, recording) stays
    off so the relay is not an accidental open server.
    """
    if not destinations:
        raise RelayConfigError(
            "no destinations configured — nothing to forward to. "
            "Add one with `xpst stream set-key <platform>`."
        )
    if not 1 <= rtmp_port <= 65535:
        raise RelayConfigError(f"rtmp port {rtmp_port} is out of range")
    path_name = _validate_path_name(path_name)

    seen: dict[str, str] = {}
    forward: list[dict[str, str]] = []
    for dest in destinations:
        if dest.platform in seen:
            raise RelayConfigError(
                f"{dest.platform} is configured twice ({seen[dest.platform]} and "
                f"{dest.ingest_url}); one platform may only forward once"
            )
        seen[dest.platform] = dest.ingest_url
        forward.append({"dest": dest.forward_url})

    return {
        "logLevel": "info",
        "rtmp": True,
        "rtmpAddress": f":{rtmp_port}",
        "rtsp": False,
        "hls": False,
        "webrtc": False,
        "srt": False,
        "moq": False,
        "api": False,
        "paths": {
            path_name: {
                "source": "publisher",
                "forward": forward,
            }
        },
    }


def render_relay_config(config: dict[str, Any]) -> str:
    """YAML text for a relay config, with a header that names the secret risk."""
    header = (
        "# Generated by xPST — live-stream relay (MediaMTX).\n"
        "# This file contains stream keys. Keep it mode 0600 and never commit it.\n"
        "# Encoder setup: push one RTMP stream to rtmp://127.0.0.1:1935/<path>.\n"
    )
    body = yaml.safe_dump(config, sort_keys=False, default_flow_style=False)
    return header + body


def write_relay_config(
    path: str | Path,
    destinations: Sequence[RelayDestination],
    *,
    path_name: str = DEFAULT_PATH_NAME,
    rtmp_port: int = DEFAULT_RTMP_PORT,
) -> Path:
    """Write the relay config to ``path`` with owner-only permissions.

    The file holds live stream keys, so it is created 0600 before any content is
    written, and an *existing* file is narrowed to 0600 as well: ``O_CREAT`` only
    applies the mode to a file it creates, so regenerating over a config someone
    chmod-ed or copied in at 0644 would otherwise leave secrets world-readable.
    Windows has no POSIX mode bits, which is why the mode is requested through
    ``os.open``/``os.fchmod`` (best effort there) instead of being asserted.
    """
    config = build_relay_config(destinations, path_name=path_name, rtmp_port=rtmp_port)
    text = render_relay_config(config)
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(target, flags, 0o600)
    if hasattr(os, "fchmod"):
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return target


def relay_summary(
    destinations: Iterable[RelayDestination],
    *,
    path_name: str = DEFAULT_PATH_NAME,
    rtmp_port: int = DEFAULT_RTMP_PORT,
) -> dict[str, Any]:
    """Key-free description of what a relay would do — safe to print or log."""
    rows = [dest.masked() for dest in destinations]
    return {
        "ok": bool(rows),
        "relay_ingest_url": f"rtmp://127.0.0.1:{rtmp_port}/{path_name}",
        "destination_count": len(rows),
        "destinations": rows,
        "note": (
            "Each destination receives the same bytes; nothing is re-encoded. "
            "A platform that issues per-session keys (Instagram, TikTok, Rumble) "
            "must be re-pasted before every stream."
        ),
    }
