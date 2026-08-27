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
        """Test ffmpeg check when ffmpeg is missing (PATH + known locations)."""
        with patch("shutil.which", return_value=None), \
             patch("pathlib.Path.home", return_value=Path("/nonexistent")), \
             patch.object(Path, "is_file", return_value=False):
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


class TestLegacyPipedInputSafety:
    """The legacy setup wizard must never crash with EOFError on pipes.

    The polished wizard.py replaced what used to crash with EOFError when
    driven over closed/piped stdin. These guards keep `xpst setup` (kept for
    compatibility) safe under automation without changing interactive prompts.
    """

    def test_confirm_returns_default_on_eoferror_pipe(self, monkeypatch):
        """_confirm degrades to its default when stdin closes (EOF)."""
        from xpst import setup

        def _eof(*args, **kwargs):
            raise EOFError

        monkeypatch.setattr(setup.console, "input", _eof)
        assert setup._confirm("Continue?", default=True) is True
        assert setup._confirm("Skip?", default=False) is False

    def test_prompt_tiktok_username_returns_empty_on_eoferror_pipe(self, monkeypatch):
        """prompt_tiktok_username returns '' (no crash) on a closed pipe."""
        from xpst import setup

        def _eof(*args, **kwargs):
            raise EOFError

        monkeypatch.setattr(setup.console, "input", _eof)
        assert setup.prompt_tiktok_username() == ""

    def test_console_input_catches_eoferror(self, monkeypatch):
        """The guard helper itself swallows EOFError and returns ''."""
        from xpst import setup

        def _eof(*args, **kwargs):
            raise EOFError

        monkeypatch.setattr(setup.console, "input", _eof)
        assert setup._console_input("prompt> ") == ""

    def test_console_input_passthrough(self, monkeypatch):
        """Interactive input is passed through unchanged."""
        from xpst import setup

        monkeypatch.setattr(setup.console, "input", lambda prompt: "hello")
        assert setup._console_input("prompt> ") == "hello"
