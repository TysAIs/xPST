"""Per-device credentials for a phone reaching the engine off-loopback.

The engine's write credential today is one shared API token stored in the
encrypted credential store. That is the wrong shape for a phone:

* the operator has to copy a 43-character secret onto the device by hand,
* revoking "the phone" means rotating the token every surface uses, and
* there is no record of *which* device is talking to the engine.

This module adds the missing primitive: a one-time pairing code shown by the
engine (``xpst pair``), exchanged once for a per-device bearer token with a
label, a last-seen timestamp and an independent revoke. Codes are short-lived,
single-use, attempt-limited and stored hashed; device tokens are stored hashed
too, so the file on disk cannot be replayed as a credential.

Storage is ``<config_dir>/devices.json`` (mode 0600), deliberately plain JSON in
the config dir rather than the Fernet credential store: the CLI writes a pairing
code in one process and the engine redeems it in another, and a hashed token
needs no encryption. Nothing here talks to the network.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Any

from xpst.utils.atomic import write_text_atomic

#: File holding pairing codes and paired devices, relative to the config dir.
DEVICES_FILENAME = "devices.json"

#: Pairing code lifetime. Long enough to walk to the phone, short enough that a
#: leaked code is dead before the next coffee.
DEFAULT_PAIR_TTL_S = 600

#: How many wrong code presentations the engine tolerates inside one pairing
#: window before it stops hashing guesses. Codes are 8 characters from a
#: 31-symbol alphabet (~8.5e11 combinations), so this is a second line of
#: defence rather than the primary one — but it must be real, and a per-code
#: counter cannot be: a wrong code matches no entry to count against.
MAX_CODE_ATTEMPTS = 5

#: Unambiguous alphabet: no 0/O, 1/I/L. Codes are read off a terminal screen.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8

#: Bytes of entropy in a device token (URL-safe, ~43 characters).
TOKEN_BYTES = 32

#: Do not touch disk on every authenticated request just to move last_seen.
LAST_SEEN_MIN_INTERVAL_S = 60


class PairingError(Exception):
    """A typed pairing refusal (unknown/expired/spent code, or bad token)."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


def devices_path(config_dir: str | Path) -> Path:
    """Return the devices file path for a config dir (not created)."""
    return Path(config_dir).expanduser() / DEVICES_FILENAME


def _now() -> float:
    return time.time()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load(config_dir: str | Path) -> dict[str, Any]:
    """Read the devices file, tolerating a missing/corrupt one.

    A corrupt file must not lock the operator out permanently: pairing state is
    a convenience, and an unreadable file simply means "nothing paired yet".
    """
    path = devices_path(config_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "pairing_codes": [], "devices": [], "failed_attempts": []}
    if not isinstance(data, dict):
        return {"version": 1, "pairing_codes": [], "devices": [], "failed_attempts": []}
    data.setdefault("version", 1)
    if not isinstance(data.get("pairing_codes"), list):
        data["pairing_codes"] = []
    if not isinstance(data.get("devices"), list):
        data["devices"] = []
    if not isinstance(data.get("failed_attempts"), list):
        data["failed_attempts"] = []
    return data


def _save(config_dir: str | Path, data: dict[str, Any]) -> None:
    path = devices_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=0o600)


def _normalize_code(code: str) -> str:
    """Strip separators/case from a code a human typed off a screen."""
    return "".join(ch for ch in str(code or "").upper() if ch.isalnum())


def _format_code(raw: str) -> str:
    return f"{raw[:4]}-{raw[4:]}" if len(raw) == CODE_LENGTH else raw


