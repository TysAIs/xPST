"""
Local file source for xPST

Handles local video files and directories with support for:
- Single video files
- Directory scanning for multiple videos
- Image sequences (for carousel content)
- Multiple video file formats
"""

from pathlib import Path
from typing import Any

from xpst.config import XPSTConfig
from xpst.sources.base import (
    ContentType,
    DownloadResult,
    SourceRegistry,
    VideoMetadata,
    VideoSource,
)
from xpst.utils.logger import get_logger

logger = get_logger(__name__)

# Supported file extensions
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
ALL_MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS


class LocalSource(VideoSource):
    """
    Local file source for xPST.

    Features:
    - Single video file support
    - Directory scanning for multiple files
    - Image sequence support (for carousels)
    - Automatic content type detection
    """

    def __init__(self, config: XPSTConfig) -> None:
        """Initialize local source with no base path set."""
        super().__init__(config)
        self._base_path: Path | None = None

    @property
    def source_name(self) -> str:
        """Return the source platform identifier."""
        return "local"

    def set_path(self, path: str | Path) -> None:
        """
        Set the base path for local files.

        Args:
            path: File path or directory path
        """
        self._base_path = Path(path).expanduser().resolve()

    def _get_base_path(self) -> Path:
        """
        Get the configured base path.

        Returns:
            Base path

        Raises:
            ValueError: If no path configured
        """
        if self._base_path:
            return self._base_path

        # Try to get from config
        base_path = getattr(self.config, 'local_path', None)
        if base_path:
            return Path(base_path).expanduser().resolve()

        raise ValueError(
            "No local path configured. Call set_path() or add 'local_path' to config."
        )

    def _detect_content_type(self, files: list[Path]) -> str:
        """
        Detect the content type based on file types.

        Args:
            files: List of file paths

        Returns:
            ContentType string
        """
        if len(files) == 1:
            ext = files[0].suffix.lower()
            if ext in VIDEO_EXTENSIONS:
                return ContentType.VIDEO
            elif ext in IMAGE_EXTENSIONS:
                return ContentType.IMAGE
            return ContentType.VIDEO

        # Multiple files - carousel
        has_video = any(f.suffix.lower() in VIDEO_EXTENSIONS for f in files)
        has_image = any(f.suffix.lower() in IMAGE_EXTENSIONS for f in files)

        if has_video and has_image:
            return ContentType.CAROUSEL_MIXED
        elif has_video:
            return ContentType.CAROUSEL_VIDEO
        else:
            return ContentType.CAROUSEL_IMAGE

    def _get_video_metadata(self, file_path: Path, index: int = 0) -> VideoMetadata:
        """
        Create VideoMetadata for a local file.

        Args:
            file_path: Path to the file
            index: Index for generating unique ID

        Returns:
            VideoMetadata instance
        """
        # Generate a simple ID from filename
        video_id = file_path.stem

        # Detect content type based on extension
        ext = file_path.suffix.lower()
        if ext in VIDEO_EXTENSIONS:
            content_type = ContentType.VIDEO
        elif ext in IMAGE_EXTENSIONS:
            content_type = ContentType.IMAGE
        else:
            content_type = ContentType.VIDEO

        return VideoMetadata(
            video_id=video_id,
            url=str(file_path),
            caption=file_path.stem,
            description=f"Local file: {file_path.name}",
            duration=0,  # Could use ffprobe to get duration
            timestamp=None,
            author="local",
            content_type=content_type,
            source_platform="local",
            media_paths=[file_path],
            extra={
                "file_path": str(file_path),
                "file_size": file_path.stat().st_size,
                "file_extension": ext,
            },
        )

    def _get_carousel_metadata(self, files: list[Path], group_name: str) -> VideoMetadata:
        """
        Create VideoMetadata for a carousel (multiple files).

        Args:
            files: List of file paths
            group_name: Name for the carousel group

        Returns:
            VideoMetadata instance
        """
        content_type = self._detect_content_type(files)

        return VideoMetadata(
            video_id=group_name,
            url=str(files[0].parent),
            caption=group_name,
            description=f"Local carousel: {len(files)} items",
            duration=0,
            timestamp=None,
            author="local",
            content_type=content_type,
            source_platform="local",
            media_paths=sorted(files),
            extra={
                "file_count": len(files),
                "directory": str(files[0].parent),
            },
        )

    async def list_videos(self, max_count: int = 10) -> list[VideoMetadata]:
        """
        List local media files.

        If the path is a single file, returns metadata for that file.
        If the path is a directory, scans for media files.

        Args:
            max_count: Maximum number of items to return

        Returns:
            List of video metadata
        """
        base_path = self._get_base_path()

        if base_path.is_file():
            # Single file
            if base_path.suffix.lower() in ALL_MEDIA_EXTENSIONS:
                return [self._get_video_metadata(base_path)]
            else:
                logger.warning(f"Unsupported file type: {base_path.suffix}")
                return []

        if not base_path.is_dir():
            raise ValueError(f"Path does not exist: {base_path}")

        # Scan directory
        all_files = []
        for ext in ALL_MEDIA_EXTENSIONS:
            all_files.extend(base_path.glob(f"*{ext}"))
            all_files.extend(base_path.glob(f"*{ext.upper()}"))

        # Remove duplicates and sort
        all_files = sorted(set(all_files))

        if not all_files:
            logger.warning(f"No media files found in {base_path}")
            return []

        # Group files by stem pattern for carousels
        # Files like "post_001.jpg", "post_002.jpg" are grouped as a carousel
        grouped = self._group_files(all_files)

        videos = []
        for group_name, group_files in grouped[:max_count]:
            if len(group_files) == 1:
                videos.append(self._get_video_metadata(group_files[0]))
            else:
                videos.append(self._get_carousel_metadata(group_files, group_name))

        logger.info(f"Found {len(videos)} media items in {base_path}")
        return videos

    def _group_files(self, files: list[Path]) -> list[tuple[str, list[Path]]]:
        """Group files by naming pattern for carousel detection.

        Files like ``post_001.jpg``, ``post_002.jpg`` are grouped under
        ``post``. Uses regex to strip trailing numeric suffixes. Single
        files get their own group.

        Args:
            files: List of file paths to group.

        Returns:
            List of (group_name, files) tuples sorted by first filename.
        """

        import re

        groups: dict[str, list[Path]] = {}

        for file_path in files:
            stem = file_path.stem

            # Try to detect grouping pattern
            # e.g., "post_001", "post_002" -> group "post"
            # e.g., "image1", "image2" -> group "image"
            match = re.match(r"^(.+?)[-_]?\d+$", stem)
            group_name = match.group(1) if match else stem

            if group_name not in groups:
                groups[group_name] = []
            groups[group_name].append(file_path)

        # Sort groups by first file's name
        sorted_groups = sorted(groups.items(), key=lambda x: x[1][0].name)
        return sorted_groups

    async def download(self, video_id: str, output_dir: Path) -> DownloadResult:
        """
        "Download" (copy/move) a local file.

        For local files, this copies the file to the output directory.

        Args:
            video_id: File stem name or path
            output_dir: Output directory

        Returns:
            DownloadResult
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        base_path = self._get_base_path()

        # Find the source file
        source_path = await self._find_source_file(video_id, base_path)
        if not source_path:
            return DownloadResult(
                success=False,
                error=f"File not found: {video_id}",
            )

        # Handle carousel (multiple files)
        if isinstance(source_path, list):
            media_paths = []
            for i, src in enumerate(source_path):
                dest = output_dir / f"{video_id}_{i:03d}{src.suffix}"
                if not dest.exists():
                    dest.write_bytes(src.read_bytes())
                media_paths.append(dest)

            return DownloadResult(
                success=True,
                video_path=media_paths[0] if media_paths else None,
                media_paths=media_paths,
                format_used="local_carousel",
            )

        # Single file
        ext = source_path.suffix
        dest = output_dir / f"{video_id}{ext}"

        if dest.exists() and dest.stat().st_size > 0:
            logger.info(f"Already exists: {dest.name}")
            return DownloadResult(
                success=True,
                video_path=dest,
                media_paths=[dest],
                format_used="cached",
            )

        # Copy file
        dest.write_bytes(source_path.read_bytes())

        logger.info(f"Copied: {source_path.name} -> {dest.name}")
        return DownloadResult(
            success=True,
            video_path=dest,
            media_paths=[dest],
            format_used="local",
        )

    async def _find_source_file(
        self, video_id: str, base_path: Path
    ) -> Path | list[Path] | None:
        """
        Find the source file(s) for a video ID.

        Args:
            video_id: File stem or path
            base_path: Base directory to search

        Returns:
            Path, list of Paths, or None
        """
        # Try direct path first
        direct_path = Path(video_id)
        if direct_path.exists():
            if direct_path.is_file():
                return direct_path
            elif direct_path.is_dir():
                # Scan directory for matching files
                files = []
                for ext in ALL_MEDIA_EXTENSIONS:
                    files.extend(direct_path.glob(f"*{ext}"))
                return sorted(files) if files else None

        # Try matching by stem in base directory
        if base_path.is_dir():
            # Look for files matching the stem
            matches = []
            for ext in ALL_MEDIA_EXTENSIONS:
                matches.extend(base_path.glob(f"{video_id}{ext}"))
                matches.extend(base_path.glob(f"{video_id}.*"))

            if len(matches) == 1:
                return matches[0]
            elif len(matches) > 1:
                return sorted(matches)

            # Look for grouped files (carousel pattern)
            grouped_files = []
            for ext in ALL_MEDIA_EXTENSIONS:
                grouped_files.extend(base_path.glob(f"{video_id}_*{ext}"))
                grouped_files.extend(base_path.glob(f"{video_id}-*{ext}"))

            if grouped_files:
                return sorted(grouped_files)

        return None

    async def check_health(self) -> dict[str, Any]:
        """
        Check local source health.

        Returns:
            Health status dictionary
        """
        path_configured = False
        path_exists = False
        file_count = 0

        try:
            base_path = self._get_base_path()
            path_configured = True
            path_exists = base_path.exists()

            if path_exists and base_path.is_dir():
                for ext in ALL_MEDIA_EXTENSIONS:
                    file_count += len(list(base_path.glob(f"*{ext}")))
        except ValueError:
            pass

        return {
            "source": "local",
            "path_configured": path_configured,
            "path_exists": path_exists,
            "file_count": file_count,
            "status": "ok" if path_configured and path_exists else "error",
        }


# Auto-register this source
SourceRegistry.register("local", LocalSource)
