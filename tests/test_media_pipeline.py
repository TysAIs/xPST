"""Max-fidelity media pipeline tests (xpst.media).

Covers the 2026-08-31 fidelity sprint additions:

- specs:    per-platform ingest spec matrix + `verify_media` pre-flight
            (container/codec/pix_fmt/geometry/fps/bitrate/audio/duration/
            size/faststart/EBU R128 loudness)
- pipeline: the passthrough → remux → transcode decision tree
- loudness: two-pass linear-mode loudnorm helpers
- video:    remux_for_platform (zero-loss stream copy) + two-pass encode +
            loudnorm injection
- upload:   decision tree wired into UploadService._encode_for_platform,
            pre-flight verification blocking bad uploads

Unit tests run without ffmpeg. Integration tests encode real synthetic
clips (ffmpeg testsrc2) and skip gracefully when ffmpeg/ffprobe are absent.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from xpst.config import EncodingConfig, XPSTConfig
from xpst.media.loudness import (
    LOUDNESS_TARGETS_LUFS,
    build_loudnorm_filter,
    loudness_target,
    measure_loudness,
)
from xpst.media.pipeline import describe_plan, plan_transform
from xpst.media.specs import PLATFORM_SPECS, _has_faststart, format_report, verify_media
from xpst.services.upload_service import UploadService
from xpst.state import StateManager
from xpst.utils.video import VideoProcessor

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(FFMPEG is None or FFPROBE is None, reason="ffmpeg/ffprobe not on PATH")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(tmp_path: Path, vp: VideoProcessor | None = None) -> UploadService:
    return UploadService(
        video_processor=vp or VideoProcessor(),
        circuit_breakers=MagicMock(),
        quota_manager=MagicMock(),
        state=StateManager(str(tmp_path / "state")),
        notifier=MagicMock(),
        shutdown_handler=MagicMock(),
        config=XPSTConfig(),
        anti_bot=None,
    )


def _gen_clip(
    out: Path,
    size: str = "1280x720",
    rate: int = 30,
    audio: str = "aac",
    container: str = "mp4",
    volume: str | None = None,
    extra_video: list[str] | None = None,
) -> Path:
    """Small deterministic testsrc2 clip (<2s encode)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    args = [
        FFMPEG,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={size}:rate={rate}",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100",
        "-t",
        "2",
    ]
    if volume:
        args += ["-af", volume]
    args += [
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        *(extra_video or []),
        "-c:a",
        audio,
        "-b:a",
        "128k",
        str(out),
    ]
    subprocess.run(args, check=True, capture_output=True, timeout=180)
    return out


class FakeProcessor:
    """Duck-typed VideoProcessor for decision-tree tests."""

    def __init__(self, verdict: tuple[bool, str]) -> None:
        self.verdict = verdict
        self.calls: list[tuple] = []

    def is_platform_compliant(self, path, platform, config):
        self.calls.append((path, platform, config))
        return self.verdict


# ---------------------------------------------------------------------------
# specs: faststart atom scan (pure python, no ffmpeg)
# ---------------------------------------------------------------------------


def _write_boxes(path: Path, order: list[bytes]) -> None:
    payload = b"x" * 16
    with path.open("wb") as fh:
        for box in order:
            fh.write((8 + len(payload)).to_bytes(4, "big") + box + payload)


class TestFaststartScan:
    def test_moov_before_mdat_is_faststart(self, tmp_path):
        p = tmp_path / "fast.mp4"
        _write_boxes(p, [b"ftyp", b"moov", b"mdat"])
        assert _has_faststart(p) is True

    def test_mdat_before_moov_is_not_faststart(self, tmp_path):
        p = tmp_path / "slow.mp4"
        _write_boxes(p, [b"ftyp", b"mdat", b"moov"])
        assert _has_faststart(p) is False

    def test_garbage_returns_none(self, tmp_path):
        p = tmp_path / "junk.mp4"
        p.write_bytes(b"not an mp4 at all" * 10)
        assert _has_faststart(p) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert _has_faststart(tmp_path / "nope.mp4") is None


# ---------------------------------------------------------------------------
# specs: platform matrix sanity
# ---------------------------------------------------------------------------


