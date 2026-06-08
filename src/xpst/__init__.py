"""XPST - Enterprise-grade, open-source cross-posting for short-form video.

Automatically distribute short-form video content from TikTok to YouTube Shorts,
X/Twitter, and Instagram Reels with enterprise reliability features.

Key Features:
    - Automatic cross-posting from TikTok to 3 platforms
    - Circuit breaker pattern for platform failure isolation
    - Retry with exponential backoff and error categorization
    - Graceful shutdown with state persistence
    - Crash recovery with checkpoint-based resumability
    - Webhook notifications (Discord/Telegram)
    - Web analytics dashboard
    - Secure credential storage (OS keychain)

Quick Start:
    >>> from xpst import XPSTConfig, CrossPostEngine
    >>> config = XPSTConfig.load()
    >>> engine = CrossPostEngine(config)
    >>> results = asyncio.run(engine.check_and_post())
"""

__version__ = "0.1.0"
__author__ = "XPST Contributors"

from .config import NotificationConfig, XPSTConfig
from .engine import CrossPostEngine
from .state import StateManager

__all__ = [
    "XPSTConfig",
    "CrossPostEngine",
    "StateManager",
    "NotificationConfig",
]
