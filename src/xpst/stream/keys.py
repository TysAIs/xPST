"""Stream ingest credentials live in the encrypted store, like every other secret.

A stream key is a bearer credential: anyone who reads it can broadcast to that
account. It therefore never lands in ``config.yaml``, never in the relay JSON
output and never in a log line. The encrypted :class:`~xpst.utils.credentials.CredentialStore`
already used for cookies and tokens is the only home for it.

Stored shape, one entry per platform::

    key: "stream_destination_<platform>"
    value: {"ingest_url": "rtmps://…", "stream_key": "…"}

The entry key is namespaced so a future platform-mirroring feature cannot
collide with an account credential of the same platform name. The separator is an
underscore, not a colon, because the encrypted file store writes the key verbatim
as ``<key>.enc`` and Windows refuses a colon in a file name (``os.replace`` →
``WinError 87``). ``CredentialStore`` now rejects such a key outright instead of
failing on the rename.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xpst.stream.destinations import STREAM_INGEST_PATHS
from xpst.stream.ingest import fetch_readiness
from xpst.stream.relay import RelayConfigError, RelayDestination
from xpst.utils.credentials import CredentialStore

if TYPE_CHECKING:
    from pathlib import Path

KEY_PREFIX = "stream_destination_"


def _credential_key(platform: str) -> str:
    return f"{KEY_PREFIX}{platform}"


def store_destination(
    config_dir: str | Path,
    platform: str,
    ingest_url: str,
    stream_key: str,
) -> RelayDestination:
    """Validate and persist one destination; returns the validated object.

    Validation happens before the write, so a rejected key cannot leave a
    half-configured destination behind.
    """
    destination = RelayDestination(platform=platform, ingest_url=ingest_url, stream_key=stream_key)
    CredentialStore(str(config_dir)).store_json(
        _credential_key(destination.platform),
        {"ingest_url": destination.ingest_url, "stream_key": destination.stream_key},
    )
    return destination


def clear_destination(config_dir: str | Path, platform: str) -> bool:
    """Remove a stored destination. Returns whether anything was stored."""
    return CredentialStore(str(config_dir)).delete(_credential_key(platform.strip().lower()))


def load_destinations(config_dir: str | Path) -> list[RelayDestination]:
    """Load every stored destination, in registry order.

    A stored row that no longer validates (hand-edited store, retired platform,
    an ingest URL that is not RTMP) raises instead of being skipped: a relay
    that quietly forwards to fewer places than the operator configured is the
    failure mode this module exists to prevent.
    """
    store = CredentialStore(str(config_dir))
    stored: dict[str, RelayDestination] = {}
    for key in store.list_keys():
        if not key.startswith(KEY_PREFIX):
            continue
        platform = key[len(KEY_PREFIX) :]
        payload = store.retrieve_json(key) or {}
        try:
            stored[platform] = RelayDestination(
                platform=platform,
                ingest_url=str(payload.get("ingest_url", "")),
                stream_key=str(payload.get("stream_key", "")),
            )
        except RelayConfigError as exc:
            raise RelayConfigError(
                f"stored destination {platform!r} is unusable: {exc}. "
                f"Re-add it with `xpst stream set-key {platform}`."
            ) from exc
    order = {name: index for index, name in enumerate(STREAM_INGEST_PATHS)}
    return sorted(stored.values(), key=lambda dest: order.get(dest.platform, len(order)))


def destination_status(config_dir: str | Path, *, config: Any | None = None) -> dict[str, Any]:
    """Per-platform readiness for the relay, without any secret material.

    Each row answers two separate questions: is a key already stored
    (``key_present``), and could xPST fetch one itself (``fetch``)? They are not
    the same answer — a platform with credentials can fetch, a platform with a
    stored key is ready, and a manual platform has neither by design.

    ``config`` is optional and only sharpens the credential check for platforms
    whose token path can be configured (YouTube's ``token_file``); a caller
    without a loaded config still gets a correct answer from the credential
    store and the default file locations.
    """
    try:
        stored = {dest.platform: dest for dest in load_destinations(config_dir)}
        error: str | None = None
    except RelayConfigError as exc:
        stored = {}
        error = str(exc)

    rows: list[dict[str, Any]] = []
    encrypted = CredentialStore(str(config_dir))
    for platform, path in STREAM_INGEST_PATHS.items():
        configured = stored.get(platform)
        readiness = (
            fetch_readiness(config_dir, platform, store=encrypted, config=config) if path.api_available else None
        )
        rows.append(
            {
                "platform": platform,
                "display_name": path.display_name,
                "ingest_class": path.ingest_class,
                "key_present": configured is not None,
                "ingest_url": configured.ingest_url if configured else None,
                "operator_action": path.operator_action,
                "note": path.note,
                "fetch": readiness.to_dict() if readiness else None,
            }
        )
    return {
        "ok": error is None,
        "error": error,
        "configured": [row["platform"] for row in rows if row["key_present"]],
        "ready_to_relay": bool(stored) and error is None,
        "fetchable": [row["platform"] for row in rows if row["fetch"] and row["fetch"]["credentials_present"]],
        "missing_credentials": [
            {
                "platform": row["platform"],
                "missing": row["fetch"]["missing"],
                "action": row["fetch"]["action"],
                "registration_url": row["fetch"]["registration_url"],
            }
            for row in rows
            if row["fetch"] and not row["fetch"]["credentials_present"]
        ],
        "platforms": rows,
    }
