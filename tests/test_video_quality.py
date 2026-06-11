"""Video fidelity tests (F2 / G12-G17).

Covers the 2026-06-11 quality overhaul:
- G12: orientation-aware long-edge scaling (the height-keyed `scale=-2:{res}`
  crushed 1080x1920 portrait to 608x1080 — the root cause of degraded uploads)
- G13: Instagram profile modernization (720p/main/3500k → 1920 long edge/high/10M)
- G14: frame rate as a cap (-fpsmax), never a force (-r 30 halved 60fps sources)
- G15: passthrough probe — already-compliant sources skip the re-encode
- bufsize parsing for fractional rates ("3.5M")

Unit tests run without ffmpeg. Integration tests encode real synthetic
assets across aspect ratios and verify dimensions/fps with ffprobe.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from xpst.config import EncodingConfig, VideoConfig
from xpst.utils.video import (
    VideoProcessor,
    build_scale_filter,
    double_rate,
)

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

requires_ffmpeg = pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe not on PATH")


@pytest.fixture
def processor(monkeypatch) -> VideoProcessor:
    """VideoProcessor with the binary check stubbed (unit tests)."""
    monkeypatch.setattr(VideoProcessor, "_verify_ffmpeg", lambda self: None)
    return VideoProcessor()


@pytest.fixture
def real_processor() -> VideoProcessor:
    """VideoProcessor against the real ffmpeg binary (integration tests)."""
    return VideoProcessor()


def _vf_of(cmd: list[str]) -> str:
    return cmd[cmd.index("-vf") + 1]


# ---------------------------------------------------------------------------
# Unit: bufsize / rate parsing
# ---------------------------------------------------------------------------


class TestBufsizeUnitParse:
    def test_bufsize_unit_parse_fractional_megabit(self):
        assert double_rate("3.5M") == "7M"

    def test_bufsize_unit_parse_kilobit(self):
        assert double_rate("3500k") == "7000k"

    def test_bufsize_unit_parse_whole_megabit(self):
        assert double_rate("10M") == "20M"
        assert double_rate("12M") == "24M"

    def test_bufsize_unit_parse_garbage_passthrough(self):
        assert double_rate("notarate") == "notarate"


# ---------------------------------------------------------------------------
# Unit: orientation-aware scale filter
# ---------------------------------------------------------------------------


class TestScaleFilter:
    def test_filter_is_orientation_aware(self):
        f = build_scale_filter(1920)
        assert "if(gt(a,1)" in f
        assert "min(1920,iw)" in f and "min(1920,ih)" in f

    def test_no_upscale_by_default(self):
        assert "min(" in build_scale_filter(1920)

    def test_upscale_mode_targets_long_edge_exactly(self):
        f = build_scale_filter(1920, upscale=True)
        assert "min(" not in f
        assert "1920" in f

    def test_flags_appended(self):
        assert build_scale_filter(1920, flags="lanczos").endswith(":flags=lanczos")


# ---------------------------------------------------------------------------
# Unit: command builders honor the fidelity rules
# ---------------------------------------------------------------------------


class TestBuilderCommands:
    @pytest.mark.parametrize("platform", ["youtube", "instagram", "x"])
    def test_no_height_keyed_scale(self, processor, platform):
        cfg = getattr(VideoConfig(), f"encoding_{platform}")
        cmd = getattr(processor, f"_build_{platform}_cmd")(Path("in.mp4"), Path("out.mp4"), cfg)
        assert "scale=-2:" not in _vf_of(cmd)
        assert "if(gt(a,1)" in _vf_of(cmd)

    @pytest.mark.parametrize("platform", ["youtube", "instagram", "x"])
    def test_no_forced_frame_rate(self, processor, platform):
        cfg = getattr(VideoConfig(), f"encoding_{platform}")
        cmd = getattr(processor, f"_build_{platform}_cmd")(Path("in.mp4"), Path("out.mp4"), cfg)
        assert "-r" not in cmd, "-r forces the output rate; use -fpsmax as a cap"
        assert "-fpsmax" in cmd
        assert cmd[cmd.index("-fpsmax") + 1] == "60"

    def test_instagram_profile_modernized(self):
        cfg = VideoConfig().encoding_instagram
        assert cfg.resolution >= 1080, "IG long-edge target must allow 1080x1920 Reels"
        assert cfg.profile == "high"
        assert cfg.crf <= 20
        assert cfg.maxrate != "3500k"

    def test_youtube_upscales_instagram_x_do_not(self, processor):
        vc = VideoConfig()
        yt = _vf_of(processor._build_youtube_cmd(Path("i.mp4"), Path("o.mp4"), vc.encoding_youtube))
        ig = _vf_of(processor._build_instagram_cmd(Path("i.mp4"), Path("o.mp4"), vc.encoding_instagram))
        x = _vf_of(processor._build_x_cmd(Path("i.mp4"), Path("o.mp4"), vc.encoding_x))
        assert "min(" not in yt.split(",")[0], "YouTube deliberately upscales to the 1080p+ tier"
        assert "min(" in ig and "min(" in x, "IG/X must never upscale"

    def test_fractional_maxrate_does_not_crash_builder(self, processor):
        cfg = EncodingConfig(resolution=1920, crf=20, maxrate="3.5M", profile="high", level="4.0", gop=72, fps=60)
        cmd = processor._build_instagram_cmd(Path("i.mp4"), Path("o.mp4"), cfg)
        assert cmd[cmd.index("-bufsize") + 1] == "7M"


# ---------------------------------------------------------------------------
# Integration: real encodes across aspect ratios, verified with ffprobe
# ---------------------------------------------------------------------------


def _make_source(path: Path, size: str, rate: int, duration: float = 1.0) -> Path:
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"testsrc2=size={size}:rate={rate}:duration={duration}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
            str(path),
        ],
        check=True, capture_output=True, timeout=120,
    )
    return path


def _probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(path)],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout
    return next(s for s in json.loads(out)["streams"] if s["codec_type"] == "video")


def _fps(stream: dict) -> float:
    num, den = stream["avg_frame_rate"].split("/")
    return float(num) / float(den)


@pytest.fixture(scope="module")
def assets(tmp_path_factory) -> dict[str, Path]:
    if not FFMPEG_AVAILABLE:
        pytest.skip("ffmpeg/ffprobe not on PATH")
    root = tmp_path_factory.mktemp("video_assets")
    return {
        "portrait": _make_source(root / "portrait_1080x1920_30.mp4", "1080x1920", 30),
        "landscape": _make_source(root / "landscape_1920x1080_30.mp4", "1920x1080", 30),
        "square": _make_source(root / "square_1080x1080_30.mp4", "1080x1080", 30),
        "portrait60": _make_source(root / "portrait_720x1280_60.mp4", "720x1280", 60),
        "oversize": _make_source(root / "oversize_2560x1440_30.mp4", "2560x1440", 30),
    }


@requires_ffmpeg
class TestRealEncodes:
    @pytest.mark.parametrize("platform", ["youtube", "instagram", "x"])
    def test_vertical_1080x1920_preserved(self, real_processor, assets, tmp_path, platform):
        """The owner's gripe: 9:16 video must NOT be crushed to 608x1080."""
        cfg = getattr(VideoConfig(), f"encoding_{platform}")
        out = real_processor.encode_for_platform(
            assets["portrait"], tmp_path / f"p_{platform}.mp4", platform, cfg
        )
        stream = _probe(out)
        assert (stream["width"], stream["height"]) == (1080, 1920), (
            f"{platform} crushed portrait to {stream['width']}x{stream['height']}"
        )

    def test_landscape_dimensions_preserved(self, real_processor, assets, tmp_path):
        cfg = VideoConfig().encoding_x
        out = real_processor.encode_for_platform(
            assets["landscape"], tmp_path / "l_x.mp4", "x", cfg
        )
        stream = _probe(out)
        assert (stream["width"], stream["height"]) == (1920, 1080)

    def test_square_not_upscaled_on_instagram(self, real_processor, assets, tmp_path):
        cfg = VideoConfig().encoding_instagram
        out = real_processor.encode_for_platform(
            assets["square"], tmp_path / "s_ig.mp4", "instagram", cfg
        )
        stream = _probe(out)
        assert (stream["width"], stream["height"]) == (1080, 1080)

    def test_fps_preserved_60(self, real_processor, assets, tmp_path):
        """60fps sources must stay 60fps (the old -r 30 halved them)."""
        cfg = VideoConfig().encoding_instagram
        out = real_processor.encode_for_platform(
            assets["portrait60"], tmp_path / "p60_ig.mp4", "instagram", cfg
        )
        assert _fps(_probe(out)) > 50, "60fps source was downsampled"

    def test_oversize_downscaled_to_long_edge(self, real_processor, assets, tmp_path):
        cfg = VideoConfig().encoding_x
        out = real_processor.encode_for_platform(
            assets["oversize"], tmp_path / "o_x.mp4", "x", cfg
        )
        stream = _probe(out)
        assert max(stream["width"], stream["height"]) == 1920


@requires_ffmpeg
class TestPassthrough:
    def test_passthrough_skips_compliant(self, real_processor, assets):
        """ISC-17/G15: an already-compliant source must not be re-encoded."""
        compliant, reason = real_processor.is_platform_compliant(
            assets["portrait"], "instagram", VideoConfig().encoding_instagram
        )
        assert compliant, f"compliant 1080x1920 h264/yuv420p source rejected: {reason}"

    def test_oversize_source_is_not_compliant(self, real_processor, assets):
        compliant, reason = real_processor.is_platform_compliant(
            assets["oversize"], "x", VideoConfig().encoding_x
        )
        assert not compliant
        assert "long edge" in reason

    def test_high_fps_source_requires_encode_when_capped(self, real_processor, assets):
        cfg = EncodingConfig(resolution=1920, fps=30, profile="high")
        compliant, reason = real_processor.is_platform_compliant(
            assets["portrait60"], "instagram", cfg
        )
        assert not compliant
        assert "fps" in reason
