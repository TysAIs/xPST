"""Adversarial-QA media fixtures: real clips generated with ffmpeg.

Generates the corpus used by test_upload_quality_adversarial.py:

- fourk60            3840x2160@60 H.264 (oversized long edge + fps)
- vertical_1080p30   1080x1920@30 H.264+AAC (compliant portrait source)
- old_720p_mp3       1280x720@30 H.264+MP3 (legacy audio codec)
- aspect_43          1440x1080@30 H.264+AAC (4:3, compliant)
- vfr                640x360 concatenated 15fps+30fps (variable frame rate)
- hevc10_bt709       1280x720@30 HEVC 10-bit bt.709 (wrong codec + depth)
- hdr_hevc10         1920x1080@60 HEVC 10-bit HDR10 (PQ transfer)
- micro3s            640x360@24, exactly 3s (micro clip)
- coverart_m4a       AAC audio + attached_pic cover (no real video)

Every generator is deterministic (lavfi sources) and fast (<2s per clip).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

FFMPEG = str(Path.home() / "bin" / "ffmpeg")
FFPROBE = str(Path.home() / "bin" / "ffmpeg-static" / "ffprobe")

# Fallbacks when the preferred static binaries are missing
if not Path(FFMPEG).exists():
    FFMPEG = "ffmpeg"
if not Path(FFPROBE).exists():
    FFPROBE = "ffprobe"


def _base_args() -> list[str]:
    return ["-y", "-hide_banner", "-loglevel", "error"]


def missing_encoders() -> list[str]:
    """Encoders this corpus needs that the local ffmpeg build lacks."""
    required = ("libx264", "libx265", "aac", "libmp3lame")
    try:
        out = subprocess.run(
            [FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return list(required)
    return [e for e in required if e not in out]


def generate_media(dir_path: Path) -> dict[str, Path]:
    """Generate the full adversarial corpus into *dir_path*. Idempotent."""
    dir_path.mkdir(parents=True, exist_ok=True)
    clips: dict[str, Path] = {}

    def run(args: list[str]) -> None:
        result = subprocess.run([FFMPEG, *_base_args(), *args], capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            raise RuntimeError(f"fixture generation failed: {result.stderr[-500:]}")

    def video_out(name: str) -> list[str]:
        return ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
                str(dir_path / f"{name}.mp4")]

    # 1. 4K60 — exceeds every profile's long edge and fps cap
    p = dir_path / "fourk60.mp4"
    if not p.exists():
        run(["-f", "lavfi", "-i", "testsrc2=size=3840x2160:rate=60",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
             "-t", "2", "-crf", "34", *video_out("fourk60")])
    clips["fourk60"] = p

    # 2. Compliant portrait 1080x1920@30 with bitrate kept under every cap
    p = dir_path / "vertical_1080p30.mp4"
    if not p.exists():
        run(["-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
             "-t", "2", "-crf", "28", "-maxrate", "6M", "-bufsize", "12M",
             *video_out("vertical_1080p30")])
    clips["vertical_1080p30"] = p

    # 3. Legacy 720p H.264 with MP3 audio (audio codec breaks compliance)
    p = dir_path / "old_720p_mp3.mp4"
    if not p.exists():
        run(["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
             "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=44100",
             "-t", "2", "-crf", "28", "-c:v", "libx264", "-preset", "ultrafast",
             "-pix_fmt", "yuv420p", "-c:a", "libmp3lame", "-b:a", "128k",
             str(dir_path / "old_720p_mp3.mp4")])
    clips["old_720p_mp3"] = p

    # 4. 4:3 aspect 1440x1080@30, compliant
    p = dir_path / "aspect_43.mp4"
    if not p.exists():
        run(["-f", "lavfi", "-i", "testsrc2=size=1440x1080:rate=30",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
             "-t", "2", "-crf", "28", "-maxrate", "6M", "-bufsize", "12M",
             *video_out("aspect_43")])
    clips["aspect_43"] = p

    # 5. VFR: concat 1s@15fps + 1s@30fps (re-encode keeps irregular timestamps)
    p = dir_path / "vfr.mp4"
    if not p.exists():
        run(["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15:d=1",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
             "-t", "1", "-c:v", "libx264", "-preset", "ultrafast",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
             str(dir_path / "_vfr_a.mp4")])
        run(["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:d=1",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
             "-t", "1", "-c:v", "libx264", "-preset", "ultrafast",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
             str(dir_path / "_vfr_b.mp4")])
        (dir_path / "_vfr_list.txt").write_text(
            f"file '{dir_path / '_vfr_a.mp4'}'\nfile '{dir_path / '_vfr_b.mp4'}'\n"
        )
        run(["-f", "concat", "-safe", "0", "-i", str(dir_path / "_vfr_list.txt"),
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k", str(dir_path / "vfr.mp4")])
        (dir_path / "_vfr_a.mp4").unlink(missing_ok=True)
        (dir_path / "_vfr_b.mp4").unlink(missing_ok=True)
        (dir_path / "_vfr_list.txt").unlink(missing_ok=True)
    clips["vfr"] = p

    # 6. HEVC 10-bit bt.709 (wrong codec, wrong bit depth for profiles)
    p = dir_path / "hevc10_bt709.mp4"
    if not p.exists():
        run(["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
             "-t", "2", "-c:v", "libx265", "-preset", "ultrafast", "-crf", "30",
             "-pix_fmt", "yuv420p10le",
             "-x265-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709:log-level=error",
             "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
             str(dir_path / "hevc10_bt709.mp4")])
    clips["hevc10_bt709"] = p

    # 7. HDR10 (PQ) HEVC 10-bit — must be tone-mapped, never re-tagged
    p = dir_path / "hdr_hevc10.mp4"
    if not p.exists():
        run(["-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=60",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
             "-t", "2", "-c:v", "libx265", "-preset", "ultrafast", "-crf", "30",
             "-pix_fmt", "yuv420p10le",
             "-x265-params",
             "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:log-level=error",
             "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
             str(dir_path / "hdr_hevc10.mp4")])
    clips["hdr_hevc10"] = p

    # 8. Micro 3-second clip
    p = dir_path / "micro3s.mp4"
    if not p.exists():
        run(["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
             "-t", "3", "-crf", "28", *video_out("micro3s")])
    clips["micro3s"] = p

    # 9. Audio + attached_pic cover art (must never become a 1-frame "video")
    p = dir_path / "coverart_m4a.m4a"
    if not p.exists():
        run(["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
             "-t", "3", "-c:a", "aac", "-b:a", "128k", str(dir_path / "_tmp_audio.m4a")])
        run(["-f", "lavfi", "-i", "testsrc2=size=320x320:rate=1",
             "-frames:v", "1", str(dir_path / "_tmp_cover.jpg")])
        run(["-i", str(dir_path / "_tmp_audio.m4a"), "-i", str(dir_path / "_tmp_cover.jpg"),
             "-map", "0:a", "-map", "1:v", "-c", "copy",
             "-disposition:v:0", "attached_pic", "-metadata:s:v", "title=Album cover",
             str(dir_path / "coverart_m4a.m4a")])
        (dir_path / "_tmp_audio.m4a").unlink(missing_ok=True)
        (dir_path / "_tmp_cover.jpg").unlink(missing_ok=True)
    clips["coverart_m4a"] = p

    return clips


def probe(path: Path) -> dict:
    """ffprobe a media file, return the parsed JSON dict."""
    result = subprocess.run(
        [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {result.stderr[-300:]}")
    return json.loads(result.stdout)


def real_video_stream(info: dict) -> dict | None:
    """First non-attached-pic video stream (independent of the impl under test)."""
    for s in info.get("streams", []):
        if s.get("codec_type") != "video":
            continue
        disp = s.get("disposition") or {}
        if disp.get("attached_pic"):
            continue
        return s
    return None


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_frame_rate(value: str) -> float:
    try:
        num, _, den = (value or "").partition("/")
        num = float(num)
        den = float(den) if den else 1.0
        return num / den if den else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def quality_metrics(src: Path, out: Path, out_w: int, out_h: int) -> dict:
    """PSNR (always) and VMAF (best effort) of *out* against *src*.

    The source is bicubically scaled to the output's geometry so the
    comparison is geometry-normalized (downscaled re-encodes included).
    """
    filters = (
        f"[0:v]scale={out_w}:{out_h}:flags=bicubic,format=yuv420p,setsar=1[ref];"
        f"[1:v]format=yuv420p,setsar=1[dis];[dis][ref]"
    )
    result = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(src), "-i", str(out),
         "-filter_complex", filters + "psnr", "-f", "null", "-"],
        capture_output=True, text=True, timeout=600,
    )
    metrics: dict[str, float | None] = {"psnr_avg": None, "psnr_y": None, "vmaf_mean": None}
    if result.returncode == 0:
        import re
        m = re.search(r"PSNR y:([\d.]+|inf) .*average:([\d.]+|inf)", result.stderr)
        if m:
            metrics["psnr_y"] = float(m.group(1)) if m.group(1) != "inf" else 99.0
            metrics["psnr_avg"] = float(m.group(2)) if m.group(2) != "inf" else 99.0

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        log_path = tf.name
    try:
        result = subprocess.run(
            [FFMPEG, "-hide_banner", "-i", str(src), "-i", str(out),
             "-filter_complex",
             filters + f"libvmaf=log_fmt=json:log_path={log_path}:n_threads=4",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode == 0 and Path(log_path).exists():
            data = json.loads(Path(log_path).read_text())
            v = data.get("pooled_metrics", {}).get("vmaf", {})
            metrics["vmaf_mean"] = round(float(v.get("mean", 0.0)), 2) or None
    except Exception:
        pass
    finally:
        Path(log_path).unlink(missing_ok=True)
    return metrics
