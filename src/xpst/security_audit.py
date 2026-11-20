"""Shared security-audit core (punch-list #4, DEEP-REVIEW-2026-09-28).

The MCP tool ``xpst_security_audit`` and the CLI ``security_audit`` command
both used to hard-code ``passed: True`` for ``dashboard_localhost``,
``mcp_readonly`` and ``encrypted_storage`` — a report that always flattered the
installation. This module computes every check from live evidence and is the
single source of truth for both surfaces.

Honesty rules:
- A check reports what was OBSERVED. Where nothing can be observed (e.g. no
  dashboard has ever bound in this process), the check reports
  ``status="unobserved"`` instead of inventing a pass.
- ``advisory`` checks report their real state but never flip the overall
  verdict — they describe hardening posture (readonly mode, provider mode),
  not a broken installation.
"""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from xpst.config import XPSTConfig

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def truthy_env(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes"}


def credential_storage_backend(store: Any) -> str:
    """Describe the storage a CredentialStore will actually use.

    Returns "keyring", "encrypted-file", or "plaintext-unavailable". The
    store refuses to write plaintext, so the third state means every
    credential write would raise — a real failure, not a pass.
    """
    if getattr(store, "_use_keyring", False):
        return "keyring"
    if getattr(store, "_fernet", None) is not None:
        return "encrypted-file"
    return "plaintext-unavailable"


def run_security_checks(
    config: XPSTConfig,
    *,
    bind_host_reader: Callable[[], str | None] | None = None,
    credential_store: Any = None,
) -> dict[str, Any]:
    """Compute the audit payload used by MCP and the CLI.

    Args:
        config: the loaded configuration (credential paths, provider mode).
        bind_host_reader: returns the host the dashboard last bound in this
            process, or None when no dashboard has started here. Defaults to
            the dashboard module's recorder.
        credential_store: optional pre-built CredentialStore (tests); built
            from the config dir otherwise.
    """
    checks: list[dict[str, Any]] = []
    all_pass = True

    # 1. Credential file permissions (0600) — unchanged, it was already real.
    cred_paths = [
        config.youtube.client_secrets,
        config.youtube.token_file,
        config.x.cookies_file,
        config.instagram.session_file,
    ]
    for cred_path in cred_paths:
        if cred_path and os.path.exists(cred_path):
            mode = stat.S_IMODE(os.stat(cred_path).st_mode)
            ok = mode == 0o600
            checks.append({
                "check": "file_permissions",
                "path": cred_path,
                "mode": oct(mode),
                "status": "observed",
                "passed": ok,
            })
            if not ok:
                all_pass = False

    # 2. Dashboard bind host — observed from the live bind in this process.
    reader = bind_host_reader
    if reader is None:
        try:
            from xpst.dashboard.server import last_bound_host

            reader = last_bound_host
        except Exception:  # noqa: BLE001 - dashboard module optional at audit time
            def _none_host() -> str | None:
                return None

            reader = _none_host

    bind_host = reader()
    if bind_host is None:
        checks.append({
            "check": "dashboard_localhost",
            "status": "unobserved",
            "passed": True,
            "advisory": True,
            "detail": "No dashboard has bound a socket in this process; "
                      "nothing observed either way.",
        })
    else:
        loopback = bind_host in _LOOPBACK_HOSTS
        checks.append({
            "check": "dashboard_localhost",
            "status": "observed",
            "passed": loopback,
            "detail": f"Dashboard bound to {bind_host}"
                      + ("" if loopback else " — non-loopback: read-only endpoints are on the network."),
        })
        if not loopback:
            all_pass = False

    # 3. MCP readonly mode — the real env state, advisory.
    readonly = truthy_env("XPST_MCP_READONLY")
    checks.append({
        "check": "mcp_readonly",
        "status": "observed",
        "passed": readonly,
        "advisory": True,
        "detail": "XPST_MCP_READONLY is set — mutating MCP tools are blocked."
                  if readonly else
                  "XPST_MCP_READONLY not set — mutating MCP tools rely on the "
                  "XPST_MCP_ALLOW_MUTATIONS/REQUIRE_CONFIRM gates.",
    })

    # 4. Provider mode — informational, both modes are valid postures.
    checks.append({
        "check": "provider_mode",
        "status": "observed",
        "passed": True,
        "advisory": True,
        "detail": f"Provider mode: {config.provider_mode}",
    })

    # 5. FFmpeg — unchanged, it was already real.
    try:
        from xpst.utils.platform import resolve_ffmpeg_path

        ffmpeg_ok = resolve_ffmpeg_path() is not None
    except Exception:  # noqa: BLE001
        ffmpeg_ok = False
    checks.append({
        "check": "ffmpeg_available",
        "status": "observed",
        "passed": ffmpeg_ok,
        "detail": "FFmpeg found" if ffmpeg_ok else "FFmpeg not found — video processing will fail",
    })
    if not ffmpeg_ok:
        all_pass = False

    # 6. Encrypted credential storage — ask the actual store which backend it
    #    resolved, instead of asserting the design on a report.
    if credential_store is None:
        from xpst.utils.credentials import CredentialStore

        try:
            credential_store = CredentialStore(config.config_dir)
        except Exception as exc:  # noqa: BLE001 - a store that cannot build cannot encrypt
            checks.append({
                "check": "encrypted_storage",
                "status": "observed",
                "passed": False,
                "detail": f"Credential store unavailable: {type(exc).__name__}",
            })
            all_pass = False
            credential_store = None

    if credential_store is not None:
        backend = credential_storage_backend(credential_store)
        encrypted = backend in ("keyring", "encrypted-file")
        checks.append({
            "check": "encrypted_storage",
            "status": "observed",
            "passed": encrypted,
            "detail": {
                "keyring": "OS keychain (opt-in) is the active backend.",
                "encrypted-file": "Fernet-encrypted file store (scrypt-derived key) is the active backend.",
                "plaintext-unavailable": "Neither keychain nor cryptography is usable — "
                                         "credential writes would be refused, not silently plaintext.",
            }[backend],
        })
        if not encrypted:
            all_pass = False

    return {
        "overall_status": "pass" if all_pass else "fail",
        "checks": checks,
        "passed_count": sum(1 for c in checks if c["passed"]),
        "failed_count": sum(1 for c in checks if not c["passed"]),
    }
