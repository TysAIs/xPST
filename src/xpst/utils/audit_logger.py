"""
Audit logger for MCP tool invocations.

Writes structured JSON lines to ~/.xpst/logs/mcp_audit.jsonl with:
- timestamp (ISO 8601)
- tool_name
- input_args (sanitized)
- output_summary (truncated)
- duration_ms
- success/error
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_AUDIT_LOG_PATH: Path | None = None


def _get_audit_log_path() -> Path:
    """Get or create the audit log path."""
    global _AUDIT_LOG_PATH
    if _AUDIT_LOG_PATH is None:
        log_dir = Path.home() / ".xpst" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        _AUDIT_LOG_PATH = log_dir / "mcp_audit.jsonl"
    return _AUDIT_LOG_PATH


def _sanitize_args(args: dict[str, Any]) -> dict[str, Any]:
    """Remove sensitive values from args before logging."""
    sensitive_keys = {"token", "password", "secret", "api_key", "access_token", "client_secret"}
    sanitized = {}
    for key, value in args.items():
        if key.lower() in sensitive_keys:
            sanitized[key] = "[REDACTED]"
        elif isinstance(value, str) and len(value) > 200:
            sanitized[key] = value[:200] + "...[truncated]"
        else:
            sanitized[key] = value
    return sanitized


def log_tool_invocation(
    tool_name: str,
    input_args: dict[str, Any],
    output: Any,
    duration_ms: float,
    success: bool,
    error: str | None = None,
) -> None:
    """Log an MCP tool invocation to the audit log.

    Args:
        tool_name: Name of the MCP tool.
        input_args: Input arguments passed to the tool.
        output: Output returned by the tool.
        duration_ms: Duration in milliseconds.
        success: Whether the invocation succeeded.
        error: Error message if failed, None otherwise.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool_name": tool_name,
        "input_args": _sanitize_args(input_args),
        "output_summary": str(output)[:500] if output else None,
        "duration_ms": round(duration_ms, 2),
        "success": success,
        "error": error,
    }
    try:
        path = _get_audit_log_path()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.warning("Failed to write audit log: %s", e)
