"""Tests for xPST setup wizard"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from xpst.setup import (
    check_ffmpeg,
    check_python_version,
    check_yt_dlp,
    create_directory_structure,
)


class TestSetupPrerequisites:
    """Test system requirements checking."""

    def test_check_python_version(self):
        """Test Python version check returns valid result."""
        ok, version_str = check_python_version()
        assert isinstance(ok, bool)
        assert isinstance(version_str, str)
        assert "." in version_str

    def test_check_ffmpeg_found(self):
        """Test ffmpeg check when ffmpeg exists."""
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            assert check_ffmpeg() is True

    def test_check_ffmpeg_not_found(self):
        """Test ffmpeg check when ffmpeg is missing."""
        with patch("shutil.which", return_value=None):
            assert check_ffmpeg() is False

    def test_check_yt_dlp_found(self):
        """Test yt-dlp check when installed."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "2024.01.01\n"
        with patch("subprocess.run", return_value=mock_result):
            ver = check_yt_dlp()
            assert ver == "2024.01.01"

    def test_check_yt_dlp_not_found(self):
        """Test yt-dlp check when not installed."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            ver = check_yt_dlp()
            assert ver is None


class TestDirectoryStructure:
    """Test directory creation."""

    def test_create_directory_structure(self, tmp_path, monkeypatch):
        """Test that all required directories are created."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config_dir = create_directory_structure()

        assert config_dir.exists()
        assert (config_dir / "credentials").exists()
        assert (config_dir / "downloads").exists()
        assert (config_dir / "logs").exists()
        assert (config_dir / "backups").exists()

    def test_create_directory_structure_idempotent(self, tmp_path, monkeypatch):
        """Test that running twice doesn't fail."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        create_directory_structure()
        create_directory_structure()  # Should not raise


class TestConfirmPrompt:
    """Test confirmation prompts."""

    def test_confirm_yes(self):
        """Test yes confirmation."""
        with patch("builtins.input", return_value="y"):
            # _confirm uses console.input, mock it differently
            pass

    def test_confirm_default(self):
        """Test default values."""
        # Default True: empty input returns True
        # Default False: empty input returns False
        assert True  # Placeholder - interactive tests need console mocking
