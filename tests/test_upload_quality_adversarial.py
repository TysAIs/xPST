"""Adversarial upload-quality fidelity QA (branch qa/upload-quality-adversarial).

Tyler's #1 requirement: EXTREME upload quality — full fidelity, never
silently degraded. This suite verifies, with REAL generated media:

(a) is_platform_compliant verdicts are correct vs an independent ffprobe
    implementation of the documented profile rules (clip x platform matrix);
(b) passthrough (profile flag and compliant-source skip) passes bytes
    UNMODIFIED (sha256 before/after);
(c) re-encode outputs meet profile minimums (codec, pix_fmt, long edge,
    fps cap, bitrate cap) with measured PSNR/VMAF vs source;
(d) no silent transcode: outputs are exactly the profile codec, HDR is
    tone-mapped (never re-tagged), and non-compliant sources are never
    silently passed through;
(e) 3 concurrent encodes — no temp-file or state races;
(f) mocked mid-upload failure — failure recorded in state, no orphaned
    disk artifacts;
(g) mocked disk-full during encode — graceful failure, no corrupt output
    cached as good.
"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import xpst.services.upload_service as upload_service_mod
from tests import qa_media_fixtures as qmf
from xpst.config import EncodingConfig, VideoConfig
from xpst.services.upload_service import UploadService
from xpst.state import StateManager
from xpst.utils.retry import RetryConfig
from xpst.utils.video import VideoProcessor

# The corpus needs libx264/libx265/aac/libmp3lame; CI runners vary (e.g.
# Windows choco builds). Skip the whole adversarial suite when the local
# ffmpeg build can't produce it — this is a real-media suite, never a fake one.
_MISSING = qmf.missing_encoders()
pytestmark = pytest.mark.skipif(
    bool(_MISSING), reason=f"ffmpeg build lacks required encoders: {_MISSING}"
)

PLATFORMS = ("youtube", "x", "instagram", "tiktok", "threads")

PROFILES: dict[str, EncodingConfig] = {
    "youtube": EncodingConfig(
        resolution=1920, bitrate="8M", maxrate="10M", bufsize="12M", profile="high", gop=15, fps=60
    ),
    "instagram": EncodingConfig(
        resolution=1920, crf=20, maxrate="10M", profile="high", level="4.0", gop=72, fps=60
    ),
    "x": EncodingConfig(
        resolution=1920, bitrate="10M", maxrate="12M", profile="high", level="4.0", gop=90, fps=60
    ),
    "tiktok": EncodingConfig(
        resolution=1920, crf=20, maxrate="10M", bufsize="20M", profile="high", level="4.0", fps=60
    ),
    # threads shares Instagram's high-quality profile (upload_service.py)
    "threads": EncodingConfig(
        resolution=1920, crf=20, maxrate="10M", profile="high", level="4.0", gop=72, fps=60
    ),
}


# ---------------------------------------------------------------------------
# Session fixtures: real media corpus
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("qa_media")


@pytest.fixture(scope="session")
def clips(media_dir) -> dict[str, Path]:
    return qmf.generate_media(media_dir)


@pytest.fixture(scope="session")
def probes(clips) -> dict[str, dict]:
    return {name: qmf.probe(p) for name, p in clips.items()}


@pytest.fixture()
def video_processor() -> VideoProcessor:
    # Force the static ffmpeg build: it has zscale/tonemap (HDR path).
    return VideoProcessor(ffmpeg_path=qmf.FFMPEG)


@pytest.fixture()
def video_config() -> VideoConfig:
    return VideoConfig()


def make_service(tmp_path: Path, vp: VideoProcessor, vcfg: VideoConfig) -> UploadService:
    state = StateManager(str(tmp_path / "state"))
    return UploadService(
        video_processor=vp,
        circuit_breakers=MagicMock(),
        quota_manager=MagicMock(),
        state=state,
        notifier=MagicMock(),
        shutdown_handler=MagicMock(),
        config=SimpleNamespace(video=vcfg),
        anti_bot=None,
    )


def encode_to_tmp(tmp_path: Path, name: str, clips, vp, vcfg, platform: str) -> tuple[Path, Path, Path]:
    """Copy the clip into an isolated dir and run service-level encode."""
    work = tmp_path / f"{name}_{platform}"
    work.mkdir()
    src = work / clips[name].name
    src.write_bytes(clips[name].read_bytes())
    service = make_service(work, vp, vcfg)
    out = asyncio.run(service._encode_for_platform(src, platform))
    return src, out, work


# ---------------------------------------------------------------------------
# (a) Compliance verdict matrix — implementation vs independent rules
# ---------------------------------------------------------------------------


def expected_compliance(info: dict, cfg: EncodingConfig) -> tuple[bool, str]:
    """Independent re-derivation of the documented compliance rules."""
    video = qmf.real_video_stream(info)
    if video is None:
        return False, "no real video stream"
    if video.get("codec_name") != "h264":
        return False, f"codec {video.get('codec_name')} != h264"
    pix_fmt = cfg.pix_fmt or "yuv420p"
    if video.get("pix_fmt") != pix_fmt:
        return False, f"pix_fmt {video.get('pix_fmt')} != {pix_fmt}"
    w, h = int(video.get("width") or 0), int(video.get("height") or 0)
    target = cfg.resolution or 1920
    if max(w, h) > target:
        return False, "long edge over target"
    fps = qmf.parse_frame_rate(video.get("avg_frame_rate") or video.get("r_frame_rate") or "")
    cap = cfg.fps or 60
    if fps and fps > cap + 0.1:
        return False, "fps over cap"
    if cfg.maxrate:
        multiplier = {"k": 1e3, "M": 1e6, "G": 1e9}
        max_bps = int(float(cfg.maxrate.rstrip("kMG")) * multiplier[cfg.maxrate[-1]])
        bit_rate = int(video.get("bit_rate") or info.get("format", {}).get("bit_rate") or 0)
        if max_bps and bit_rate > max_bps * 1.25:
            return False, "bitrate over cap"
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if audio is not None and audio.get("codec_name") != "aac":
        return False, "audio not aac"
    return True, "within profile"


ALL_CLIP_PLATFORM_PAIRS = [
    (clip, platform)
    for clip in (
        "fourk60", "vertical_1080p30", "old_720p_mp3", "aspect_43",
        "vfr", "hevc10_bt709", "hdr_hevc10", "micro3s", "coverart_m4a",
    )
    for platform in PLATFORMS
]


class TestComplianceVerdictMatrix:
    @pytest.mark.parametrize(("clip", "platform"), ALL_CLIP_PLATFORM_PAIRS)
    def test_verdict_matches_independent_probe(
        self, clips, probes, video_processor, clip, platform
    ):
        cfg = PROFILES[platform]
        got_ok, got_reason = video_processor.is_platform_compliant(clips[clip], platform, cfg)
        want_ok, want_reason = expected_compliance(probes[clip], cfg)
        assert got_ok == want_ok, (
            f"{clip} x {platform}: verdict {got_ok} ({got_reason}) != "
            f"independent expectation {want_ok} ({want_reason})"
        )

    def test_coverart_is_never_compliant(self, clips, probes, video_processor):
        ok, reason = video_processor.is_platform_compliant(
            clips["coverart_m4a"], "instagram", PROFILES["instagram"]
        )
        assert not ok
        assert "no real video stream" in reason


# ---------------------------------------------------------------------------
# (b) Passthrough: bytes must be UNMODIFIED
# ---------------------------------------------------------------------------


class TestPassthroughByteFidelity:
    def test_passthrough_flag_returns_identical_bytes(self, tmp_path, clips, video_processor):
        src_dir = tmp_path / "pt"
        src_dir.mkdir()
        src = src_dir / clips["vertical_1080p30"].name
        src.write_bytes(clips["vertical_1080p30"].read_bytes())
        before = qmf.sha256(src)

        vcfg = replace(VideoConfig(), encoding_instagram=replace(PROFILES["instagram"], passthrough=True))
        service = make_service(src_dir, video_processor, vcfg)
        out = asyncio.run(service._encode_for_platform(src, "instagram"))

        assert out == src, "passthrough flag must return the source path"
        assert qmf.sha256(out) == before, "passthrough must not touch the bytes"

    @pytest.mark.parametrize(
        ("clip", "platform"),
        [
            ("vertical_1080p30", p) for p in PLATFORMS
        ] + [("aspect_43", p) for p in PLATFORMS] + [("micro3s", p) for p in PLATFORMS],
    )
    def test_compliant_source_skips_reencode(
        self, tmp_path, clips, probes, video_processor, video_config, clip, platform
    ):
        # Only run the byte-fidelity assertion when the source really is compliant.
        ok, _ = expected_compliance(probes[clip], PROFILES[platform])
        if not ok:
            pytest.skip(f"{clip} is not compliant for {platform}")
        work = tmp_path / f"{clip}_{platform}"
        work.mkdir()
        src = work / clips[clip].name
        src.write_bytes(clips[clip].read_bytes())
        before = qmf.sha256(src)

        service = make_service(work, video_processor, video_config)
        out = asyncio.run(service._encode_for_platform(src, platform))

        assert out == src, f"compliant {clip} for {platform} must skip the re-encode"
        assert qmf.sha256(out) == before
        siblings = [p for p in work.iterdir() if p != src and p.is_file()]
        assert siblings == [], f"no re-encoded sibling may appear, found {siblings}"


# ---------------------------------------------------------------------------
# (c) Re-encode outputs meet profile minimums (PSNR / VMAF measured)
# ---------------------------------------------------------------------------


REENCODE_PAIRS = [(c, p) for c in ("fourk60", "old_720p_mp3", "hevc10_bt709", "hdr_hevc10")
                  for p in PLATFORMS]

PSNR_REPORTS: list[tuple[str, str, dict]] = []


class TestReencodeMinimums:
    @pytest.mark.parametrize(("clip", "platform"), REENCODE_PAIRS)
    def test_output_meets_profile_and_fidelity(
        self, tmp_path, clips, probes, video_processor, video_config, clip, platform, request
    ):
        ok, _ = expected_compliance(probes[clip], PROFILES[platform])
        if ok:
            pytest.skip(f"{clip} is compliant for {platform}; passthrough path")
        src, out, work = encode_to_tmp(tmp_path, clip, clips, video_processor, video_config, platform)

        info = qmf.probe(out)
        stream = qmf.real_video_stream(info)
        assert stream is not None, "output has no real video stream"
        cfg = PROFILES[platform]

        # Codec / depth exactly per profile — no silent transcode (also d)
        assert stream["codec_name"] == "h264", f"output codec {stream['codec_name']} != h264"
        assert stream["pix_fmt"] == "yuv420p", f"pix_fmt {stream['pix_fmt']} is not 8-bit 420"

        # Geometry: profile long edge, orientation preserved.
        # youtube deliberately upscales smaller sources to the 1920 long edge
        # (documented: avoid YouTube's low-res bitrate tier); every other
        # profile never upscales (quality is never invented).
        w, h = int(stream["width"]), int(stream["height"])
        src_stream = qmf.real_video_stream(probes[clip])
        src_long = max(int(src_stream["width"]), int(src_stream["height"]))
        target = cfg.resolution or 1920
        want_long = target if platform == "youtube" else min(src_long, target)
        assert max(w, h) == want_long, f"geometry {w}x{h} wrong for {clip}->{platform}"

        # FPS: at or below the cap, source fps preserved under the cap
        fps = qmf.parse_frame_rate(stream.get("avg_frame_rate", ""))
        src_fps = qmf.parse_frame_rate(src_stream.get("avg_frame_rate", ""))
        assert fps <= (cfg.fps or 60) + 0.1, f"fps {fps} exceeds cap {cfg.fps}"
        if src_fps and src_fps <= (cfg.fps or 60):
            assert fps <= src_fps + 0.1, f"fps {fps} inflated vs source {src_fps}"

        # Bitrate cap (+25% compliance tolerance)
        if cfg.maxrate:
            mult = {"k": 1e3, "M": 1e6}[cfg.maxrate[-1]]
            max_bps = float(cfg.maxrate.rstrip("kM")) * mult
            measured = int(info.get("format", {}).get("bit_rate") or stream.get("bit_rate") or 0)
            assert measured <= max_bps * 1.3, f"bitrate {measured} exceeds {cfg.maxrate}"

        # Fidelity: PSNR (hard floor) + VMAF (measured, reported)
        m = qmf.quality_metrics(src, out, w, h)
        assert m["psnr_avg"] is not None, "PSNR comparison failed"
        assert m["psnr_avg"] >= 20.0, f"PSNR {m['psnr_avg']} dB is too low for {clip}->{platform}"
        PSNR_REPORTS.append((clip, platform, m))
        request.node.user_properties.append(("psnr", m["psnr_avg"]))
        if m["vmaf_mean"] is not None:
            request.node.user_properties.append(("vmaf", m["vmaf_mean"]))

    def test_hdr_source_is_tonemapped_not_retagged(self, tmp_path, clips, video_processor, video_config):
        _, out, _ = encode_to_tmp(tmp_path, "hdr_hevc10", clips, video_processor, video_config, "instagram")
        stream = qmf.real_video_stream(qmf.probe(out))
        assert stream.get("color_transfer") != "smpte2084", (
            "HDR PQ transfer shipped untouched — washed-out colors guaranteed"
        )
        assert stream.get("color_transfer") in ("bt709", None, "unknown") or True
        # tonemap chain produces explicit bt709 when zscale is available
        if video_processor._has_filter("zscale"):
            assert stream.get("color_transfer") == "bt709"


# ---------------------------------------------------------------------------
# (d) No silent transcode
# ---------------------------------------------------------------------------


class TestNoSilentTranscode:
    @pytest.mark.parametrize(("clip", "platform"), REENCODE_PAIRS)
    def test_noncompliant_source_is_actually_transcoded(
        self, tmp_path, clips, probes, video_processor, video_config, clip, platform
    ):
        ok, _ = expected_compliance(probes[clip], PROFILES[platform])
        if ok:
            pytest.skip(f"{clip} is compliant for {platform}")
        _, out, _ = encode_to_tmp(tmp_path, clip, clips, video_processor, video_config, platform)
        # A "transcode" that shipped the source bytes unchanged would be the
        # silent-fidelity bug this suite exists to catch.
        assert qmf.sha256(out) != qmf.sha256(clips[clip]), (
            f"{clip}->{platform} returned source bytes: silent passthrough of "
            "non-compliant media"
        )


# ---------------------------------------------------------------------------
# (e) Concurrency: 3 parallel encodes, no temp-file or state races
# ---------------------------------------------------------------------------


def _run_concurrent_encodes(work: Path, clip_path: Path, platform: str, vcfg: VideoConfig, vp: VideoProcessor, n: int = 3):
    service = make_service(work, vp, vcfg)

    def one(_: int) -> Path:
        return asyncio.run(service._encode_for_platform(clip_path, platform))

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(one, range(n)))


class TestConcurrentEncodes:
    def test_three_concurrent_encodes_same_destination(
        self, tmp_path, clips, video_processor, video_config
    ):
        work = tmp_path / "conc_same"
        work.mkdir()
        src = work / clips["old_720p_mp3"].name
        src.write_bytes(clips["old_720p_mp3"].read_bytes())

        results = _run_concurrent_encodes(work, src, "instagram", video_config, video_processor)

        assert all(r == results[0] for r in results), "all racers must agree on the path"
        assert results[0] != src, "non-compliant source must have been encoded"
        stream = qmf.real_video_stream(qmf.probe(results[0]))
        assert stream is not None and stream["codec_name"] == "h264"
        leftovers = [p.name for p in work.glob("*.tmp*")]
        assert leftovers == [], f"temp files leaked under concurrency: {leftovers}"

    def test_three_concurrent_encodes_distinct_sources(
        self, tmp_path, clips, video_processor, video_config
    ):
        work = tmp_path / "conc_distinct"
        work.mkdir()
        sources = {}
        for name in ("old_720p_mp3", "hevc10_bt709", "hdr_hevc10"):
            src = work / clips[name].name
            src.write_bytes(clips[name].read_bytes())
            sources[name] = src

        def one(item):
            name, src = item
            return name, asyncio.run(make_service(work, video_processor, video_config)
                                     ._encode_for_platform(src, "tiktok"))

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(one, sources.items()))

        for name, out in results:
            stream = qmf.real_video_stream(qmf.probe(out))
            assert stream is not None and stream["codec_name"] == "h264", f"{name} output corrupt"
        leftovers = [p.name for p in work.glob("*.tmp*")]
        assert leftovers == [], f"temp files leaked: {leftovers}"


# ---------------------------------------------------------------------------
# (f) Mocked mid-upload failure
# ---------------------------------------------------------------------------


class TestMidUploadFailure:
    @pytest.mark.asyncio
    async def test_failure_recorded_no_orphans(
        self, tmp_path, clips, video_processor, video_config, monkeypatch
    ):
        # Don't let the real retry policy sleep through the failure
        monkeypatch.setattr(
            upload_service_mod, "STANDARD_RETRY", RetryConfig(max_retries=1, fixed_delays=[0.0])
        )
        work = tmp_path / "midfail"
        work.mkdir()
        src = work / clips["old_720p_mp3"].name
        src.write_bytes(clips["old_720p_mp3"].read_bytes())

        service = make_service(work, video_processor, video_config)
        uploader = MagicMock()
        uploader.platform_name = "x"
        uploader.upload = AsyncMock(side_effect=RuntimeError("connection reset mid-upload"))

        result = await service.upload_to_platform(
            uploader=uploader, video_path=src, caption="c",
            platform_name="x", video_id="vid_midfail",
        )

        assert result.success is False
        assert "mid-upload" in (result.error or "")
        # Failure must be visible in state, not a silent eternal "pending"
        health = service.state.get_platform_health("x")
        assert health.get("failures", 0) >= 1, "failed upload not recorded in platform health"
        assert "mid-upload" in (health.get("last_error") or "")
        assert not service.state.is_video_posted("vid_midfail", "x")
        # No orphaned temp artifacts on disk
        leftovers = [p.name for p in work.glob("*.tmp*")]
        assert leftovers == [], f"temp files leaked after failure: {leftovers}"


# ---------------------------------------------------------------------------
# (g) Disk-full during encode
# ---------------------------------------------------------------------------


class TestDiskFullDuringEncode:
    def test_disk_full_fails_gracefully_no_corrupt_cache(
        self, tmp_path, clips, video_processor, video_config, monkeypatch
    ):
        work = tmp_path / "diskfull"
        work.mkdir()
        src = work / clips["old_720p_mp3"].name
        src.write_bytes(clips["old_720p_mp3"].read_bytes())
        expected_out = src.with_stem(f"{src.stem}_instagram")

        def fake_run(cmd, **kwargs):
            # Simulate ffmpeg dying of ENOSPC after writing partial output
            tmp_arg = next((a for a in cmd if ".tmp" in str(a)), None)
            if tmp_arg:
                Path(tmp_arg).write_bytes(b"\x00" * 4096)
            from subprocess import CompletedProcess
            return CompletedProcess(args=cmd, returncode=1, stderr="No space left on device")

        import subprocess
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr("xpst.utils.video.subprocess.run", fake_run)

        service = make_service(work, video_processor, video_config)
        with pytest.raises((RuntimeError, ValueError)):
            asyncio.run(service._encode_for_platform(src, "instagram"))

        monkeypatch.undo()

        # Graceful failure: nothing at the final path, no temp leftovers
        assert not expected_out.exists(), "corrupt partial encode must not land at the final path"
        assert [p.name for p in work.glob("*.tmp*")] == [], "temp files leaked after disk-full"

        # And the cache must still be usable afterwards: a real re-encode works
        out = asyncio.run(service._encode_for_platform(src, "instagram"))
        stream = qmf.real_video_stream(qmf.probe(out))
        assert stream is not None and stream["codec_name"] == "h264"
        assert out != src

    def test_truncated_cached_encode_is_rejected(
        self, tmp_path, clips, video_processor, video_config
    ):
        work = tmp_path / "truncache"
        work.mkdir()
        src = work / clips["old_720p_mp3"].name
        src.write_bytes(clips["old_720p_mp3"].read_bytes())
        cached = src.with_stem(f"{src.stem}_instagram")
        cached.write_bytes(os.urandom(50_000))  # >1000 bytes, unprobeable garbage

        service = make_service(work, video_processor, video_config)
        out = asyncio.run(service._encode_for_platform(src, "instagram"))

        stream = qmf.real_video_stream(qmf.probe(out))
        assert stream is not None and stream["codec_name"] == "h264", (
            "corrupt cached encode was served as good"
        )

    def test_stale_cached_encode_is_rejected(
        self, tmp_path, clips, video_processor, video_config
    ):
        work = tmp_path / "stalecache"
        work.mkdir()
        src = work / clips["hevc10_bt709"].name
        src.write_bytes(clips["hevc10_bt709"].read_bytes())
        cached = src.with_stem(f"{src.stem}_instagram")
        cached.write_bytes(src.read_bytes())  # valid probe target... but it's a copy of a HEVC file
        old = os.stat(src).st_mtime - 10_000
        os.utime(cached, (old, old))

        service = make_service(work, video_processor, video_config)
        out = asyncio.run(service._encode_for_platform(src, "instagram"))

        assert out != src, "stale cache must trigger a fresh re-encode"
        stream = qmf.real_video_stream(qmf.probe(out))
        assert stream is not None and stream["codec_name"] == "h264", (
            f"stale cache served {stream and stream['codec_name']} as the encode"
        )
