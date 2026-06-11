"""
Video processing utilities for xPST

Handles FFmpeg-based video encoding with platform-specific profiles.
Each platform has research-verified optimal settings for maximum quality
after re-encoding by the platform.

Research sources:
- Instagram: dev.to/alfg/ffmpeg-for-instagram (reverse-engineered specs)
- X/Twitter: gehrcke.de + gist.github.com/transkatgirl (official requirements)
- YouTube: Upscale to 1080p + re-encode at 8 Mbps to avoid low-bitrate tier

Key findings (updated 2026-06-11, fidelity-first):
- Scaling is ORIENTATION-AWARE, targeting the LONG edge (1920). The old
  height-keyed `scale=-2:{res}` crushed 1080x1920 portrait video to 608x1080
  (Instagram: 406x720) — the direct cause of degraded upload quality.
- Instagram Reels: 1080x1920 @ CRF 20, High@L4.0, fixed GOP 72, bt.709, yuv420p
- X/Twitter: up to 1920 long edge @ 10 Mbps, High@L4.0, yuv420p (REQUIRED), keyint=90
- YouTube: long edge 1920 (upscaled if smaller, to avoid the low-bitrate tier),
  8 Mbps, High Profile, closed GOP 15, bt.709, yuv420p
- Frame rate is a CAP (`-fpsmax`), never a force: 60fps sources stay 60fps;
  the old forced `-r 30` halved them.
"""

import re
import subprocess
import sys
from pathlib import Path

from xpst.config import EncodingConfig
from xpst.utils.logger import get_logger
from xpst.utils.platform import get_ffmpeg_name, get_ffprobe_name

logger = get_logger(__name__)


def build_scale_filter(long_edge: int, *, upscale: bool = False, flags: str = "") -> str:
    """Build an orientation-aware ffmpeg scale filter targeting the long edge.

    Sizes the LONG edge of the video to ``long_edge`` while preserving aspect
    ratio, so portrait (9:16), landscape (16:9), and square sources all keep
    their native orientation. With ``upscale=False`` a smaller source passes
    through at native size — quality is never invented (fidelity invariant).

    The previous height-keyed filter (``scale=-2:{res}``) treated the target
    as a height, crushing 1080x1920 portrait video to 608x1080.
    """
    if upscale:
        w_target = str(long_edge)
        h_target = str(long_edge)
    else:
        w_target = f"trunc(min({long_edge},iw)/2)*2"
        h_target = f"trunc(min({long_edge},ih)/2)*2"
    flag_part = f":flags={flags}" if flags else ""
    return f"scale=w='if(gt(a,1),{w_target},-2)':h='if(gt(a,1),-2,{h_target})'{flag_part}"


def double_rate(rate: str) -> str:
    """Double an ffmpeg rate string ('3500k' → '7000k', '3.5M' → '7M').

    Used to derive bufsize from maxrate. Handles fractional values that the
    previous ``int()`` parse rejected.
    """
    m = re.fullmatch(r"([0-9.]+)\s*([kKmM]?)", rate.strip())
    if not m:
        return rate
    doubled = float(m.group(1)) * 2
    value: int | float = int(doubled) if doubled.is_integer() else doubled
    return f"{value}{m.group(2)}"


def _parse_frame_rate(raw: str) -> float | None:
    """Parse an ffprobe frame-rate fraction ('30000/1001', '60/1') to float."""
    try:
        if "/" in raw:
            num, den = raw.split("/", 1)
            denominator = float(den)
            return float(num) / denominator if denominator else None
        return float(raw)
    except ValueError:
        return None


def _rate_to_bps(rate: str) -> int | None:
    """Convert an ffmpeg rate string ('10M', '3500k') to bits per second."""
    m = re.fullmatch(r"([0-9.]+)\s*([kKmM]?)", rate.strip())
    if not m:
        return None
    multiplier = {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1)
    return int(float(m.group(1)) * multiplier)


def ffmpeg_install_hint() -> str:
    """Return a platform-specific, actionable FFmpeg install instruction."""
    if sys.platform == "darwin":
        return "Install it with: brew install ffmpeg (macOS)"
    if sys.platform == "win32":
        return "Download it from https://ffmpeg.org/download.html and add it to PATH (Windows)"
    return "Install it with your package manager (Linux), e.g. apt install ffmpeg, dnf install ffmpeg, or pacman -S ffmpeg"


