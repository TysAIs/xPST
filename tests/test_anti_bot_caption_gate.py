"""Caption variation is opt-in (defect: every post was silently mutated).

``AntiBotProtection.vary_caption`` appended engagement-bait suffixes
("🎬 Subscribe for more!", "#Reels #ReelsInstagram") to EVERY video/image
upload, with no config gate and no consent. The operator's copy is theirs: a
mutation that the user never asked for is not anti-bot hardening, it is a
product defect.
"""

from pathlib import Path

from xpst.config import XPSTConfig
from xpst.services.upload_service import UploadService


class _SpyAntiBot:
    """Records calls so the test asserts on real behaviour, not mock wiring."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def vary_caption(self, caption: str, platform: str, username: str = "") -> str:
        self.calls.append((caption, platform))
        return f"{caption} 🎬 Subscribe for more!"


def _service(tmp_path: Path, *, caption_variation: bool) -> tuple[UploadService, _SpyAntiBot]:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.anti_bot.caption_variation = caption_variation
    spy = _SpyAntiBot()
    service = UploadService(
        None,  # video_processor
        None,  # circuit_breakers
        None,  # quota_manager
        None,  # state
        None,  # notifier
        None,  # shutdown_handler
        config,
        spy,
    )
    return service, spy


def test_caption_is_untouched_by_default(tmp_path: Path) -> None:
    service, spy = _service(tmp_path, caption_variation=False)

    assert service._caption_for_destination("my own words", "youtube") == "my own words"
    assert spy.calls == [], "the operator's caption was mutated without consent"


def test_caption_variation_is_opt_in(tmp_path: Path) -> None:
    service, spy = _service(tmp_path, caption_variation=True)

    assert service._caption_for_destination("my own words", "youtube") == (
        "my own words 🎬 Subscribe for more!"
    )
    assert spy.calls == [("my own words", "youtube")]


def test_config_defaults_caption_variation_off() -> None:
    assert XPSTConfig().anti_bot.caption_variation is False


def test_config_file_can_opt_in(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("anti_bot:\n  caption_variation: true\n", encoding="utf-8")

    loaded = XPSTConfig.load(str(config_path))

    assert loaded.anti_bot.caption_variation is True
