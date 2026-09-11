"""Durable, provider-neutral setup transactions.

The setup transaction is the state machine shared by CLI clients today and UI/MCP
clients later.  It deliberately knows nothing about provider probes or
credentials.  A caller supplies verified role readiness through the adapter
seam; this module only persists the safe result and enforces the completion
invariant.

Only non-secret identifiers and human-action instructions are persisted.  OAuth
values, account identifiers, email addresses, local paths, provider responses,
and exception text are intentionally not part of the schema.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urldefrag, urlsplit, urlunsplit

SCHEMA_VERSION = 1
SETUP_TRANSACTION_SCHEMA_VERSION = SCHEMA_VERSION
TRANSACTION_FILENAME = "setup_transaction.json"
SETUP_TRANSACTION_FILENAME = TRANSACTION_FILENAME
BACKUP_FILENAME = f"{TRANSACTION_FILENAME}.bak"
TEMP_FILENAME = f"{TRANSACTION_FILENAME}.tmp"

SOURCE_ROLE = "source"
VIDEO_DESTINATION_ROLE = "video_destination"
ALLOWED_ROLES = frozenset({SOURCE_ROLE, VIDEO_DESTINATION_ROLE, "analytics", "messaging"})
STEP_STATES = frozenset({"pending", "in_progress", "waiting_human", "verified", "skipped", "error"})
COMPLETION_STATES = frozenset({"in_progress", "finish_later", "completed"})

DEFAULT_ROLE_CAPABILITIES: tuple[dict[str, str], ...] = (
    {"role": SOURCE_ROLE, "capability": "tiktok"},
    {"role": VIDEO_DESTINATION_ROLE, "capability": "youtube"},
    {"role": VIDEO_DESTINATION_ROLE, "capability": "x"},
    {"role": VIDEO_DESTINATION_ROLE, "capability": "instagram"},
    {"role": VIDEO_DESTINATION_ROLE, "capability": "threads"},
)

# Public JSON-schema-shaped declaration for UI/MCP consumers.  The runtime
# validator below is deliberately stricter about identifiers and secret-like
# input than JSON Schema alone can express.
TRANSACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "schema_version",
        "transaction_id",
        "selected_role_capabilities",
        "steps",
        "created_at",
        "updated_at",
        "state",
        "readiness",
        "completion",
        "errors",
    ],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "transaction_id": {"type": "string"},
        "selected_role_capabilities": {"type": "array"},
        "steps": {"type": "array"},
        "created_at": {"type": "string"},
        "updated_at": {"type": "string"},
        "state": {"enum": sorted(COMPLETION_STATES)},
        "readiness": {"type": "object"},
        "completion": {"type": "object"},
        "errors": {"type": "array"},
        "human_actions": {"type": "array"},
    },
}

_SAFE_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_.-]{1,63}$")
_SECRET_WORDS = (
    "token",
    "secret",
    "password",
    "cookie",
    "credential",
    "oauth",
    "email",
    "account_id",
    "user_id",
    "access_key",
    "refresh",
)

_ERROR_MESSAGES: dict[str, tuple[str, str]] = {
    "AUTH_REQUIRED": (
        "Human approval is required for this capability.",
        "Complete the listed human action, then resume the setup transaction.",
    ),
    "READINESS_NOT_VERIFIED": (
        "A live readiness verification is still required.",
        "Verify at least one source and one live-ready video destination, then resume.",
    ),
    "READINESS_NOT_SELECTED": (
        "The readiness result does not match a selected role-capability.",
        "Select the capability first, then submit its live readiness result.",
    ),
    "STATE_RECOVERED": (
        "The previous setup state was unreadable and was recovered safely.",
        "Review the pending actions and resume setup.",
    ),
    "INVALID_COMPLETION": (
        "The saved completion marker did not include both required readiness roles.",
        "Verify a source and a live-ready video destination before finishing setup.",
    ),
    "RESET": (
        "The setup transaction was reset before completion.",
        "Start setup again when you are ready.",
    ),
}


class ReadinessAdapter(Protocol):
    """Optional provider-neutral seam for a live readiness implementation."""

    def verify(self, selected_role_capabilities: list[dict[str, str]]) -> Mapping[str, Any]:
        """Return safe role-capability readiness data."""


class SetupTransactionError(ValueError):
    """Base error for invalid setup transaction operations."""


class SetupTransactionNotFoundError(SetupTransactionError):
    """Raised when resume/verification is requested without an active transaction."""


# Short aliases are kept for callers that adopted the initial service API.
SetupTransactionNotFound = SetupTransactionNotFoundError


class SetupTransactionCorruptError(SetupTransactionError):
    """Raised internally when persisted JSON is not a transaction document."""


SetupTransactionCorrupt = SetupTransactionCorruptError


class _LoadResult:
    def __init__(self, state: dict[str, Any] | None, recovered: bool = False) -> None:
        self.state = state
        self.recovered = recovered


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_identifier(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise SetupTransactionError(f"{field} must be a safe identifier")
    normalized = value.strip().lower()
    if not _SAFE_IDENTIFIER.fullmatch(normalized) or any(word in normalized for word in _SECRET_WORDS):
        raise SetupTransactionError(f"{field} must be a non-secret capability identifier")
    return normalized


def _safe_role(value: Any) -> str:
    role = _safe_identifier(value, field="role")
    if role not in ALLOWED_ROLES:
        raise SetupTransactionError(f"Unsupported setup role: {role}")
    return role


def _safe_code(value: Any) -> str:
    if not isinstance(value, str):
        return "SETUP_ERROR"
    code = value.strip().upper()
    return code if _SAFE_CODE.fullmatch(code) and not any(word.upper() in code for word in _SECRET_WORDS) else "SETUP_ERROR"


def _safe_error(value: Any) -> dict[str, str]:
    """Build an error from an allowlisted code; never persist caller text."""
    code = _safe_code(value.get("code")) if isinstance(value, Mapping) else _safe_code(value)
    message, action = _ERROR_MESSAGES.get(code, ("Setup needs attention.", "Review the pending setup action and resume."))
    return {"code": code, "message": message, "action": action}


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    # Query strings/fragments are common places for one-time codes.  Keep only
    # an origin/path that is safe to show to a human.
    clean, _ = urldefrag(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")))
    return clean[:512]


def _safe_human_action(value: Mapping[str, Any], *, role: str, capability: str, state: str = "pending") -> dict[str, Any]:
    action_id = value.get("action_id", f"connect_{role}_{capability}")
    action_id = _safe_identifier(action_id, field="action_id")
    kind = value.get("kind", "connect")
    kind = _safe_identifier(kind, field="action kind")
    # Labels/instructions are generated from known identifiers rather than
    # copying arbitrary provider or user text into durable state.
    label = f"Connect {capability} as {role.replace('_', ' ')}"
    instructions = "Complete the provider's human approval, then resume this setup transaction."
    docs_url = _safe_url(value.get("url"))
    return {
        "action_id": action_id,
        "kind": kind,
        "label": label,
        "instructions": instructions,
        "url": docs_url,
        "status": "completed" if state == "verified" else "pending",
    }


def normalize_role_capabilities(values: Iterable[Any] | None) -> list[dict[str, str]]:
    """Normalize role-capability selections to a stable, non-secret shape."""
    if values is None:
        values = DEFAULT_ROLE_CAPABILITIES
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in values:
        entries: list[tuple[Any, Any]] = []
        if isinstance(item, str):
            if ":" not in item:
                raise SetupTransactionError("Role-capabilities must use 'role:capability' notation")
            entries.append(tuple(item.split(":", 1)))
        elif isinstance(item, Mapping):
            if "role" in item and "capability" in item:
                entries.append((item["role"], item["capability"]))
            else:
                # Friendly UI shape: {"source": ["tiktok"], ...}.
                for role, capabilities in item.items():
                    if isinstance(capabilities, str):
                        capabilities = [capabilities]
                    if isinstance(capabilities, Iterable):
                        entries.extend((role, capability) for capability in capabilities)
        else:
            raise SetupTransactionError("Each selected role-capability must be an object or role:capability string")
        for role_value, capability_value in entries:
            role = _safe_role(role_value)
            capability = _safe_identifier(capability_value, field="capability")
            key = (role, capability)
            if key not in seen:
                normalized.append({"role": role, "capability": capability})
                seen.add(key)
    if not normalized:
        raise SetupTransactionError("At least one role-capability must be selected")
    return normalized


def _safe_readiness_entry(value: Any, *, capability: str, verified_at: str) -> dict[str, Any]:
    verified = True
    live_ready = True
    if isinstance(value, Mapping):
        readiness_state = str(value.get("state", value.get("status", ""))).lower()
        verified = bool(value.get("verified", value.get("ready", readiness_state in {"ready", "verified", "live_ready"})))
        live_ready = bool(value.get("live_ready", value.get("live", readiness_state in {"ready", "live_ready"} or verified)))
    elif isinstance(value, bool):
        verified = value
        live_ready = value
    return {
        "capability": capability,
        "verified": verified,
        "live_ready": live_ready if verified else False,
        "verified_at": verified_at if verified and live_ready else None,
    }


def _normalize_readiness_input(
    readiness: Mapping[str, Any] | None,
    *,
    sources: Iterable[Any] | None = None,
    video_destinations: Iterable[Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    data = dict(readiness or {})
    source_values = sources if sources is not None else data.get(
        "sources", data.get("verified_sources", data.get(SOURCE_ROLE, []))
    )
    destination_values = (
        video_destinations
        if video_destinations is not None
        else data.get(
            "video_destinations",
            data.get("verified_video_destinations", data.get(VIDEO_DESTINATION_ROLE, [])),
        )
    )

    def values(value: Any) -> list[Any]:
        if isinstance(value, Mapping):
            return [{"capability": key, **(item if isinstance(item, Mapping) else {"verified": item})} for key, item in value.items()]
        if isinstance(value, str):
            return [value]
        if value is None:
            return []
        return list(value)

    timestamp = _now()
    output: dict[str, list[dict[str, Any]]] = {"sources": [], "video_destinations": []}
    for role, raw_values in ((SOURCE_ROLE, values(source_values)), (VIDEO_DESTINATION_ROLE, values(destination_values))):
        output_key = "sources" if role == SOURCE_ROLE else "video_destinations"
        for raw in raw_values:
            if isinstance(raw, Mapping):
                capability = _safe_identifier(raw.get("capability"), field="readiness capability")
            else:
                capability = _safe_identifier(raw, field="readiness capability")
            output[output_key].append(_safe_readiness_entry(raw, capability=capability, verified_at=timestamp))
    return output


def _new_readiness() -> dict[str, Any]:
    return {
        "state": "unverified",
        "sources": [],
        "video_destinations": [],
        "verified_at": None,
    }


def _readiness_state(readiness: Mapping[str, Any]) -> str:
    source_ready = any(bool(item.get("verified")) and bool(item.get("live_ready")) for item in readiness.get("sources", []))
    destination_ready = any(
        bool(item.get("verified")) and bool(item.get("live_ready"))
        for item in readiness.get("video_destinations", [])
    )
    if source_ready and destination_ready:
        return "ready"
    if source_ready or destination_ready:
        return "partial"
    return "unverified"


def _completion(readiness: Mapping[str, Any], state: str) -> dict[str, Any]:
    ready = _readiness_state(readiness) == "ready"
    if ready:
        return {"state": "completed", "complete": True, "resumable": False, "reason": None}
    if state == "finish_later":
        return {
            "state": "finish_later",
            "complete": False,
            "resumable": True,
            "reason": "Setup was explicitly deferred and can be resumed.",
        }
    return {
        "state": "in_progress",
        "complete": False,
        "resumable": True,
        "reason": "Verify at least one source and one live-ready video destination.",
    }


def _pending_actions(transaction: Mapping[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for step in transaction.get("steps", []):
        if step.get("state") != "verified":
            action = step.get("human_action")
            if isinstance(action, Mapping):
                actions.append(dict(action))
    return actions


def _validate_transaction(transaction: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(transaction, Mapping) or transaction.get("schema_version") != SCHEMA_VERSION:
        raise SetupTransactionCorrupt("Unsupported setup transaction schema")
    required = {
        "schema_version",
        "transaction_id",
        "selected_role_capabilities",
        "steps",
        "created_at",
        "updated_at",
        "state",
        "completion",
        "readiness",
        "errors",
    }
    if not required.issubset(transaction):
        raise SetupTransactionCorrupt("Incomplete setup transaction")
    allowed = required | {"pending_human_actions", "human_actions"}
    if set(transaction) - allowed:
        raise SetupTransactionCorrupt("Unknown setup transaction fields")
    try:
        uuid.UUID(str(transaction["transaction_id"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise SetupTransactionCorrupt("Invalid setup transaction identifier") from exc
    selected = transaction["selected_role_capabilities"]
    steps = transaction["steps"]
    readiness = transaction["readiness"]
    completion = transaction["completion"]
    errors = transaction["errors"]
    if not isinstance(selected, list) or not selected:
        raise SetupTransactionCorrupt("Invalid selected role-capabilities")
    for item in selected:
        if not isinstance(item, Mapping) or set(item) != {"role", "capability"}:
            raise SetupTransactionCorrupt("Invalid selected role-capability")
        role = item.get("role")
        capability = item.get("capability")
        if (
            not isinstance(role, str)
            or role not in ALLOWED_ROLES
            or not isinstance(capability, str)
            or not _SAFE_IDENTIFIER.fullmatch(capability)
            or any(word in capability for word in _SECRET_WORDS)
        ):
            raise SetupTransactionCorrupt("Invalid selected role-capability")
    if not isinstance(steps, list) or not all(isinstance(step, Mapping) for step in steps):
        raise SetupTransactionCorrupt("Invalid setup steps")
    for step in steps:
        expected_step_keys = {
            "step_id",
            "role",
            "capability",
            "state",
            "human_action",
            "started_at",
            "completed_at",
            "updated_at",
            "error",
        }
        if set(step) != expected_step_keys:
            raise SetupTransactionCorrupt("Incomplete setup step")
        if (
            not isinstance(step.get("state"), str)
            or step.get("state") not in STEP_STATES
            or not isinstance(step.get("human_action"), Mapping)
        ):
            raise SetupTransactionCorrupt("Invalid setup step state")
        if (
            not isinstance(step.get("step_id"), str)
            or not _SAFE_IDENTIFIER.fullmatch(step["step_id"])
            or step.get("role") not in ALLOWED_ROLES
            or not isinstance(step.get("capability"), str)
            or not _SAFE_IDENTIFIER.fullmatch(step["capability"])
            or any(word in step["capability"] for word in _SECRET_WORDS)
        ):
            raise SetupTransactionCorrupt("Unsafe setup step identifier")
        action = step["human_action"]
        if set(action) != {"action_id", "kind", "label", "instructions", "url", "status"}:
            raise SetupTransactionCorrupt("Invalid setup human action")
        if (
            not isinstance(action["action_id"], str)
            or not _SAFE_IDENTIFIER.fullmatch(action["action_id"])
            or any(word in action["action_id"] for word in _SECRET_WORDS)
            or not isinstance(action["kind"], str)
            or not _SAFE_IDENTIFIER.fullmatch(action["kind"])
            or not isinstance(action["status"], str)
            or action["status"] not in {"pending", "completed"}
            or action["label"] != f"Connect {step['capability']} as {step['role'].replace('_', ' ')}"
            or action["instructions"] != "Complete the provider's human approval, then resume this setup transaction."
            or action["url"] is not None and _safe_url(action["url"]) != action["url"]
            or step["error"] is not None
        ):
            raise SetupTransactionCorrupt("Unsafe setup human action")
    if not isinstance(readiness, Mapping) or not isinstance(readiness.get("sources"), list) or not isinstance(readiness.get("video_destinations"), list):
        raise SetupTransactionCorrupt("Invalid setup readiness")
    for readiness_key in ("sources", "video_destinations"):
        for item in readiness[readiness_key]:
            if (
                not isinstance(item, Mapping)
                or set(item) != {"capability", "verified", "live_ready", "verified_at"}
                or not isinstance(item.get("capability"), str)
                or not _SAFE_IDENTIFIER.fullmatch(item["capability"])
                or any(word in item["capability"] for word in _SECRET_WORDS)
                or not isinstance(item.get("verified"), bool)
                or not isinstance(item.get("live_ready"), bool)
                or item.get("verified_at") is not None and not isinstance(item.get("verified_at"), str)
            ):
                raise SetupTransactionCorrupt("Unsafe setup readiness")
    if not isinstance(completion, Mapping) or completion.get("state") not in COMPLETION_STATES:
        raise SetupTransactionCorrupt("Invalid setup completion state")
    if not isinstance(errors, list) or not all(isinstance(error, Mapping) for error in errors):
        raise SetupTransactionCorrupt("Invalid setup errors")
    for error in errors:
        if set(error) != {"code", "message", "action"} or dict(error) != _safe_error(error.get("code")):
            raise SetupTransactionCorrupt("Unsafe setup error")
    return dict(transaction)


class SetupTransactionStore:
    """Atomic JSON persistence with a last-known-good recovery copy."""

    def __init__(self, config_dir: Any) -> None:
        if hasattr(config_dir, "config_dir"):
            config_dir = config_dir.config_dir
        self.config_dir = Path(config_dir).expanduser()
        self.path = self.config_dir / TRANSACTION_FILENAME
        self.backup_path = self.config_dir / BACKUP_FILENAME
        self.temp_path = self.config_dir / TEMP_FILENAME

    def _atomic_write_bytes(self, path: Path, payload: bytes) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=self.config_dir)
        temp = Path(name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            try:
                directory_fd = os.open(self.config_dir, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def save(self, transaction: Mapping[str, Any]) -> None:
        checked = _validate_transaction(transaction)
        payload = json.dumps(checked, sort_keys=True, indent=2, ensure_ascii=True).encode("utf-8")
        existing: bytes | None = None
        try:
            existing = self.path.read_bytes()
            _validate_transaction(json.loads(existing.decode("utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, SetupTransactionCorrupt):
            existing = None
        if existing is not None:
            self._atomic_write_bytes(self.backup_path, existing)
        self._atomic_write_bytes(self.path, payload)
        if existing is None:
            # A first write still gets a recovery point.  It is also useful when
            # a process dies immediately after the first successful transaction.
            self._atomic_write_bytes(self.backup_path, payload)

    def load(self) -> _LoadResult:
        candidates = (self.path, self.backup_path, self.temp_path)
        primary_error = False
        for index, candidate in enumerate(candidates):
            try:
                raw = candidate.read_text(encoding="utf-8")
                state = _validate_transaction(json.loads(raw))
                recovered = index != 0
                if recovered:
                    # Restore the known-good document through the same atomic
                    # path; never replace a corrupt file in place.
                    self._atomic_write_bytes(self.path, json.dumps(state, sort_keys=True, indent=2).encode("utf-8"))
                return _LoadResult(state, recovered=recovered or primary_error)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, SetupTransactionCorrupt):
                if index == 0:
                    primary_error = True
        return _LoadResult(None, recovered=primary_error)

    def reset(self) -> None:
        for path in (self.path, self.backup_path, self.temp_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        for path in self.config_dir.glob(f".{TRANSACTION_FILENAME}.*.tmp"):
            try:
                path.unlink()
            except OSError:
                pass


class SetupTransactionService:
    """Provider-neutral setup transaction service.

    ``readiness_adapter`` is intentionally optional.  Without one, the
    service never probes a platform and can only report pending human actions.
    """

    def __init__(
        self,
        config_dir: str | Path,
        readiness_adapter: ReadinessAdapter | Callable[[list[dict[str, str]]], Mapping[str, Any]] | None = None,
        *,
        readiness_checker: ReadinessAdapter | Callable[[list[dict[str, str]]], Mapping[str, Any]] | None = None,
    ) -> None:
        self.store = SetupTransactionStore(config_dir)
        self.readiness_adapter = readiness_adapter or readiness_checker
        self._state_recovery_pending = False

    def _load(self) -> dict[str, Any] | None:
        result = self.store.load()
        self._state_recovery_pending = result.recovered
        transaction = result.state
        if transaction is None:
            return None
        if result.recovered:
            errors = transaction.setdefault("errors", [])
            if not any(error.get("code") == "STATE_RECOVERED" for error in errors if isinstance(error, Mapping)):
                errors.append(_safe_error("STATE_RECOVERED"))
        self._reconcile(transaction)
        if result.recovered:
            self.store.save(transaction)
        return transaction

    @staticmethod
    def _reconcile(transaction: dict[str, Any]) -> None:
        readiness = transaction.get("readiness")
        if not isinstance(readiness, dict):
            readiness = _new_readiness()
            transaction["readiness"] = readiness
        readiness["state"] = _readiness_state(readiness)
        verified_at = [
            item.get("verified_at")
            for role in (SOURCE_ROLE, VIDEO_DESTINATION_ROLE)
            for item in readiness.get(role, [])
            if item.get("verified_at")
        ]
        readiness["verified_at"] = max(verified_at) if verified_at else None
        if not isinstance(transaction.get("state"), str) or transaction.get("state") not in COMPLETION_STATES:
            transaction["state"] = "in_progress"
        transaction["completion"] = _completion(readiness, transaction["state"])
        # Completion is derived, never trusted from disk.
        if transaction["completion"]["complete"]:
            transaction["state"] = "completed"
            transaction["completion"] = _completion(readiness, "completed")
        elif transaction["state"] == "completed":
            transaction["state"] = "in_progress"
            transaction["completion"] = _completion(readiness, "in_progress")
            errors = transaction.setdefault("errors", [])
            if not any(error.get("code") == "INVALID_COMPLETION" for error in errors if isinstance(error, Mapping)):
                errors.append(_safe_error("INVALID_COMPLETION"))
        transaction["pending_human_actions"] = _pending_actions(transaction)
        transaction["human_actions"] = list(transaction["pending_human_actions"])
        transaction["updated_at"] = transaction.get("updated_at") or _now()

    def _new(self, selected_role_capabilities: Iterable[Any] | None) -> dict[str, Any]:
        selected = normalize_role_capabilities(selected_role_capabilities)
        timestamp = _now()
        steps: list[dict[str, Any]] = []
        for item in selected:
            role = item["role"]
            capability = item["capability"]
            step_id = f"verify_{role}_{capability}"
            steps.append(
                {
                    "step_id": step_id,
                    "role": role,
                    "capability": capability,
                    "state": "pending",
                    "human_action": _safe_human_action({}, role=role, capability=capability),
                    "started_at": None,
                    "completed_at": None,
                    "updated_at": timestamp,
                    "error": None,
                }
            )
        transaction: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "transaction_id": str(uuid.uuid4()),
            "selected_role_capabilities": selected,
            "steps": steps,
            "created_at": timestamp,
            "updated_at": timestamp,
            "state": "in_progress",
            "readiness": _new_readiness(),
            "completion": _completion(_new_readiness(), "in_progress"),
            "errors": [],
        }
        transaction["pending_human_actions"] = _pending_actions(transaction)
        transaction["human_actions"] = list(transaction["pending_human_actions"])
        return transaction

    def _require(self, transaction_id: str | None = None) -> dict[str, Any]:
        transaction = self._load()
        if transaction is None:
            raise SetupTransactionNotFound("No setup transaction exists; start setup first.")
        if transaction_id is not None and transaction.get("transaction_id") != transaction_id:
            raise SetupTransactionNotFound("The requested setup transaction is not active.")
        return transaction

    @staticmethod
    def _touch_step(transaction: dict[str, Any], step_id: str, state: str) -> None:
        if state not in STEP_STATES:
            raise SetupTransactionError(f"Unsupported setup step state: {state}")
        for step in transaction["steps"]:
            if step.get("step_id") == step_id:
                timestamp = _now()
                if step.get("started_at") is None:
                    step["started_at"] = timestamp
                step["state"] = state
                step["updated_at"] = timestamp
                step["completed_at"] = timestamp if state == "verified" else None
                action = step.get("human_action")
                if isinstance(action, dict):
                    action["status"] = "completed" if state == "verified" else "pending"
                return
        raise SetupTransactionError("The requested setup step is not in this transaction.")

    @staticmethod
    def _merge_readiness(transaction: dict[str, Any], update: dict[str, list[dict[str, Any]]]) -> None:
        selected = {(item["role"], item["capability"]) for item in transaction["selected_role_capabilities"]}
        readiness = transaction["readiness"]
        timestamp = _now()
        for role in (SOURCE_ROLE, VIDEO_DESTINATION_ROLE):
            update_key = "sources" if role == SOURCE_ROLE else "video_destinations"
            for item in update.get(update_key, []):
                key = (role, item["capability"])
                if key not in selected:
                    error = _safe_error("READINESS_NOT_SELECTED")
                    if not any(existing.get("code") == error["code"] for existing in transaction["errors"]):
                        transaction["errors"].append(error)
                    continue
                items = [existing for existing in readiness[update_key] if existing.get("capability") != item["capability"]]
                items.append(item)
                readiness[update_key] = items
                for step in transaction["steps"]:
                    if step.get("role") == role and step.get("capability") == item["capability"]:
                        SetupTransactionService._touch_step(
                            transaction,
                            step["step_id"],
                            "verified" if item["verified"] and item["live_ready"] else "error",
                        )
        readiness["verified_at"] = timestamp

    def start(
        self,
        selected_role_capabilities: Iterable[Any] | None = None,
        *,
        role_capabilities: Iterable[Any] | None = None,
        readiness: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start a transaction, or return the active one so aliases resume it."""
        existing = self._load()
        if existing is not None:
            return existing
        selected = selected_role_capabilities if selected_role_capabilities is not None else role_capabilities
        transaction = self._new(selected)
        if self._state_recovery_pending:
            transaction["errors"].append(_safe_error("STATE_RECOVERED"))
        if readiness is not None:
            self._merge_readiness(transaction, _normalize_readiness_input(readiness))
        self._reconcile(transaction)
        self.store.save(transaction)
        self._state_recovery_pending = False
        return transaction

    def status(self, transaction_id: str | None = None) -> dict[str, Any] | None:
        """Return the active transaction without creating one."""
        transaction = self._load()
        if transaction is None:
            return None
        if transaction_id is not None and transaction.get("transaction_id") != transaction_id:
            return None
        return transaction

    def resume(
        self,
        transaction_id: str | None = None,
        *,
        step_id: str | None = None,
        step_state: str | None = None,
        step_updates: Mapping[str, Any] | None = None,
        readiness: Mapping[str, Any] | None = None,
        finish_later: bool = False,
        error: Mapping[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        """Apply safe progress/readiness data and persist the same transaction."""
        transaction = self._require(transaction_id)
        if transaction["state"] == "completed":
            return transaction
        if step_id is not None:
            if step_state is None:
                raise SetupTransactionError("step_state is required with step_id")
            self._touch_step(transaction, step_id, step_state)
        if step_updates:
            for updated_step_id, value in step_updates.items():
                state = value.get("state") if isinstance(value, Mapping) else value
                self._touch_step(transaction, str(updated_step_id), str(state))
        if readiness is not None:
            self._merge_readiness(transaction, _normalize_readiness_input(readiness))
        if error is not None:
            safe_error = _safe_error(error)
            if not any(existing.get("code") == safe_error["code"] for existing in transaction["errors"]):
                transaction["errors"].append(safe_error)
        if self.readiness_adapter is not None and readiness is None:
            adapter = self.readiness_adapter
            selected = transaction["selected_role_capabilities"]
            if hasattr(adapter, "verify"):
                raw = adapter.verify(selected)  # type: ignore[union-attr]
            elif hasattr(adapter, "verify_readiness"):
                raw = adapter.verify_readiness(selected)  # type: ignore[union-attr]
            else:
                raw = adapter(selected)  # type: ignore[operator]
            self._merge_readiness(transaction, _normalize_readiness_input(raw))
        transaction["state"] = "finish_later" if finish_later else "in_progress"
        self._reconcile(transaction)
        transaction["updated_at"] = _now()
        self.store.save(transaction)
        return transaction

    def record_readiness(
        self,
        readiness: Mapping[str, Any] | None = None,
        *,
        sources: Iterable[Any] | None = None,
        video_destinations: Iterable[Any] | None = None,
    ) -> dict[str, Any]:
        """Record caller-supplied live verification; no provider probe is made."""
        update = _normalize_readiness_input(readiness, sources=sources, video_destinations=video_destinations)
        return self.resume(readiness=update)

    def finish_later(self, transaction_id: str | None = None) -> dict[str, Any]:
        """Explicitly defer setup while keeping it visible and resumable."""
        return self.resume(transaction_id, finish_later=True)

    def reset(self, transaction_id: str | None = None) -> dict[str, Any]:
        """Delete the active transaction and all recovery copies."""
        existing = self.status()
        if transaction_id is not None and (existing is None or existing.get("transaction_id") != transaction_id):
            raise SetupTransactionNotFound("The requested setup transaction is not active.")
        old_id = existing.get("transaction_id") if existing else None
        self.store.reset()
        return {
            "schema_version": SCHEMA_VERSION,
            "state": "reset",
            "transaction_id": old_id,
            "completion": {"state": "in_progress", "complete": False, "resumable": False, "reason": "No active setup transaction."},
            "pending_human_actions": [],
        }

    def start_transaction(self, selected_role_capabilities: Iterable[Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.start(selected_role_capabilities, **kwargs)

    def get_status(self, transaction_id: str | None = None) -> dict[str, Any] | None:
        return self.status(transaction_id)

    def resume_transaction(self, transaction_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.resume(transaction_id, **kwargs)

    def reset_transaction(self, transaction_id: str | None = None) -> dict[str, Any]:
        return self.reset(transaction_id)

    @staticmethod
    def _response(operation: str, transaction: Mapping[str, Any] | None, *, alias: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": True, "operation": operation}
        if transaction is None:
            payload.update({"transaction": None, "transaction_id": None, "pending_human_actions": []})
        else:
            clean = dict(transaction)
            payload.update(clean)
            payload["transaction"] = clean
        if alias is not None:
            payload["alias"] = dict(alias)
        return payload

    def start_json(
        self,
        selected_role_capabilities: Iterable[Any] | None = None,
        *,
        role_capabilities: Iterable[Any] | None = None,
        readiness: Mapping[str, Any] | None = None,
        alias: str = "setup",
    ) -> dict[str, Any]:
        return self._response(
            "start",
            self.start(
                selected_role_capabilities,
                role_capabilities=role_capabilities,
                readiness=readiness,
            ),
            alias=_alias_metadata(alias),
        )

    def status_json(self, transaction_id: str | None = None, *, alias: str = "setup") -> dict[str, Any]:
        return self._response("status", self.status(transaction_id), alias=_alias_metadata(alias))

    def resume_json(self, transaction_id: str | None = None, *, alias: str = "setup", **kwargs: Any) -> dict[str, Any]:
        return self._response("resume", self.resume(transaction_id, **kwargs), alias=_alias_metadata(alias))

    def reset_json(self, transaction_id: str | None = None, *, alias: str = "setup") -> dict[str, Any]:
        return self._response("reset", self.reset(transaction_id), alias=_alias_metadata(alias))

    def handle_json(self, request: Mapping[str, Any] | str) -> dict[str, Any]:
        """Dispatch a JSON-compatible operation for UI/agent clients."""
        try:
            payload = json.loads(request) if isinstance(request, str) else dict(request)
            operation = str(payload.get("operation", payload.get("action", "status"))).lower()
            alias = str(payload.get("alias", "setup"))
            if operation == "start":
                return self.start_json(payload.get("selected_role_capabilities"), alias=alias)
            if operation == "status":
                return self.status_json(payload.get("transaction_id"), alias=alias)
            if operation in {"resume", "finish_later"}:
                kwargs = dict(payload)
                kwargs.pop("operation", None)
                kwargs.pop("action", None)
                kwargs.pop("alias", None)
                kwargs.pop("transaction_id", None)
                if operation == "finish_later":
                    kwargs["finish_later"] = True
                return self.resume_json(payload.get("transaction_id"), alias=alias, **kwargs)
            if operation == "reset":
                return self.reset_json(payload.get("transaction_id"), alias=alias)
            return {"ok": False, "operation": operation, "error": _safe_error("SETUP_ERROR")}
        except (TypeError, ValueError, SetupTransactionError, SetupTransactionNotFound):
            return {"ok": False, "operation": "unknown", "error": _safe_error("SETUP_ERROR")}

    def handle_json_text(self, request: Mapping[str, Any] | str) -> str:
        return json.dumps(self.handle_json(request), sort_keys=True)


def _alias_metadata(command: str) -> dict[str, Any]:
    canonical = "setup"
    return {
        "command": command,
        "alias_of": canonical,
        "deprecated": command != canonical,
        "semantics": "All setup aliases address the same resumable transaction.",
    }


__all__ = [
    "ALLOWED_ROLES",
    "DEFAULT_ROLE_CAPABILITIES",
    "ReadinessAdapter",
    "SCHEMA_VERSION",
    "SETUP_TRANSACTION_FILENAME",
    "SETUP_TRANSACTION_SCHEMA_VERSION",
    "SetupTransactionCorrupt",
    "SetupTransactionCorruptError",
    "SetupTransactionError",
    "SetupTransactionNotFound",
    "SetupTransactionNotFoundError",
    "SetupTransactionService",
    "SetupTransactionStore",
    "TRANSACTION_FILENAME",
    "TRANSACTION_SCHEMA",
    "normalize_role_capabilities",
]
