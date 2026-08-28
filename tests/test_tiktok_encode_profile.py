"""TikTok dedicated encode profile + hardware-encoder auto-detection.

Covers:
- TikTok no longer shares Instagram's encode profile: 1080x1920 long edge,
  CRF 20, maxrate 10M / bufsize 20M, GOP = 2 * source fps (30fps→60,
  60fps→120), +faststart, yuv420p, AAC 128k @ 44.1 kHz.
- Hardware encoder auto-detection (h264_videotoolbox / h264_nvenc /
  h264_qsv / h264_vaapi) from mocked `ffmpeg -encoders` output, with
  XPST_HW_ENCODER env / config.hw_encoder overrides and libx264 fallback.
- Runtime quality guard: hw-encoder failure falls back to libx264.
- Profile selection per platform in UploadService (tiktok → its own profile,
  instagram/threads → unchanged IG profile).

Unit tests run without ffmpeg. The real-encode integration test at the
bottom requires ffmpeg/ffprobe on PATH (like test_video_quality.py).
"""

import asyncio
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from xpst.config import EncodingConfig, VideoConfig, XPSTConfig
from xpst.services.upload_service import UploadService
from xpst.utils import video as video_mod
from xpst.utils.video import (
    HW_ENCODER_CANDIDATES,
    VideoProcessor,
    detect_hardware_encoder,
    resolve_encoder,
)

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

requires_ffmpeg = pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe not on PATH")


@pytest.fixture
def processor(monkeypatch) -> VideoProcessor:
    """VideoProcessor with the binary check stubbed (unit tests)."""
    monkeypatch.setattr(VideoProcessor, "_verify_ffmpeg", lambda self: None)
    proc = VideoProcessor(ffmpeg_path="/usr/bin/ffmpeg")
    monkeypatch.setattr(proc, "_source_fps", lambda path: 30.0)
    return proc


FAKE_ENCODERS_OUTPUT = """\
Encoders:
 V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (codec h264)
 V....D h264_videotoolbox    VideoToolbox H.264 Encoder (codec h264)
 V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)
 A....D aac                  AAC (Advanced Audio Coding)
"""


def _fake_run(stdout: str, returncode: int = 0):
    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = returncode
        result.stdout = stdout
        result.stderr = ""
        return result

    return fake_run


# ---------------------------------------------------------------------------
# Hardware encoder detection (mocked ffmpeg -encoders)
# ---------------------------------------------------------------------------


class TestDetectHardwareEncoder:
    def test_detects_videotoolbox_when_present(self, monkeypatch):
        monkeypatch.setattr(video_mod, "_available_hw_encoders", lambda path=None: {"h264_videotoolbox", "libx264"})
        # macOS preference order puts videotoolbox first
        monkeypatch.setattr(video_mod.sys, "platform", "darwin")
        assert detect_hardware_encoder() == "h264_videotoolbox"

    def test_detects_nvenc_when_only_nvenc_available(self, monkeypatch):
        monkeypatch.setattr(video_mod, "_available_hw_encoders", lambda path=None: {"h264_nvenc"})
        assert detect_hardware_encoder() == "h264_nvenc"

    def test_preference_order_non_mac_prefers_nvenc_over_videotoolbox(self, monkeypatch):
        monkeypatch.setattr(video_mod, "_available_hw_encoders", lambda path=None: set(HW_ENCODER_CANDIDATES))
        monkeypatch.setattr(video_mod.sys, "platform", "linux")
        assert detect_hardware_encoder() == "h264_nvenc"

    def test_returns_none_when_no_hw_encoders(self, monkeypatch):
        monkeypatch.setattr(video_mod, "_available_hw_encoders", lambda path=None: set())
        assert detect_hardware_encoder() is None

    def test_parses_fake_ffmpeg_encoders_output(self, monkeypatch):
        monkeypatch.setattr(video_mod.subprocess, "run", _fake_run(FAKE_ENCODERS_OUTPUT))
        monkeypatch.setattr(video_mod.sys, "platform", "darwin")
        assert detect_hardware_encoder() == "h264_videotoolbox"

    def test_ffmpeg_crash_returns_none(self, monkeypatch):
        monkeypatch.setattr(video_mod.subprocess, "run", _fake_run("", returncode=1))
        assert detect_hardware_encoder() is None

    def test_ffmpeg_missing_returns_none(self, monkeypatch):
        def raise_fnf(cmd, **kwargs):
            raise FileNotFoundError(cmd)

        monkeypatch.setattr(video_mod.subprocess, "run", raise_fnf)
        assert detect_hardware_encoder() is None


