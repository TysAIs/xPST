"""
Video sources for XPST

Source modules handle downloading content from various platforms.
Each source inherits from VideoSource and registers itself with SourceRegistry.

Available sources:
- TikTokSource: TikTok videos and slideshows
- InstagramSource: Instagram Reels, carousels, and posts
- YouTubeSource: YouTube videos
- XSource: X/Twitter video tweets
- LocalSource: Local files and directories
"""

# Import all source modules to trigger auto-registration.
# Each module calls SourceRegistry.register() at module level,
# making it discoverable by the engine without explicit configuration.
# Sources with external dependencies (yt-dlp, instagrapi, twikit) are
# wrapped in try/except so missing deps don't crash the whole package.

from . import local  # noqa: F401
from .base import (
    ContentType,
    DownloadResult,
    SourceRegistry,
    VideoMetadata,
    VideoSource,
)

# Conditional imports for sources with external dependencies
try:
    from . import tiktok  # noqa: F401
except ImportError:
    pass

try:
    from . import youtube  # noqa: F401
except ImportError:
    pass

try:
    from . import x  # noqa: F401
except ImportError:
    pass

try:
    from . import instagram  # noqa: F401
except ImportError:
    pass

__all__ = [
    "VideoSource",
    "SourceRegistry",
    "VideoMetadata",
    "DownloadResult",
    "ContentType",
]