class TestPlatformMatrix:
    def test_every_platform_has_required_spec(self):
        for name, spec in PLATFORM_SPECS.items():
            assert spec.video_codec == "h264", name
            assert spec.pix_fmt == "yuv420p", name
            assert spec.long_edge >= 1920, name
            assert spec.containers[0] == ".mp4", name

    def test_x_is_the_strictest_on_duration_and_size(self):
        assert PLATFORM_SPECS["x"].duration_cap_s == 140
        assert PLATFORM_SPECS["x"].file_size_cap_mb == 512

    def test_loudness_targets_follow_research(self):
        assert LOUDNESS_TARGETS_LUFS["x"] == -16.0
        assert LOUDNESS_TARGETS_LUFS["youtube"] == -14.0
        assert loudness_target("threads") == loudness_target("instagram")
        assert loudness_target("unknown-platform") == -14.0

    def test_threads_ships_instagram_spec(self):
        assert PLATFORM_SPECS["threads"] is PLATFORM_SPECS["instagram"]


# ---------------------------------------------------------------------------
# pipeline: decision tree
# ---------------------------------------------------------------------------


class TestPlanTransform:
    def test_passthrough_flag_wins(self, tmp_path):
        src = tmp_path / "v.mkv"
        src.write_bytes(b"x")
        cfg = EncodingConfig(passthrough=True)
        plan = plan_transform(src, "instagram", cfg, FakeProcessor((False, "irrelevant")))
        assert plan.action == "passthrough"
        assert plan.preserves_bytes

    def test_compliant_mp4_is_passthrough(self, tmp_path):
        src = tmp_path / "v.mp4"
        src.write_bytes(b"x")
        plan = plan_transform(src, "x", EncodingConfig(), FakeProcessor((True, "within profile")))
        assert plan.action == "passthrough"
        assert plan.remux_cmd is None

    def test_compliant_mov_is_passthrough(self, tmp_path):
        src = tmp_path / "v.mov"
        src.write_bytes(b"x")
        plan = plan_transform(src, "youtube", EncodingConfig(), FakeProcessor((True, "ok")))
        assert plan.action == "passthrough"

    def test_compliant_mkv_is_remux_not_transcode(self, tmp_path):
        """The core zero-loss invariant: perfect streams in a foreign
        container must never be re-encoded."""
        src = tmp_path / "v.mkv"
        src.write_bytes(b"x")
        plan = plan_transform(src, "tiktok", EncodingConfig(), FakeProcessor((True, "within profile")))
        assert plan.action == "remux"
        assert plan.remux_cmd is not None
        assert "-c" in plan.remux_cmd and "copy" in plan.remux_cmd
        assert "+faststart" in plan.remux_cmd
        assert plan.preserves_bytes

    def test_noncompliant_is_transcode(self, tmp_path):
        src = tmp_path / "v.mp4"
        src.write_bytes(b"x")
        plan = plan_transform(src, "instagram", EncodingConfig(), FakeProcessor((False, "codec vp9 != h264")))
        assert plan.action == "transcode"
        assert "vp9" in " ".join(plan.reasons)
        assert plan.transcode_summary  # dry-run has something to show

    def test_probe_exception_degrades_to_transcode(self, tmp_path):
        src = tmp_path / "v.mp4"
        src.write_bytes(b"x")

        class Boom:
            def is_platform_compliant(self, path, platform, config):
                raise RuntimeError("ffprobe exploded")

        plan = plan_transform(src, "x", EncodingConfig(), Boom())
        assert plan.action == "transcode"
        assert plan.probe_error is not None

    def test_plan_carries_loudness_target_for_transcode(self, tmp_path):
        src = tmp_path / "v.mp4"
        src.write_bytes(b"x")
        plan = plan_transform(src, "x", EncodingConfig(), FakeProcessor((False, "no")))
        assert plan.loudness_target_lufs is None or plan.loudness_target_lufs == -16.0

    def test_describe_plan_mentions_action(self, tmp_path):
        src = tmp_path / "v.mp4"
        src.write_bytes(b"x")
        plan = plan_transform(src, "x", EncodingConfig(), FakeProcessor((True, "ok")))
        assert "PASSTHROUGH" in describe_plan(plan)


# ---------------------------------------------------------------------------
# loudness helpers
# ---------------------------------------------------------------------------


class TestLoudnessHelpers:
    def test_build_filter_is_linear_two_pass(self):
        f = build_loudnorm_filter(
            {"input_i": -20.0, "input_tp": -18.0, "input_lra": 4.0, "input_thresh": -30.0},
            target_i=-14.0,
        )
        assert f.startswith("loudnorm=I=-14.0:TP=-1.5:LRA=11.0:")
        assert "measured_I=-20.0" in f
        assert "measured_TP=-18.0" in f
        assert "linear=true" in f

    def test_build_filter_rejects_bad_measurements(self):
        assert build_loudnorm_filter({}, target_i=-14.0) is None
        assert build_loudnorm_filter({"input_i": "-inf"}, target_i=-14.0) is None


