"""xPST v1.1.0 — Free, open-source, local-first cross-posting suite.

Distribute short-form video to YouTube, Instagram, X, TikTok, and Threads —
all for free via official APIs. Includes cross-post
correlation, follower tracking, best-time-to-post analytics, a knowledge
base that works with zero config, and an AI agent integration layer
(CLI + MCP).

Key Features:
    - Cross-posting to 5 video platforms (YouTube, IG, X, TikTok, Threads)
    - Official APIs by default, community mode for unofficial (opt-in)
    - Cross-post correlation — one video = one entry with total metrics
    - Follower tracking, best-time-to-post, engagement rate analytics
    - Knowledge base with zero-config deterministic fallback
    - sqlite-vec vector store (<1MB, replaces LanceDB)
    - 24+ MCP tools for AI agent integration
    - 36+ CLI commands with --json
    - PySide6/QML desktop app with accessibility (WCAG AA target)
    - Secure credential storage (Fernet + scrypt, OS keychain)
    - Circuit breaker, retry, crash recovery, graceful shutdown

Quick Start:
    >>> from xpst import XPSTConfig, CrossPostEngine
    >>> config = XPSTConfig.load()
    >>> engine = CrossPostEngine(config)
    >>> results = asyncio.run(engine.check_and_post())
"""

__version__ = "1.1.0"
__author__ = "xPST Contributors"

from .config import NotificationConfig, XPSTConfig
from .engine import CrossPostEngine
from .state import StateManager

__all__ = [
    "XPSTConfig",
    "CrossPostEngine",
    "StateManager",
    "NotificationConfig",
]