def create_pairing_code(
    config_dir: str | Path,
    *,
    label: str = "",
    ttl_s: int = DEFAULT_PAIR_TTL_S,
    code: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Create (and persist) a one-time pairing code.

    Returns the code in cleartext **once** — the file only stores its hash — so
    the caller (``xpst pair``) is the only place it is ever displayed.
    """
    ttl = max(30, int(ttl_s or DEFAULT_PAIR_TTL_S))
    issued = float(now if now is not None else _now())
    raw = _normalize_code(code) if code else "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    if len(raw) != CODE_LENGTH:
        raise PairingError("PAIR_CODE_SHAPE", f"A pairing code must be {CODE_LENGTH} characters.")
    data = _load(config_dir)
    # Drop expired and spent codes; leaving them forever lets the file grow and
    # makes "no active code" ambiguous.
    data["pairing_codes"] = [
        item
        for item in data["pairing_codes"]
        if not item.get("redeemed_at") and float(item.get("expires_at") or 0) > issued
    ]
    entry = {
        "code_sha256": _sha256(raw),
        "label": str(label or "")[:80],
        "created_at": issued,
        "expires_at": issued + ttl,
        "attempts": 0,
        "redeemed_at": None,
        "device_id": None,
    }
    data["pairing_codes"].append(entry)
    # Issuing a code proves a human is at the engine machine, which is exactly
    # what the guess budget is protecting: a fresh code starts a fresh budget.
    # Without this, "run `xpst pair` for a fresh one" would be a lie while the
    # presentation limit was still spent.
    data["failed_attempts"] = []
    _save(config_dir, data)
    return {
        "code": _format_code(raw),
        "label": entry["label"],
        "created_at": issued,
        "expires_at": entry["expires_at"],
        "expires_in_s": ttl,
    }


def redeem_pairing_code(
    config_dir: str | Path,
    code: str,
    *,
    device_label: str = "",
    client: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    """Exchange a pairing code for a device token.

    Raises:
        PairingError: unknown code, expired code, spent code, or too many
            attempts. The presented code itself is never echoed in the message.
    """
    presented = _normalize_code(code)
    stamp = float(now if now is not None else _now())
    data = _load(config_dir)
    # Failed presentations are counted over the code lifetime, so a client that
    # guesses cannot spin forever and a stale burst cannot lock the operator out
    # the next morning.
    window_start = stamp - float(DEFAULT_PAIR_TTL_S)
    data["failed_attempts"] = [
        float(item) for item in data.get("failed_attempts") or [] if float(item) >= window_start
    ]
    if len(data["failed_attempts"]) >= MAX_CODE_ATTEMPTS:
        _save(config_dir, data)
        raise PairingError(
            "PAIR_TOO_MANY_ATTEMPTS",
            "Too many wrong pairing codes. Run `xpst pair` on the engine machine for a fresh one.",
            status_code=429,
        )
    digest = _sha256(presented)
    entry = None
    for candidate in data["pairing_codes"]:
        if hmac.compare_digest(str(candidate.get("code_sha256") or ""), digest):
            entry = candidate
            break
    if entry is None:
        data["failed_attempts"].append(stamp)
        _save(config_dir, data)
        raise PairingError(
            "PAIR_CODE_UNKNOWN",
            "That pairing code is not valid. Run `xpst pair` on the engine machine for a fresh one.",
            status_code=401,
        )
    if entry.get("redeemed_at"):
        raise PairingError(
            "PAIR_CODE_SPENT",
            "That pairing code was already used. Run `xpst pair` again for a new one.",
            status_code=401,
        )
    if float(entry.get("expires_at") or 0) <= stamp:
        raise PairingError(
            "PAIR_CODE_EXPIRED",
            "That pairing code expired. Run `xpst pair` again for a fresh one.",
            status_code=401,
        )

    token = secrets.token_urlsafe(TOKEN_BYTES)
    device_id = f"dev_{secrets.token_hex(6)}"
    label = str(device_label or entry.get("label") or "phone")[:80]
    device = {
        "device_id": device_id,
        "label": label,
        "token_sha256": _sha256(token),
        "created_at": stamp,
        "last_seen_at": stamp,
        "client": str(client or "")[:120],
    }
    data["devices"].append(device)
    entry["redeemed_at"] = stamp
    entry["device_id"] = device_id
    entry["attempts"] = int(entry.get("attempts") or 0) + 1
    # A good code proves the operator is there: start the guess budget fresh.
    data["failed_attempts"] = []
    _save(config_dir, data)
    return {
        "device": _public_device(device),
        "token": token,
        "message": f"Paired '{label}'. This token is shown once; store it on the device.",
    }


def _public_device(device: dict[str, Any]) -> dict[str, Any]:
    """A device record with the token hash removed — the only safe shape."""
    return {
        "device_id": device.get("device_id"),
        "label": device.get("label"),
        "created_at": device.get("created_at"),
        "last_seen_at": device.get("last_seen_at"),
        "client": device.get("client") or "",
        "revoked": bool(device.get("revoked")),
    }


def list_devices(config_dir: str | Path) -> list[dict[str, Any]]:
    """Return paired devices (newest first), never their tokens."""
    devices = [_public_device(item) for item in _load(config_dir).get("devices", [])]
    devices.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
    return devices


def revoke_device(config_dir: str | Path, device_id: str, *, now: float | None = None) -> bool:
    """Revoke one device. Returns ``True`` when a live device was revoked."""
    target = str(device_id or "").strip()
    stamp = float(now if now is not None else _now())
    data = _load(config_dir)
    for device in data.get("devices", []):
        if device.get("device_id") == target:
            if device.get("revoked"):
                return False
            device["revoked"] = True
            device["revoked_at"] = stamp
            _save(config_dir, data)
            return True
    return False


def verify_device_token(
    config_dir: str | Path,
    token: str | None,
    *,
    now: float | None = None,
    record_last_seen: bool = True,
) -> dict[str, Any] | None:
    """Return the live device for a presented token, or ``None``.

    Comparison is constant-time over the stored hash. ``last_seen_at`` is moved
    at most once per :data:`LAST_SEEN_MIN_INTERVAL_S` so authentication does not
    rewrite the file on every request.
    """
    presented = str(token or "").strip()
    if not presented:
        return None
    digest = _sha256(presented)
    stamp = float(now if now is not None else _now())
    data = _load(config_dir)
    matched: dict[str, Any] | None = None
    for device in data.get("devices", []):
        if device.get("revoked"):
            continue
        if hmac.compare_digest(str(device.get("token_sha256") or ""), digest):
            matched = device
            break
    if matched is None:
        return None
    if record_last_seen and stamp - float(matched.get("last_seen_at") or 0) >= LAST_SEEN_MIN_INTERVAL_S:
        matched["last_seen_at"] = stamp
        try:
            _save(config_dir, data)
        except OSError:  # noqa: BLE001 - a last-seen write must never fail auth
            pass
    return _public_device(matched)


__all__ = [
    "CODE_ALPHABET",
    "DEVICES_FILENAME",
    "DEFAULT_PAIR_TTL_S",
    "MAX_CODE_ATTEMPTS",
    "PairingError",
    "create_pairing_code",
    "devices_path",
    "list_devices",
    "redeem_pairing_code",
    "revoke_device",
    "verify_device_token",
]