class TestResolveEncoder:
    def test_auto_detect_picks_videotoolbox(self, monkeypatch):
        monkeypatch.delenv("XPST_HW_ENCODER", raising=False)
        monkeypatch.setattr(video_mod, "_available_hw_encoders", lambda path=None: {"h264_videotoolbox"})
        assert resolve_encoder() == "h264_videotoolbox"

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("XPST_HW_ENCODER", "h264_nvenc")
        monkeypatch.setattr(video_mod, "_available_hw_encoders", lambda path=None: {"h264_videotoolbox", "h264_nvenc"})
        assert resolve_encoder() == "h264_nvenc"

    def test_env_override_forces_software(self, monkeypatch):
        monkeypatch.setenv("XPST_HW_ENCODER", "libx264")
        monkeypatch.setattr(video_mod, "_available_hw_encoders", lambda path=None: {"h264_videotoolbox"})
        assert resolve_encoder() == "libx264"

    def test_invalid_env_override_falls_back_to_detection(self, monkeypatch):
        monkeypatch.setenv("XPST_HW_ENCODER", "h264_doesnotexist")
        monkeypatch.setattr(video_mod, "_available_hw_encoders", lambda path=None: {"h264_videotoolbox"})
        assert resolve_encoder() == "h264_videotoolbox"

    def test_config_field_override(self, monkeypatch):
        monkeypatch.delenv("XPST_HW_ENCODER", raising=False)
        monkeypatch.setattr(video_mod, "_available_hw_encoders", lambda path=None: {"h264_videotoolbox", "h264_qsv"})
        assert resolve_encoder(config_hw_encoder="h264_qsv") == "h264_qsv"

    def test_no_hardware_falls_back_to_libx264(self, monkeypatch):
        monkeypatch.delenv("XPST_HW_ENCODER", raising=False)
        monkeypatch.setattr(video_mod, "_available_hw_encoders", lambda path=None: set())
        assert resolve_encoder() == "libx264"

    def test_tiktok_cmd_uses_env_override_encoder(self, monkeypatch, processor):
        monkeypatch.setenv("XPST_HW_ENCODER", "h264_nvenc")
        monkeypatch.setattr(video_mod, "_available_hw_encoders", lambda path=None: {"h264_nvenc"})
        config = VideoConfig().encoding_tiktok
        cmd = processor._build_tiktok_cmd(Path("in.mp4"), Path("out.mp4"), config)
        idx = cmd.index("-c:v")
        assert cmd[idx + 1] == "h264_nvenc"
        # CRF does not apply to hardware encoders — bitrate-capped instead
        assert "-crf" not in cmd
        assert "-preset" not in cmd
        assert cmd[cmd.index("-b:v") + 1] == "10M"
        assert cmd[cmd.index("-maxrate") + 1] == "10M"
        assert cmd[cmd.index("-bufsize") + 1] == "20M"


# ---------------------------------------------------------------------------
# TikTok profile parameters
# ---------------------------------------------------------------------------


