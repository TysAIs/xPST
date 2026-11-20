"""
Structured logging for xPST

Provides consistent, structured logging with support for:
- Human-readable console output (with colors via rich)
- JSON file output for machine parsing
- Log rotation (size-based)
- Per-platform log contexts

Example usage:
    from xpst.utils.logger import get_logger

    logger = get_logger("xpst.platforms.youtube")
    logger.info("Uploading video", video_id="123", platform="youtube")
"""

import logging
import logging.handlers
import sys
from pathlib import Path

from xpst.utils.redaction import RedactingFormatter, install_handler_redaction

try:
    import structlog  # noqa: F401
    HAS_STRUCTLOG = True
except ImportError:
    HAS_STRUCTLOG = False

try:
    from rich.console import Console  # noqa: F401
    from rich.logging import RichHandler
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


def setup_logging(
    log_level: str = "INFO",
    log_file: str | None = None,
    log_rotation: str = "10 MB",
    enable_json: bool = False,
) -> None:
    """
    Set up logging configuration.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Path to log file (None to disable file logging)
        log_rotation: Log rotation size (e.g., "10 MB", "100 MB")
        enable_json: Enable JSON-formatted file output
    """
    # Parse rotation size
    max_bytes = _parse_size(log_rotation)

    # Configure root logger
    root_logger = logging.getLogger("xpst")
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Remove existing handlers
    root_logger.handlers.clear()

    # Console handler (with rich if available) — always use stderr so stdout stays clean for --json
    if HAS_RICH:
        stderr_console = Console(stderr=True)
        console_handler = RichHandler(
            console=stderr_console,
            rich_tracebacks=True,
            show_time=True,
            show_path=False,
        )
        console_handler.setLevel(logging.DEBUG)
    else:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    root_logger.addHandler(console_handler)

    # File handler (with rotation)
    if log_file:
        log_path = Path(log_file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)

        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)

        if enable_json and HAS_STRUCTLOG:
            # Use structlog for JSON output
            file_handler.setFormatter(
                RedactingFormatter("%(message)s")
            )
        else:
            file_handler.setFormatter(
                RedactingFormatter(
                    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )

        root_logger.addHandler(file_handler)

    # Handler-level redaction is what covers records that PROPAGATE from child
    # loggers (e.g. xpst.platforms.instagram) — logger-level filters do not run
    # for propagated records. Together with the filter installed by get_logger
    # this closes both directions.
    for _handler in root_logger.handlers:
        install_handler_redaction(_handler)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance, with secret redaction attached.

    Every logger handed out here carries a ``RedactionFilter`` so that a token,
    cookie, session id or Authorization header can never reach a log file —
    including via ``httpx``/``urllib`` exception strings that embed the full
    request URL (which is how the Meta Graph API access token used to leak on a
    failed upload). See :mod:`xpst.utils.redaction`.

    Args:
        name: Logger name (usually module path)

    Returns:
        Logger instance
    """
    from xpst.utils.redaction import install_redaction_filter

    logger = logging.getLogger(name)
    install_redaction_filter(logger)
    return logger


def _parse_size(size_str: str) -> int:
    """Parse a human-readable size string to bytes.

    Supports suffixes: B, KB/K, MB/M, GB/G. If no suffix, defaults to MB.
    Whitespace within the number is stripped (e.g., "10 MB" → 10485760).

    Args:
        size_str: Size string (e.g., ``"10 MB"``, ``"1 GB"``).

    Returns:
        Size in bytes. Defaults to 10 MB on parse failure.
    """

    size_str = size_str.strip().upper()

    multipliers = {
        "GB": 1024 * 1024 * 1024,
        "G": 1024 * 1024 * 1024,
        "MB": 1024 * 1024,
        "M": 1024 * 1024,
        "KB": 1024,
        "K": 1024,
        "B": 1,
    }

    for suffix, multiplier in multipliers.items():
        if size_str.endswith(suffix):
            number = size_str[: -len(suffix)].strip().replace(" ", "")
            return int(float(number) * multiplier)

    # Default to MB if no suffix
    try:
        return int(float(size_str) * 1024 * 1024)
    except ValueError:
        return 10 * 1024 * 1024  # Default 10 MB