class FFmpegNotFoundError(RuntimeError):
    """Raised when the FFmpeg binary cannot be found or run.

    Subclasses RuntimeError so existing callers that catch RuntimeError keep
    working, while carrying an actionable, platform-specific install hint so
    the desktop app can surface a friendly first-run message instead of a raw
    traceback (W3-3).
    """

    def __init__(self, ffmpeg_path: str, hint: str | None = None) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.hint = hint or ffmpeg_install_hint()
        super().__init__(
            f"FFmpeg not found at '{ffmpeg_path}'. "
            "FFmpeg is required for video encoding. "
            f"{self.hint} or see https://ffmpeg.org/download.html"
        )


class VideoProcessor:
    """
    Video processing with FFmpeg.

    Handles platform-specific encoding for optimal quality preservation.
    """

    def __init__(self, ffmpeg_path: str | None = None):
        """
        Initialize video processor.

        Args:
            ffmpeg_path: Path to ffmpeg binary. Defaults to platform-specific name.
        """
        self.ffmpeg_path = ffmpeg_path or get_ffmpeg_name()
        self._verify_ffmpeg()

    def _verify_ffmpeg(self) -> None:
        """Verify FFmpeg is installed and accessible on PATH.

        Raises:
            FFmpegNotFoundError: If FFmpeg binary is not found or returns
                error. Subclasses RuntimeError, so existing RuntimeError
                handlers keep working while gaining an actionable install hint.
        """

        try:
            result = subprocess.run(
                [self.ffmpeg_path, "-version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                raise FFmpegNotFoundError(self.ffmpeg_path)
        except (FileNotFoundError, PermissionError, OSError):
            raise FFmpegNotFoundError(self.ffmpeg_path) from None

    def get_video_info(self, video_path: Path) -> dict:
        """
        Get video file information.

        Args:
            video_path: Path to video file

        Returns:
            Dictionary with video info (width, height, duration, etc.)
        """
        cmd = [
            get_ffprobe_name(),
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(video_path),
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            raise RuntimeError(f"ffprobe failed: {result.stderr}")

        import json
        return json.loads(result.stdout)

    def is_platform_compliant(
        self,
        video_path: Path,
        platform: str,
        config: EncodingConfig,
    ) -> tuple[bool, str]:
        """Probe whether a source already satisfies a platform's profile.

        Returns ``(True, summary)`` when uploading the source as-is loses
        nothing, so the pipeline can skip the re-encode entirely. Fidelity
        invariant: never spend a quality generation on compliant media.
        """
        info = self.get_video_info(video_path)
        streams = info.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if video is None:
            return False, "no video stream"

        if video.get("codec_name") != "h264":
            return False, f"codec {video.get('codec_name')} != h264"
        pix_fmt = config.pix_fmt or "yuv420p"
        if video.get("pix_fmt") != pix_fmt:
            return False, f"pix_fmt {video.get('pix_fmt')} != {pix_fmt}"

        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        long_edge_target = config.resolution or 1920
        if max(width, height) > long_edge_target:
            return False, f"long edge {max(width, height)} > {long_edge_target}"

        fps_cap = config.fps or 60
        fps = _parse_frame_rate(video.get("avg_frame_rate") or video.get("r_frame_rate") or "")
        if fps and fps > fps_cap + 0.1:
            return False, f"fps {fps:.1f} > cap {fps_cap}"

        if config.maxrate:
            max_bps = _rate_to_bps(config.maxrate)
            bit_rate = int(video.get("bit_rate") or info.get("format", {}).get("bit_rate") or 0)
            if max_bps and bit_rate > max_bps * 1.25:
                return False, f"bitrate {bit_rate} exceeds {config.maxrate} (+25% tolerance)"

        if audio is not None and audio.get("codec_name") != "aac":
            return False, f"audio {audio.get('codec_name')} != aac"

        return True, f"h264/{pix_fmt} {width}x{height} within {platform} profile"

    def encode_for_platform(
        self,
        input_path: Path,
        output_path: Path,
        platform: str,
        config: EncodingConfig,
    ) -> Path:
        """
        Encode video for a specific platform.

        Args:
            input_path: Source video path
            output_path: Output video path
            platform: Platform name (youtube, instagram, x)
            config: Encoding configuration

        Returns:
            Path to encoded video

        Raises:
            RuntimeError: If encoding fails
        """
        logger.info(f"Encoding for {platform}: {input_path.name}")

        # Build FFmpeg command based on platform
        if platform == "youtube":
            cmd = self._build_youtube_cmd(input_path, output_path, config)
        elif platform == "instagram":
            cmd = self._build_instagram_cmd(input_path, output_path, config)
        elif platform == "x":
            cmd = self._build_x_cmd(input_path, output_path, config)
        else:
            raise ValueError(f"Unknown platform: {platform}")

        # Run FFmpeg
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
        )

        if result.returncode != 0:
            logger.error(f"FFmpeg failed for {platform}: {result.stderr[:500]}")
            # Clean up failed output
            if output_path.exists():
                output_path.unlink()
            raise RuntimeError(f"FFmpeg encoding failed: {result.stderr[:200]}")

        if not output_path.exists() or output_path.stat().st_size < 1000:
            raise RuntimeError(f"Encoded video is empty or too small: {output_path}")

        size_mb = output_path.stat().st_size / 1024 / 1024
        logger.info(f"Encoded for {platform}: {output_path.name} ({size_mb:.1f} MB)")

        return output_path

    def _build_instagram_cmd(
        self,
        input_path: Path,
        output_path: Path,
        config: EncodingConfig,
    ) -> list[str]:
        """
        Build FFmpeg command for Instagram encoding.

        Instagram Reels optimal settings (fidelity-first, updated 2026-06-11):
        - Long edge 1920 (1080x1920 portrait preserved; never upscaled)
        - CRF 20 quality floor (~8-10 Mbps for Reels-grade detail)
        - H.264 High Profile @ Level 4.0
        - Fixed GOP 72
        - Frame rate capped at 60 (source rate preserved below the cap)
        - yuv420p pixel format
        - bt.709 color space
        - AAC 256k audio
        """
        resolution = config.resolution or 1920
        crf = config.crf or 20
        maxrate = config.maxrate or "10M"
        profile = config.profile or "high"
        level = config.level or "4.0"
        gop = config.gop or 72
        fps_cap = config.fps or 60
        color = config.color or "bt709"
        pix_fmt = config.pix_fmt or "yuv420p"

        return [
            self.ffmpeg_path,
            "-y",  # Overwrite output
            "-i", str(input_path),
            "-vf", f"{build_scale_filter(resolution)},setsar=1,format={pix_fmt}",
            "-c:v", "libx264",
            "-preset", "slow",  # Better quality
            "-profile:v", profile,
            "-level:v", level,
            "-x264-params", f"scenecut=0:open_gop=0:min-keyint={gop}:keyint={gop}:ref=4",
            "-crf", str(crf),
            "-maxrate", maxrate,
            "-bufsize", double_rate(maxrate),
            "-color_primaries", color,
            "-color_trc", color,
            "-colorspace", color,
            "-fpsmax", str(fps_cap),
            "-c:a", "aac",
            "-b:a", "256k",
            "-ar", "44100",
            "-movflags", "+faststart",
            "-sn",  # No subtitles
            str(output_path),
        ]

    def _build_x_cmd(
        self,
        input_path: Path,
        output_path: Path,
        config: EncodingConfig,
    ) -> list[str]:
        """
        Build FFmpeg command for X/Twitter encoding.

        X/Twitter optimal settings (fidelity-first, updated 2026-06-11):
        - Long edge 1920 (portrait and landscape preserved; never upscaled)
        - 10 Mbps (up to 25 Mbps supported)
        - H.264 High Profile @ Level 4.0
        - Fixed GOP 90
        - Frame rate capped at 60 (source rate preserved below the cap)
        - yuv420p pixel format (REQUIRED - X rejects other formats!)
        - bt.709 color space
        - AAC 256k audio
        - Lanczos scaling (high quality)
        """
        resolution = config.resolution or 1920
        bitrate = config.bitrate or "10M"
        maxrate = config.maxrate or "12M"
        profile = config.profile or "high"
        level = config.level or "4.0"
        gop = config.gop or 90
        fps_cap = config.fps or 60
        color = config.color or "bt709"
        pix_fmt = config.pix_fmt or "yuv420p"

        return [
            self.ffmpeg_path,
            "-y",  # Overwrite output
            "-i", str(input_path),
            "-vf", f"{build_scale_filter(resolution, flags='lanczos')},setsar=1,format={pix_fmt}",
            "-c:v", "libx264",
            "-preset", "slow",  # Better quality
            "-profile:v", profile,
            "-level:v", level,
            "-x264-params", f"scenecut=0:open_gop=0:min-keyint={gop}:keyint={gop}:ref=4",
            "-b:v", bitrate,
            "-maxrate", maxrate,
            "-bufsize", double_rate(maxrate),
            "-color_primaries", color,
            "-color_trc", color,
            "-colorspace", color,
            "-fpsmax", str(fps_cap),
            "-c:a", "aac",
            "-b:a", "256k",
            "-ar", "44100",
            "-movflags", "+faststart",
            "-sn",  # No subtitles
            str(output_path),
        ]

    def _build_youtube_cmd(
        self,
        input_path: Path,
        output_path: Path,
        config: EncodingConfig,
    ) -> list[str]:
        """
        Build FFmpeg command for YouTube encoding.

        YouTube optimal settings (fidelity-first, updated 2026-06-11):
        - Long edge scaled to 1920 (Lanczos; upscaled if smaller, to avoid
          YouTube's low-resolution bitrate tier — orientation preserved)
        - 8 Mbps (maxrate 10M, bufsize 12M)
        - H.264 High Profile
        - Closed GOP 15 frames
        - Frame rate capped at 60 (source rate preserved below the cap)
        - yuv420p pixel format
        - bt.709 color space
        - AAC 48kHz 256k audio
        - movflags +faststart for progressive playback
        """
        resolution = config.resolution or 1920
        bitrate = config.bitrate or "8M"
        maxrate = config.maxrate or "10M"
        bufsize = config.bufsize or "12M"
        profile = config.profile or "high"
        gop = config.gop or 15
        fps_cap = config.fps or 60
        color = config.color or "bt709"
        pix_fmt = config.pix_fmt or "yuv420p"

        return [
            self.ffmpeg_path,
            "-y",
            "-i", str(input_path),
            "-vf", f"{build_scale_filter(resolution, upscale=True, flags='lanczos')},setsar=1,format={pix_fmt}",
            "-c:v", "libx264",
            "-preset", "slow",
            "-profile:v", profile,
            "-x264-params", f"scenecut=0:open_gop=0:min-keyint={gop}:keyint={gop}:ref=4",
            "-b:v", bitrate,
            "-maxrate", maxrate,
            "-bufsize", bufsize,
            "-color_primaries", color,
            "-color_trc", color,
            "-colorspace", color,
            "-fpsmax", str(fps_cap),
            "-c:a", "aac",
            "-b:a", "256k",
            "-ar", "48000",
            "-movflags", "+faststart",
            "-sn",
            str(output_path),
        ]

    def generate_thumbnail(
        self,
        video_path: Path,
        output_path: Path,
        timestamp: str = "00:00:01",
    ) -> Path | None:
        """
        Generate a thumbnail from a video.

        Args:
            video_path: Source video
            output_path: Output thumbnail path
            timestamp: Timestamp to capture

        Returns:
            Path to thumbnail or None if failed
        """
        cmd = [
            self.ffmpeg_path,
            "-y",
            "-i", str(video_path),
            "-ss", timestamp,
            "-vframes", "1",
            "-q:v", "2",
            str(output_path),
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if result.returncode == 0 and output_path.exists():
            return output_path

        return None

    def stitch_carousel_to_video(
        self,
        media_paths: list[Path],
        output_path: Path,
        fps: int = 30,
        duration_per_image: float = 3.0,
        max_duration: float = 60.0,
    ) -> Path:
        """
        Combine images and videos into a single 1080x1920 vertical video.

        Args:
            media_paths: List of paths to images and/or videos
            output_path: Where to write the output video
            fps: Output frame rate
            duration_per_image: Seconds to show each image
            max_duration: Maximum output duration in seconds (YouTube Shorts limit)

        Returns:
            Path to the stitched video

        Raises:
            RuntimeError: If stitching fails
            ValueError: If no valid media files provided
        """
        if not media_paths:
            raise ValueError("No media files provided")

        for p in media_paths:
            if not p.exists():
                raise FileNotFoundError(f"Media file not found: {p}")

        # Separate images and videos
        image_exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
        images = [p for p in media_paths if p.suffix.lower() in image_exts]
        videos = [p for p in media_paths if p.suffix.lower() not in image_exts]

        if not images and not videos:
            raise ValueError("No valid media files found")

        # Build FFmpeg inputs and filter complex
        inputs = []
        filter_parts = []

        for _i, path in enumerate(media_paths):
            inputs.extend(["-i", str(path)])

        target_w, target_h = 1080, 1920
        crossfade_duration = 0.5 if len(media_paths) > 1 else 0.0

        scaled_labels = []
        durations = []

        for _i, path in enumerate(media_paths):
            is_image = path.suffix.lower() in image_exts
            label = f"[v{_i}]"
            if is_image:
                # Image: loop for duration_per_image seconds
                filter_parts.append(
                    f"[{_i}:v]loop=loop=-1:size={int(fps * duration_per_image)}:start=0,"
                    f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                    f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black,"
                    f"setsar=1,format=yuv420p,{label}"
                )
                durations.append(duration_per_image)
            else:
                # Video: scale to target
                try:
                    info = self.get_video_info(path)
                    vid_duration = float(info.get("format", {}).get("duration", str(duration_per_image)))
                except Exception as e:
                    logger.debug("Unexpected error: %s", e)
                    vid_duration = duration_per_image
                filter_parts.append(
                    f"[{_i}:v]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                    f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black,"
                    f"setsar=1,format=yuv420p,{label}"
                )
                durations.append(vid_duration)
            scaled_labels.append(label)

        # Calculate total duration
        total_duration = sum(durations) - crossfade_duration * max(0, len(media_paths) - 1)
        if total_duration > max_duration:
            total_duration = max_duration

        if len(scaled_labels) == 1:
            # Single input — just map directly
            video_map = "[v0]"
            filter_str = ";".join(filter_parts)
        else:
            # Chain xfade transitions
            prev_label = scaled_labels[0]
            offset = durations[0] - crossfade_duration

            for idx in range(1, len(scaled_labels)):
                next_label = scaled_labels[idx]
                out_label = f"[xf{idx}]" if idx < len(scaled_labels) - 1 else "[vout]"
                filter_parts.append(
                    f"{prev_label}{next_label}xfade=transition=fade:"
                    f"duration={crossfade_duration}:offset={offset:.3f}{out_label}"
                )
                prev_label = out_label
                if idx < len(scaled_labels) - 1:
                    offset += durations[idx] - crossfade_duration

            video_map = "[vout]"
            filter_str = ";".join(filter_parts)

        # Build FFmpeg command with silent audio track
        cmd = [
            self.ffmpeg_path,
            "-y",
            *inputs,
            "-filter_complex",
            f"{filter_str};anullsrc=r=44100:cl=stereo[a]",
            "-map", video_map,
            "-map", "[a]",
            "-t", str(total_duration),
            "-c:v", "libx264",
            "-preset", "slow",
            "-profile:v", "high",
            "-crf", "20",
            "-r", str(fps),
            "-c:a", "aac",
            "-b:a", "256k",
            "-ar", "44100",
            "-movflags", "+faststart",
            "-shortest",
            str(output_path),
        ]

        logger.info(
            f"Stitching {len(media_paths)} media files into carousel video ({total_duration:.1f}s)"
        )
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        if result.returncode != 0:
            logger.error(f"FFmpeg stitch failed: {result.stderr[:500]}")
            if output_path.exists():
                output_path.unlink()
            raise RuntimeError(f"Carousel stitching failed: {result.stderr[:200]}")

        if not output_path.exists() or output_path.stat().st_size < 1000:
            raise RuntimeError(f"Stitched video is empty or too small: {output_path}")

        size_mb = output_path.stat().st_size / 1024 / 1024
        logger.info(
            f"Carousel video created: {output_path.name} ({size_mb:.1f} MB, {total_duration:.1f}s)"
        )
        return output_path