class TestTikTokProfile:
    def test_libx264_fallback_command_params(self, processor):
        config = VideoConfig().encoding_tiktok
        cmd = processor._build_tiktok_cmd(Path("in.mp4"), Path("out.mp4"), config, encoder="libx264")
        joined = " ".join(cmd)
        assert "if(gt(a,1)" in joined and "1920" in joined  # long-edge scale
        assert "format=yuv420p" in joined
        assert cmd[cmd.index("-c:v") + 1] == "libx264"
        assert cmd[cmd.index("-preset") + 1] == "slow"
        assert cmd[cmd.index("-crf") + 1] == "20"
        assert cmd[cmd.index("-maxrate") + 1] == "10M"
        assert cmd[cmd.index("-bufsize") + 1] == "20M"
        assert cmd[cmd.index("-profile:v") + 1] == "high"
        assert cmd[cmd.index("-level:v") + 1] == "4.0"
        assert "keyint=60" in cmd[cmd.index("-x264-params") + 1]
        assert cmd[cmd.index("-fpsmax") + 1] == "60"
        assert cmd[cmd.index("-c:a") + 1] == "aac"
        assert cmd[cmd.index("-b:a") + 1] == "128k"
        assert cmd[cmd.index("-ar") + 1] == "44100"
        assert "+faststart" in cmd

    def test_gop_is_two_times_source_fps(self, processor):
        config = VideoConfig().encoding_tiktok
        cmd = processor._build_tiktok_cmd(Path("in.mp4"), Path("out.mp4"), config, encoder="libx264")
        x264 = cmd[cmd.index("-x264-params") + 1]
        assert "keyint=60" in x264 and "min-keyint=60" in x264

    def test_gop_120_for_60fps_source(self, monkeypatch):
        monkeypatch.setattr(VideoProcessor, "_verify_ffmpeg", lambda self: None)
        proc = VideoProcessor(ffmpeg_path="/usr/bin/ffmpeg")
        monkeypatch.setattr(proc, "_source_fps", lambda path: 60.0)
        config = VideoConfig().encoding_tiktok
        cmd = proc._build_tiktok_cmd(Path("in.mp4"), Path("out.mp4"), config, encoder="libx264")
        assert "keyint=120" in cmd[cmd.index("-x264-params") + 1]

    def test_gop_capped_by_fps_cap(self):
        # 90fps source with a 60fps cap → effective 60 → GOP 120
        assert VideoProcessor._tiktok_gop(90.0, 60) == 120

    def test_gop_defaults_to_60_when_probe_fails(self):
        assert VideoProcessor._tiktok_gop(None, 60) == 60

    def test_explicit_gop_override_respected(self, processor):
        config = EncodingConfig(resolution=1920, crf=20, maxrate="10M", bufsize="20M", gop=48, fps=60)
        cmd = processor._build_tiktok_cmd(Path("in.mp4"), Path("out.mp4"), config, encoder="libx264")
        assert "keyint=48" in cmd[cmd.index("-x264-params") + 1]

    def test_default_config_matches_spec(self):
        cfg = VideoConfig().encoding_tiktok
        assert cfg.resolution == 1920
        assert cfg.crf == 20
        assert cfg.maxrate == "10M"
        assert cfg.bufsize == "20M"
        assert cfg.profile == "high"
        assert cfg.level == "4.0"
        assert cfg.fps == 60
        assert cfg.pix_fmt == "yuv420p"
        assert cfg.gop is None  # dynamic: 2 * source fps
        # Must be a distinct object from the Instagram profile
        assert cfg is not VideoConfig().encoding_instagram

    def test_tiktok_profile_serialized_to_config_dict(self, tmp_path):
        cfg = XPSTConfig()
        cfg.save(str(tmp_path / "config.yaml"))
        import yaml

        saved = yaml.safe_load((tmp_path / "config.yaml").read_text())
        tiktok = saved["video"]["encoding"]["tiktok"]
        assert tiktok["crf"] == 20
        assert tiktok["maxrate"] == "10M"
        assert tiktok["bufsize"] == "20M"
        assert "hw_encoder" in tiktok

    def test_config_loader_reads_tiktok_section(self, tmp_path):
        raw = {
            "video": {
                "encoding": {
                    "tiktok": {"crf": 18, "bufsize": "24M", "hw_encoder": "h264_videotoolbox"},
                },
            },
        }
        config = XPSTConfig._merge_config(XPSTConfig(), raw)
        assert config.video.encoding_tiktok.crf == 18
        assert config.video.encoding_tiktok.bufsize == "24M"
        assert config.video.encoding_tiktok.hw_encoder == "h264_videotoolbox"


# ---------------------------------------------------------------------------
# Profile selection per platform (Instagram unchanged)
# ---------------------------------------------------------------------------


class TestInstagramProfileUnchanged:
    def test_instagram_cmd_still_uses_ig_params(self, processor):
        config = VideoConfig().encoding_instagram
        cmd = processor._build_instagram_cmd(Path("in.mp4"), Path("out.mp4"), config)
        assert cmd[cmd.index("-c:v") + 1] == "libx264"
        assert cmd[cmd.index("-crf") + 1] == "20"
        assert "keyint=72" in cmd[cmd.index("-x264-params") + 1]
        assert cmd[cmd.index("-b:a") + 1] == "256k"  # NOT TikTok's 128k


def _bare_upload_service(config: XPSTConfig, video_processor) -> UploadService:
    """UploadService with dummy collaborators (only config/processor used)."""
    return UploadService(
        video_processor=video_processor,
        circuit_breakers=MagicMock(),
        quota_manager=MagicMock(),
        state=MagicMock(),
        notifier=MagicMock(),
        shutdown_handler=MagicMock(),
        config=config,
    )