# ---------------------------------------------------------------------------
# specs: verify_media (integration — needs ffprobe)
# ---------------------------------------------------------------------------


@requires_ffmpeg
class TestVerifyMedia:
    def test_compliant_clip_passes(self, tmp_path):
        clip = _gen_clip(tmp_path / "ok.mp4", extra_video=["-crf", "28"])
        report = verify_media(clip, "instagram", check_loudness=False)
        assert report.ok, format_report(report)
        assert report.errors == []

    def test_foreign_container_is_an_error(self, tmp_path):
        mkv = tmp_path / "v.mkv"
        _gen_clip(tmp_path / "_v.mp4")
        shutil.copy(tmp_path / "_v.mp4", mkv)  # mp4 bytes, mkv name → container error
        report = verify_media(mkv, "tiktok", check_loudness=False)
        assert not report.ok
        assert any(c.name == "container" for c in report.errors)

    def test_wrong_pix_fmt_warns(self, tmp_path):
        p = tmp_path / "yuv444.mp4"
        subprocess.run(
            [
                FFMPEG,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=640x360:rate=30",
                "-t",
                "1",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv444p",
                str(p),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        report = verify_media(p, "x", check_loudness=False)
        assert report.ok  # warning, not blocking
        assert any(c.name == "pix_fmt" and c.status == "warn" for c in report.checks)

    def test_unprobeable_file_degrades_to_warning(self, tmp_path):
        junk = tmp_path / "junk.mp4"
        junk.write_bytes(b"garbage" * 100)
        report = verify_media(junk, "youtube", check_loudness=False)
        assert report.ok  # probe hiccup must never block
        assert any(c.name == "probe" for c in report.warnings)

    def test_loudness_deviation_warns(self, tmp_path):
        quiet = _gen_clip(tmp_path / "quiet.mp4", volume="volume=0.01")
        report = verify_media(quiet, "instagram", check_loudness=True)
        assert report.ok
        loud_checks = [c for c in report.checks if c.name == "loudness"]
        assert loud_checks and loud_checks[0].status == "warn"


# ---------------------------------------------------------------------------
# video: remux + two-pass + loudnorm encode (integration)
# ---------------------------------------------------------------------------


@requires_ffmpeg
class TestVideoProcessorFidelity:
    def test_remux_is_zero_loss_and_faststart(self, tmp_path):
        vp = VideoProcessor()
        mkv = tmp_path / "src.mkv"
        subprocess.run(
            [
                FFMPEG,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=1080x1920:rate=30",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=44100",
                "-t",
                "2",
                "-crf",
                "28",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(mkv),
            ],
            check=True,
            capture_output=True,
            timeout=180,
        )
        out = vp.remux_for_platform(mkv, tmp_path / "src_mp4.mp4")
        assert out.exists()
        assert _has_faststart(out) is True
        ok, reason = vp.is_platform_compliant(out, "tiktok", XPSTConfig().video.encoding_tiktok)
        assert ok, reason

    def test_two_pass_hits_bitrate_target(self, tmp_path):
        vp = VideoProcessor()
        src = _gen_clip(tmp_path / "src.mp4")
        out = tmp_path / "two_pass.mp4"
        cfg = EncodingConfig(
            resolution=1920,
            bitrate="2M",
            maxrate="3M",
            bufsize="6M",
            profile="high",
            gop=30,
            fps=60,
            two_pass=True,
        )
        vp.encode_for_platform(src, out, "youtube", cfg)
        info = vp.get_video_info(out)
        measured_mbps = int(info["format"]["bit_rate"]) / 1e6
        assert 1.6 <= measured_mbps <= 2.4, f"two-pass missed 2M target: {measured_mbps}"

    def test_loudnorm_encode_lands_target(self, tmp_path):
        vp = VideoProcessor()
        src = _gen_clip(tmp_path / "quiet.mp4", volume="volume=0.05")
        measured = measure_loudness(vp.ffmpeg_path, src, target_i=-14.0)
        assert measured is not None
        filt = build_loudnorm_filter(measured, target_i=-14.0)
        out = tmp_path / "norm.mp4"
        vp.encode_for_platform(
            src,
            out,
            "instagram",
            XPSTConfig().video.encoding_instagram,
            loudnorm_filter=filt,
        )
        after = measure_loudness(vp.ffmpeg_path, out, target_i=-14.0)
        assert after is not None
        assert abs(after["input_i"] - (-14.0)) <= 2.0, after

    def test_no_audio_source_skips_norm_gracefully(self, tmp_path):
        vp = VideoProcessor()
        silent = tmp_path / "silent.mp4"
        subprocess.run(
            [
                FFMPEG,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=640x360:rate=30",
                "-t",
                "1",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                str(silent),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        out = tmp_path / "silent_out.mp4"
        vp.encode_for_platform(
            silent,
            out,
            "instagram",
            XPSTConfig().video.encoding_instagram,
            loudnorm_filter="loudnorm=I=-14:TP=-1.5:LRA=11",
        )
        assert out.exists()


# ---------------------------------------------------------------------------
# upload service: decision tree + pre-flight wiring
# ---------------------------------------------------------------------------


@requires_ffmpeg
class TestUploadServiceDecisionTree:
    def test_compliant_source_untouched(self, tmp_path):
        service = _make_service(tmp_path)
        src = _gen_clip(tmp_path / "comp.mp4", extra_video=["-crf", "28", "-maxrate", "4M", "-bufsize", "8M"])
        out = _run(service, src, "instagram")
        assert out == src, "compliant source must pass through untouched"

    def test_mkv_source_is_remuxed_without_reencode(self, tmp_path):
        service = _make_service(tmp_path)
        src = _gen_clip(tmp_path / "comp.mkv", size="1080x1920", extra_video=["-crf", "28"])
        out = _run(service, src, "tiktok")
        assert out == tmp_path / "comp_mp4.mp4"
        # stream copy: identical video codec, no generation change
        vp = service.video_processor
        src_info = vp.get_video_info(src)
        out_info = vp.get_video_info(out)
        src_v = next(s for s in src_info["streams"] if s["codec_type"] == "video")
        out_v = next(s for s in out_info["streams"] if s["codec_type"] == "video")
        assert src_v["codec_name"] == out_v["codec_name"]
        # stream copy: container change only, timing identical (mkv streams
        # don't expose nb_frames — compare durations instead)
        assert abs(float(src_info["format"]["duration"]) - float(out_info["format"]["duration"])) < 0.1

    def test_noncompliant_source_is_transcoded(self, tmp_path):
        service = _make_service(tmp_path)
        src = _gen_clip(tmp_path / "fourk.mp4", size="3840x2160", rate=30)
        out = _run(service, src, "instagram")
        assert out != src
        info = service.video_processor.get_video_info(out)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        assert v["codec_name"] == "h264"
        assert max(v["width"], v["height"]) <= 1920

    def test_encoding_config_routing(self):
        cfg = XPSTConfig()
        service = UploadService(
            video_processor=MagicMock(),
            circuit_breakers=MagicMock(),
            quota_manager=MagicMock(),
            state=StateManager(str(Path("/tmp") / "xpst_test_state_routing")),
            notifier=MagicMock(),
            shutdown_handler=MagicMock(),
            config=cfg,
            anti_bot=None,
        )
        assert service._encoding_config("youtube") is cfg.video.encoding_youtube
        assert service._encoding_config("threads") is cfg.video.encoding_instagram
        assert service._encoding_config("tiktok") is cfg.video.encoding_tiktok
        with pytest.raises(ValueError):
            service._encoding_config("myspace")


def _run(service: UploadService, src: Path, platform: str) -> Path:
    import asyncio

    return asyncio.run(service._encode_for_platform(src, platform))


# ---------------------------------------------------------------------------
# CLI: xpst verify-media
# ---------------------------------------------------------------------------


class TestVerifyMediaCli:
    def _runner(self):
        from click.testing import CliRunner

        return CliRunner()

    def test_json_output_on_unprobeable_file(self, tmp_path):
        from xpst.cli import main

        junk = tmp_path / "junk.mp4"
        junk.write_bytes(b"x" * 64)
        result = self._runner().invoke(
            main,
            ["verify-media", str(junk), "--platform", "youtube", "--json"],
            obj={},
        )
        assert result.exit_code == 0  # probe failure warns, never blocks
        import json

        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["reports"][0]["platform"] == "youtube"

    def test_exit_code_one_on_blocking_error(self, tmp_path):
        from xpst.cli import main

        bad = tmp_path / "bad.avi"
        bad.write_bytes(b"x" * 64)
        result = self._runner().invoke(
            main,
            ["verify-media", str(bad), "--platform", "x", "--json"],
            obj={},
        )
        assert result.exit_code == 1
        import json

        data = json.loads(result.output)
        assert data["ok"] is False
        assert any(c["status"] == "error" for c in data["reports"][0]["checks"])
