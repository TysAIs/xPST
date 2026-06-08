"""
Video processing utilities for XPST

Handles FFmpeg-based video encoding with platform-specific profiles.
Each platform has research-verified optimal settings for maximum quality
after re-encoding by the platform.

Research sources:
- Instagram: dev.to/alfg/ffmpeg-for-instagram (reverse-engineered specs)
- X/Twitter: gehrcke.de + gist.github.com/transkatgirl (official requirements)
- YouTube: Upscale to 1080p + re-encode at 8 Mbps to avoid low-bitrate tier

Key findings:
- Instagram: 720p @ CRF 23, Main@L3.0, fixed GOP 72, 30fps, bt.709, yuv420p
- X/Twitter: 1080p @ 10 Mbps, High@L4.0, yuv420p (REQUIRED), bt.709, keyint=90
- YouTube: 1920x1080 @ 8 Mbps, High Profile, closed GOP 15, 30fps, bt.709, yuv420p
"""

import subprocess
from pathlib import Path

from xpst.config import EncodingConfig
from xpst.utils.logger import get_logger

logger = get_logger(__name__)


class VideoProcessor:
    """
    Video processing with FFmpeg.

    Handles platform-specific encoding for optimal quality preservation.
    """

    def __init__(self, ffmpeg_path: str = "ffmpeg"):
        """
        Initialize video processor.

        Args:
            ffmpeg_path: Path to ffmpeg binary
        """
        self.ffmpeg_path = ffmpeg_path
        self._verify_ffmpeg()

    def _verify_ffmpeg(self) -> None:
        """Verify FFmpeg is installed and accessible on PATH.

        Raises:
            RuntimeError: If FFmpeg binary is not found or returns error.
        """

        try:
            result = subprocess.run(
                [self.ffmpeg_path, "-version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                raise RuntimeError(f"FFmpeg returned non-zero exit code: {result.returncode}")
        except (FileNotFoundError, PermissionError, OSError):
            raise RuntimeError(
                f"FFmpeg not found at {self.ffmpeg_path}. "
                "Please install FFmpeg: https://ffmpeg.org/download.html"
            ) from None

    def get_video_info(self, video_path: Path) -> dict:
        """
        Get video file information.

        Args:
            video_path: Path to video file

        Returns:
            Dictionary with video info (width, height, duration, etc.)
        """
        cmd = [
            "ffprobe",
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

        Instagram optimal settings (research-verified):
        - 720p height (scale to fit)
        - CRF 23 (quality-based, ~3500 kbps)
        - H.264 Main Profile @ Level 3.0
        - Fixed GOP 72 (2.4 seconds at 30fps)
        - 30fps
        - yuv420p pixel format
        - bt.709 color space
        - AAC 256k audio
        """
        resolution = config.resolution or 720
        crf = config.crf or 23
        maxrate = config.maxrate or "3500k"
        profile = config.profile or "main"
        level = config.level or "3.0"
        gop = config.gop or 72
        fps = config.fps or 30
        color = config.color or "bt709"
        pix_fmt = config.pix_fmt or "yuv420p"

        return [
            self.ffmpeg_path,
            "-y",  # Overwrite output
            "-i", str(input_path),
            "-vf", f"scale=-2:{resolution},setsar=1,format={pix_fmt}",
            "-c:v", "libx264",
            "-preset", "slow",  # Better quality
            "-profile:v", profile,
            "-level:v", level,
            "-x264-params", f"scenecut=0:open_gop=0:min-keyint={gop}:keyint={gop}:ref=4",
            "-crf", str(crf),
            "-maxrate", maxrate,
            "-bufsize", f"{int(maxrate.replace('k', '').replace('M', '')) * 2}{'k' if 'k' in maxrate else 'M'}",
            "-color_primaries", color,
            "-color_trc", color,
            "-colorspace", color,
            "-r", str(fps),
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

        X/Twitter optimal settings (research-verified):
        - 1080p height (scale to fit)
        - 10 Mbps CBR (up to 25 Mbps supported)
        - H.264 High Profile @ Level 4.0
        - Fixed GOP 90 (3 seconds at 30fps)
        - 30fps
        - yuv420p pixel format (REQUIRED - X rejects other formats!)
        - bt.709 color space
        - AAC 256k audio
        - Lanczos scaling (high quality)
        """
        resolution = config.resolution or 1080
        bitrate = config.bitrate or "10M"
        maxrate = config.maxrate or "12M"
        profile = config.profile or "high"
        level = config.level or "4.0"
        gop = config.gop or 90
        fps = config.fps or 30
        color = config.color or "bt709"
        pix_fmt = config.pix_fmt or "yuv420p"

        return [
            self.ffmpeg_path,
            "-y",  # Overwrite output
            "-i", str(input_path),
            "-vf", f"scale=-2:{resolution}:flags=lanczos,setsar=1,format={pix_fmt}",
            "-c:v", "libx264",
            "-preset", "slow",  # Better quality
            "-profile:v", profile,
            "-level:v", level,
            "-x264-params", f"scenecut=0:open_gop=0:min-keyint={gop}:keyint={gop}:ref=4",
            "-b:v", bitrate,
            "-maxrate", maxrate,
            "-bufsize", f"{int(maxrate.replace('M', '').replace('k', '')) * 2}{'k' if 'k' in maxrate else 'M'}",
            "-color_primaries", color,
            "-color_trc", color,
            "-colorspace", color,
            "-r", str(fps),
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

        YouTube optimal settings:
        - Upscale to 1080x1920 (Lanczos scaler)
        - 8 Mbps CBR (maxrate 10M, bufsize 12M)
        - H.264 High Profile
        - Closed GOP 15 frames (0.5s at 30fps)
        - 30fps
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
        fps = config.fps or 30
        color = config.color or "bt709"
        pix_fmt = config.pix_fmt or "yuv420p"

        return [
            self.ffmpeg_path,
            "-y",
            "-i", str(input_path),
            "-vf", f"scale=-2:{resolution}:flags=lanczos,setsar=1,format={pix_fmt}",
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
            "-r", str(fps),
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
                except Exception:
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