class TestProfileSelectionPerPlatform:
    @pytest.mark.parametrize(
        ("platform", "expected_profile"),
        [
            ("youtube", "encoding_youtube"),
            ("instagram", "encoding_instagram"),
            ("x", "encoding_x"),
            ("tiktok", "encoding_tiktok"),
            ("threads", "encoding_instagram"),
        ],
    )
    def test_encode_uses_platform_profile(self, platform, expected_profile):
        config = XPSTConfig()
        captured: dict = {}

        class FakeProcessor:
            def is_platform_compliant(self, path, plat, enc):
                return False, "force encode"

            def encode_for_platform(self, input_path, output_path, plat, enc):
                captured["platform"] = plat
                captured["config"] = enc
                return output_path

        service = _bare_upload_service(config, FakeProcessor())
        asyncio.run(service._encode_for_platform(Path("video.mp4"), platform))
        assert captured["platform"] == platform
        assert captured["config"] is getattr(config.video, expected_profile)

    def test_tiktok_profile_differs_from_instagram(self):
        config = XPSTConfig()
        assert config.video.encoding_tiktok is not config.video.encoding_instagram
        assert config.video.encoding_tiktok.bufsize == "20M"
        assert config.video.encoding_instagram.bufsize is None  # IG derives it


# ---------------------------------------------------------------------------
# Runtime quality guard: hw encoder failure → libx264 fallback
# ---------------------------------------------------------------------------


class TestHwEncoderRuntimeFallback:
    def test_encode_falls_back_to_libx264_on_hw_failure(self, processor, monkeypatch, tmp_path):
        config = VideoConfig().encoding_tiktok
        calls: list[list[str]] = []

        # Simulate a machine where videotoolbox was auto-detected
        monkeypatch.setattr(video_mod, "resolve_encoder", lambda *a, **k: "h264_videotoolbox")

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            result = MagicMock()
            ok = "-crf" in cmd  # libx264 attempts succeed, hw attempts fail
            result.returncode = 0 if ok else 1
            result.stdout = ""
            result.stderr = "" if ok else "hw encode boom"
            if ok:
                Path(cmd[-1]).write_bytes(b"x" * 2048)
            return result

        monkeypatch.setattr(video_mod.subprocess, "run", fake_run)
        out = tmp_path / "out.mp4"
        out.write_bytes(b"x" * 2048)  # pass the min-size check
        result_path = processor.encode_for_platform(Path("in.mp4"), out, "tiktok", config)
        assert result_path == out
        # First attempt hardware, retries with libx264 presets slow then medium
        assert any("h264_videotoolbox" in c for c in calls)
        assert any(
            "-preset" in c and c[c.index("-preset") + 1] == "slow"
            for c in calls
            if "-c:v" in c and c[c.index("-c:v") + 1] == "libx264"
        )
        assert calls[-1][calls[-1].index("-c:v") + 1] == "libx264"


# ---------------------------------------------------------------------------
# Integration: real ffmpeg encode through the TikTok pipeline
# ---------------------------------------------------------------------------


@requires_ffmpeg
class TestTikTokRealEncode:
    def test_real_tiktok_encode_output(self, tmp_path):
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        ffprobe = shutil.which("ffprobe") or "ffprobe"
        src = tmp_path / "src.mp4"
        # 1080x1920 portrait, 30fps, 2s, with audio — synthetic test clip
        subprocess.run(
            [
                ffmpeg, "-y",
                "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=30:duration=2",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest",
                str(src),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )

        # Force the software path so the gate is deterministic in CI
        # containers without hardware encoders.
        config = VideoConfig().encoding_tiktok
        processor = VideoProcessor()
        out = tmp_path / "src_tiktok.mp4"
        result = processor.encode_for_platform(src, out, "tiktok", config)
        assert result == out and out.stat().st_size > 1000

        probe = subprocess.run(
            [
                ffprobe, "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", str(out),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        import json

        info = json.loads(probe.stdout)
        vstream = next(s for s in info["streams"] if s["codec_type"] == "video")
        astream = next(s for s in info["streams"] if s["codec_type"] == "audio")
        assert vstream["codec_name"] == "h264"
        assert vstream["pix_fmt"] == "yuv420p"
        assert vstream["width"] == 1080 and vstream["height"] == 1920
        assert astream["codec_name"] == "aac"
        assert int(astream["sample_rate"]) == 44100
        assert vstream["avg_frame_rate"] in ("30/1", "30000/1001")
        # +faststart: moov atom must precede mdat in the file
        data = out.read_bytes()
        assert data.find(b"moov") != -1 and data.find(b"mdat") != -1
        assert data.find(b"moov") < data.find(b"mdat")
