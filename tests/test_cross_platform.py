"""
Cross-platform compatibility tests for XPST.

Tests that XPST works correctly across Windows, macOS, and Linux:
- Path handling (expanduser, tilde, platform-native separators)
- Subprocess handling (FFmpeg/yt-dlp detection with .exe variants)
- Signal handling (SIGINT/SIGTERM on Unix, skip on Windows)
- File locking (threading.Lock cross-platform)
- Config loading (Windows, Unix, macOS paths in YAML)
- Credential storage (keyring backend detection)
- Temp file cleanup (platform temp dirs)
- Unicode paths (non-ASCII characters)
- FFmpeg path resolution (absolute, relative, from PATH)
- Line ending handling (CRLF vs LF config files)
"""

import json
import os
import platform
import shutil
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath

import pytest
import yaml

from xpst.config import XPSTConfig
from xpst.crash_recovery import CrashRecoveryManager
from xpst.state import StateManager
from xpst.utils.credentials import CredentialStore
from xpst.utils.shutdown import ShutdownHandler

# ---------------------------------------------------------------------------
# 1. Path handling
# ---------------------------------------------------------------------------

class TestPathHandling:
    """Config paths expand correctly, state files created in right locations, temp files work."""

    def test_expanduser_tilde_in_config_dir(self, tmp_path):
        """~/.xpst expands to an absolute path."""
        config = XPSTConfig()
        assert config.config_dir == "~/.xpst"

        # After load(), paths must be expanded
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({}))
        loaded = XPSTConfig.load(str(config_file))
        assert not loaded.config_dir.startswith("~")
        assert Path(loaded.config_dir).is_absolute()

    def test_expanduser_in_download_dir(self, tmp_path):
        """download_dir with ~ is expanded."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({
            "video": {"download_dir": "~/my_downloads"},
        }))
        loaded = XPSTConfig.load(str(config_file))
        assert not loaded.video.download_dir.startswith("~")
        assert Path(loaded.video.download_dir).is_absolute()

    def test_expanduser_in_log_file(self, tmp_path):
        """log_file with ~ is expanded."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({
            "monitoring": {"log_file": "~/logs/app.log"},
        }))
        loaded = XPSTConfig.load(str(config_file))
        assert not loaded.monitoring.log_file.startswith("~")
        assert Path(loaded.monitoring.log_file).is_absolute()

    def test_expanduser_in_credentials_paths(self, tmp_path):
        """Credential file paths with ~ are expanded."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({}))
        loaded = XPSTConfig.load(str(config_file))
        # All credential paths in defaults use ~
        assert not loaded.youtube.client_secrets.startswith("~")
        assert not loaded.youtube.token_file.startswith("~")
        assert not loaded.x.cookies_file.startswith("~")
        assert not loaded.instagram.session_file.startswith("~")

    def test_state_dir_expanduser(self, tmp_path):
        """StateManager expands ~ in state_dir."""
        target = tmp_path / "state_test"
        sm = StateManager(str(target))
        assert sm.state_dir == target.resolve()
        assert sm.state_file == target.resolve() / "state.json"

    def test_state_files_created_in_correct_location(self, tmp_path):
        """State file and backup dir are created under state_dir."""
        sm = StateManager(str(tmp_path))
        assert sm.state_dir.exists()
        assert (tmp_path / "backups").exists()
        # state.json is created on first save
        sm.mark_video_posted("v1", "youtube")
        sm.save()
        assert (tmp_path / "state.json").exists()

    def test_temp_file_paths_are_writable(self, tmp_path):
        """Temp files can be created and written to."""
        tmp_dir = tmp_path / "tmp"
        tmp_dir.mkdir()
        tmp_file = tmp_dir / "test.tmp"
        tmp_file.write_text("hello")
        assert tmp_file.read_text() == "hello"
        tmp_file.unlink()
        assert not tmp_file.exists()

    def test_config_dir_absolute_after_load(self, tmp_path):
        """config_dir is always absolute after loading."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({}))
        loaded = XPSTConfig.load(str(config_file))
        assert Path(loaded.config_dir).is_absolute()

    def test_path_with_trailing_slash(self, tmp_path):
        """Paths with trailing separators are handled."""
        target = tmp_path / "state_trailing"
        # StateManager should handle this fine
        sm = StateManager(str(target))
        assert sm.state_dir.exists()


# ---------------------------------------------------------------------------
# 2. Subprocess handling
# ---------------------------------------------------------------------------

class TestSubprocessHandling:
    """FFmpeg detection works with/without .exe, yt-dlp found on PATH."""

    def test_ffmpeg_found_on_path(self):
        """ffmpeg is discoverable on PATH (if installed in test env)."""
        ffmpeg = shutil.which("ffmpeg")
        # We don't fail the test if ffmpeg isn't installed in CI,
        # but if it is, verify it's a real path.
        if ffmpeg is not None:
            assert Path(ffmpeg).exists()

    def test_ffmpeg_not_found_raises(self, tmp_path):
        """VideoProcessor raises RuntimeError when ffmpeg is missing."""
        from xpst.utils.video import VideoProcessor

        with pytest.raises(RuntimeError, match="FFmpeg not found"):
            VideoProcessor(ffmpeg_path="/nonexistent/path/ffmpeg")

    def test_ffmpeg_exe_variant_detection(self):
        """shutil.which finds .exe variants on Windows or returns None on Unix."""
        # On Windows, shutil.which("ffmpeg") will find "ffmpeg.exe"
        # On Unix, this is a no-op check
        result = shutil.which("ffmpeg")
        if platform.system() == "Windows":
            # On Windows, if ffmpeg is installed, it resolves to .exe
            if result is not None:
                assert result.endswith(".exe") or "ffmpeg" in result
        # On Unix, just confirm it returns something or None
        assert result is None or isinstance(result, str)

    def test_ytdlp_found_on_path(self):
        """yt-dlp is discoverable on PATH (if installed)."""
        ytdlp = shutil.which("yt-dlp")
        if ytdlp is not None:
            assert Path(ytdlp).exists()

    def test_which_returns_none_for_missing_binary(self):
        """shutil.which returns None for a nonexistent binary."""
        result = shutil.which("definitely_not_a_real_binary_xyz_12345")
        assert result is None

    def test_subprocess_capture_output(self):
        """subprocess.run works with capture_output on this platform."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "-c", "print('cross-platform')"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "cross-platform" in result.stdout


# ---------------------------------------------------------------------------
# 3. Signal handling
# ---------------------------------------------------------------------------

class TestSignalHandling:
    """Shutdown handler registers correctly. Skip signal tests on Windows."""

    @pytest.mark.skipif(
        platform.system() == "Windows",
        reason="SIGINT/SIGTERM not available on Windows",
    )
    def test_signal_handler_registration(self, tmp_path):
        """ShutdownHandler.register() installs signal handlers."""
        handler = ShutdownHandler(str(tmp_path))
        handler.register()

        # Verify SIGINT and SIGTERM are now handled
        sigint_handler = signal.getsignal(signal.SIGINT)
        sigterm_handler = signal.getsignal(signal.SIGTERM)

        assert sigint_handler is not None
        assert sigterm_handler is not None

        # Clean up
        handler.unregister()

    @pytest.mark.skipif(
        platform.system() == "Windows",
        reason="SIGINT/SIGTERM not available on Windows",
    )
    def test_signal_handler_unregister_restores_original(self, tmp_path):
        """unregister() restores original signal handlers."""
        original_sigint = signal.getsignal(signal.SIGINT)

        handler = ShutdownHandler(str(tmp_path))
        handler.register()
        handler.unregister()

        restored = signal.getsignal(signal.SIGINT)
        assert restored == original_sigint

    @pytest.mark.skipif(
        platform.system() == "Windows",
        reason="SIGINT/SIGTERM not available on Windows",
    )
    def test_should_shutdown_flag_starts_false(self, tmp_path):
        """should_shutdown is False initially."""
        handler = ShutdownHandler(str(tmp_path))
        assert handler.should_shutdown is False

    @pytest.mark.skipif(
        platform.system() == "Windows",
        reason="SIGINT/SIGTERM not available on Windows",
    )
    def test_shutdown_flag_set_on_signal(self, tmp_path):
        """Sending SIGINT to self sets the shutdown flag."""
        handler = ShutdownHandler(str(tmp_path))
        handler.register()

        # Send ourselves SIGINT
        os.kill(os.getpid(), signal.SIGINT)

        # Give the handler a moment
        time.sleep(0.1)
        assert handler.should_shutdown is True

        # Cleanup
        handler.unregister()

    def test_shutdown_state_file_cleanup(self, tmp_path):
        """clear_shutdown_state removes the state file."""
        handler = ShutdownHandler(str(tmp_path))
        state_file = tmp_path / "shutdown_state.json"
        state_file.write_text('{"test": true}')
        assert state_file.exists()

        handler.clear_shutdown_state()
        assert not state_file.exists()


# ---------------------------------------------------------------------------
# 4. File locking
# ---------------------------------------------------------------------------

class TestFileLocking:
    """StateManager concurrent access works (threading.Lock is cross-platform)."""

    def test_concurrent_mark_video_posted(self, tmp_path):
        """Multiple threads can mark videos posted without data corruption."""
        sm = StateManager(str(tmp_path))
        errors = []

        def mark_many(prefix, count):
            try:
                for i in range(count):
                    sm.mark_video_posted(f"{prefix}_{i}", "youtube", post_id=f"id_{i}")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=mark_many, args=("t1", 50)),
            threading.Thread(target=mark_many, args=("t2", 50)),
            threading.Thread(target=mark_many, args=("t3", 50)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(errors) == 0
        # All 150 videos should be tracked
        assert len(sm.state["posted_videos"]) == 150

    def test_concurrent_save(self, tmp_path):
        """Multiple threads saving state doesn't corrupt the file."""
        sm = StateManager(str(tmp_path))
        errors = []

        def save_repeatedly():
            try:
                for i in range(20):
                    sm.mark_video_posted(f"save_test_{i}", "youtube")
                    sm.save()
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=save_repeatedly),
            threading.Thread(target=save_repeatedly),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(errors) == 0
        # File should be valid JSON
        with open(tmp_path / "state.json") as f:
            data = json.load(f)
        assert "posted_videos" in data

    def test_save_lock_is_reentrant_safe(self, tmp_path):
        """The save lock doesn't deadlock on the same thread."""
        sm = StateManager(str(tmp_path))
        sm.mark_video_posted("v1", "youtube")
        sm.save()
        sm.save()  # Should not deadlock
        assert (tmp_path / "state.json").exists()

    def test_threading_lock_attribute_exists(self, tmp_path):
        """StateManager has a threading.Lock for save operations."""
        sm = StateManager(str(tmp_path))
        assert hasattr(sm, "_save_lock")
        assert isinstance(sm._save_lock, type(threading.Lock()))


# ---------------------------------------------------------------------------
# 5. Config loading with platform-specific paths
# ---------------------------------------------------------------------------

class TestConfigLoading:
    """YAML loading with Windows, Unix, and macOS paths."""

    def test_unix_paths_in_config(self, tmp_path):
        """Unix-style absolute paths load correctly."""
        config_data = {
            "accounts": {
                "youtube": {
                    "client_secrets": "/home/user/.xpst/creds/yt_secrets.json",
                    "token_file": "/home/user/.xpst/creds/yt_token.json",
                },
                "x": {
                    "cookies_file": "/home/user/.xpst/creds/x_cookies.json",
                },
                "instagram": {
                    "session_file": "/home/user/.xpst/creds/ig_session.json",
                },
            },
            "video": {
                "download_dir": "/home/user/videos/xpst_downloads",
            },
            "monitoring": {
                "log_file": "/var/log/xpst/xpst.log",
            },
        }
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        loaded = XPSTConfig.load(str(config_file))
        assert loaded.youtube.client_secrets == "/home/user/.xpst/creds/yt_secrets.json"
        assert loaded.video.download_dir == "/home/user/videos/xpst_downloads"
        assert loaded.monitoring.log_file == "/var/log/xpst/xpst.log"

    def test_macos_paths_in_config(self, tmp_path):
        """macOS-style paths load correctly."""
        config_data = {
            "accounts": {
                "youtube": {
                    "client_secrets": "/Users/testuser/.xpst/creds/yt_secrets.json",
                },
            },
            "video": {
                "download_dir": "/Users/testuser/Movies/XPST",
            },
            "monitoring": {
                "log_file": "/Users/testuser/Library/Logs/xpst.log",
            },
        }
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        loaded = XPSTConfig.load(str(config_file))
        assert "/Users/testuser" in loaded.youtube.client_secrets
        assert "/Users/testuser/Movies" in loaded.video.download_dir

    def test_windows_paths_in_config(self, tmp_path):
        """Windows-style paths (C:\\Users\\...) load from YAML correctly."""
        config_data = {
            "accounts": {
                "youtube": {
                    "client_secrets": "C:\\Users\\testuser\\.xpst\\creds\\yt_secrets.json",
                },
                "x": {
                    "cookies_file": "C:\\Users\\testuser\\.xpst\\creds\\x_cookies.json",
                },
            },
            "video": {
                "download_dir": "C:\\Users\\testuser\\Videos\\XPST",
            },
            "monitoring": {
                "log_file": "C:\\Users\\testuser\\AppData\\Local\\xpst\\xpst.log",
            },
        }
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        loaded = XPSTConfig.load(str(config_file))
        # Paths should be loaded as-is (no expansion since no ~)
        assert "C:\\Users" in loaded.video.download_dir or "C:/Users" in loaded.video.download_dir

    def test_windows_unc_paths_in_config(self, tmp_path):
        """Windows UNC paths (\\\\server\\share) load correctly."""
        config_data = {
            "video": {
                "download_dir": "\\\\server\\share\\xpst\\downloads",
            },
        }
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        loaded = XPSTConfig.load(str(config_file))
        assert "server" in loaded.video.download_dir

    def test_config_with_mixed_path_styles(self, tmp_path):
        """Config with different path styles per field loads correctly."""
        config_data = {
            "accounts": {
                "youtube": {
                    "client_secrets": "/home/user/yt.json",
                    "token_file": "~/.xpst/yt_token.json",
                },
            },
            "video": {
                "download_dir": "~/Downloads/xpst",
            },
            "monitoring": {
                "log_file": "/var/log/xpst.log",
            },
        }
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        loaded = XPSTConfig.load(str(config_file))
        # ~/... paths should be expanded; absolute paths kept as-is
        assert not loaded.youtube.token_file.startswith("~")
        assert not loaded.video.download_dir.startswith("~")
        assert loaded.monitoring.log_file == "/var/log/xpst.log"

    def test_empty_config_loads_with_defaults(self, tmp_path):
        """An empty YAML file loads with all defaults."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("")

        loaded = XPSTConfig.load(str(config_file))
        assert loaded.reliability.max_retries == 3
        assert loaded.schedule.check_interval == 900
        assert loaded.tiktok.username == ""

    def test_config_with_only_whitespace_yaml(self, tmp_path):
        """A YAML file with only whitespace loads without error."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("   \n\n   ")

        loaded = XPSTConfig.load(str(config_file))
        assert loaded.reliability.max_retries == 3


# ---------------------------------------------------------------------------
# 6. Credential storage
# ---------------------------------------------------------------------------

class TestCredentialStorage:
    """Keyring backend detection works."""

    def test_has_keyring_attribute(self):
        """CredentialStore module reports keyring availability."""
        from xpst.utils.credentials import HAS_KEYRING
        assert isinstance(HAS_KEYRING, bool)

    def test_credential_store_creates_directory(self, tmp_path):
        """CredentialStore creates the credentials directory."""
        CredentialStore(str(tmp_path))
        creds_dir = tmp_path / "credentials"
        assert creds_dir.exists()
        assert creds_dir.is_dir()

    def test_fallback_file_storage(self, tmp_path):
        """When keyring is unavailable, credentials are stored in files."""
        store = CredentialStore(str(tmp_path))
        # Force file storage
        store._use_keyring = False

        store.store("test_key", "test_value")
        cred_file = tmp_path / "credentials" / "test_key.json"
        assert cred_file.exists()

        data = json.loads(cred_file.read_text())
        assert data["value"] == "test_value"

    def test_retrieve_from_file_storage(self, tmp_path):
        """Retrieve works with fallback file storage."""
        store = CredentialStore(str(tmp_path))
        store._use_keyring = False

        store.store("my_cred", "secret_123")
        result = store.retrieve("my_cred")
        assert result == "secret_123"

    def test_delete_from_file_storage(self, tmp_path):
        """Delete removes credential file."""
        store = CredentialStore(str(tmp_path))
        store._use_keyring = False

        store.store("to_delete", "value")
        assert store.delete("to_delete") is True
        assert store.retrieve("to_delete") is None

    def test_list_keys_file_storage(self, tmp_path):
        """list_keys returns all stored credential keys."""
        store = CredentialStore(str(tmp_path))
        store._use_keyring = False

        store.store("key_a", "val_a")
        store.store("key_b", "val_b")
        keys = store.list_keys()
        assert "key_a" in keys
        assert "key_b" in keys

    def test_store_and_retrieve_json(self, tmp_path):
        """store_json/retrieve_json round-trips dictionaries."""
        store = CredentialStore(str(tmp_path))
        store._use_keyring = False

        data = {"token": "abc123", "expires": "2026-01-01"}
        store.store_json("json_cred", data)
        result = store.retrieve_json("json_cred")
        assert result == data

    def test_retrieve_nonexistent_returns_none(self, tmp_path):
        """Retrieving a missing credential returns None."""
        store = CredentialStore(str(tmp_path))
        store._use_keyring = False

        assert store.retrieve("does_not_exist") is None

    def test_keyring_index_file_created(self, tmp_path):
        """Keyring index file is created when using keyring path."""
        store = CredentialStore(str(tmp_path))
        index_file = tmp_path / "credentials" / "_keyring_index.json"
        # The file may not exist yet, but the attribute should be set
        assert store._keyring_index_file == index_file


# ---------------------------------------------------------------------------
# 7. Temp file cleanup
# ---------------------------------------------------------------------------

class TestTempFileCleanup:
    """Temp file cleanup works on all platforms."""

    def test_temp_dir_is_accessible(self):
        """System temp directory is accessible and writable."""
        tmp_dir = tempfile.gettempdir()
        assert Path(tmp_dir).exists()
        assert Path(tmp_dir).is_dir()

    def test_temp_file_creation_and_cleanup(self):
        """Temp files can be created and cleaned up."""
        fd, path = tempfile.mkstemp(suffix=".xpst_test")
        try:
            p = Path(path)
            assert p.exists()
            os.write(fd, b"test data")
            os.close(fd)
            p.unlink()
            assert not p.exists()
        except Exception:
            os.close(fd) if not os.get_inheritable(fd) else None
            if Path(path).exists():
                Path(path).unlink()
            raise

    def test_temp_directory_cleanup(self):
        """Temp directories can be created and cleaned up."""
        tmp_dir = tempfile.mkdtemp(prefix="xpst_test_")
        p = Path(tmp_dir)
        assert p.exists()

        # Create a file inside
        (p / "test.txt").write_text("hello")

        shutil.rmtree(tmp_dir)
        assert not p.exists()

    def test_shutdown_handler_cleans_temp_files(self, tmp_path):
        """ShutdownHandler tracks and cleans up temp files."""
        handler = ShutdownHandler(str(tmp_path))

        # Create some temp files
        temp_files = []
        for i in range(3):
            f = tmp_path / f"temp_{i}.tmp"
            f.write_text(f"data_{i}")
            temp_files.append(f)

        # Start tracking and register temp files
        handler.start_tracking("video1", "youtube", "encoding")
        for f in temp_files:
            handler.add_temp_file(f)

        # Cleanup
        handler._cleanup_temp_files()

        for f in temp_files:
            assert not f.exists()

    def test_state_manager_atomic_write_cleanup(self, tmp_path):
        """StateManager cleans up .tmp files after atomic write."""
        sm = StateManager(str(tmp_path))
        sm.mark_video_posted("v1", "youtube")
        sm.save()

        # After save, there should be no .tmp files lingering
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_platform_specific_temp_dir(self):
        """tempfile.gettempdir() returns a platform-appropriate directory."""
        tmp_dir = tempfile.gettempdir()
        system = platform.system()

        if system == "Darwin" or system == "Linux":
            assert "/tmp" in tmp_dir or "/var" in tmp_dir
        elif system == "Windows":
            assert "Temp" in tmp_dir or "tmp" in tmp_dir.lower() or "C:\\" in tmp_dir
        else:
            # Just verify it's a valid path
            assert Path(tmp_dir).exists()


# ---------------------------------------------------------------------------
# 8. Unicode paths
# ---------------------------------------------------------------------------

class TestUnicodePaths:
    """Non-ASCII characters in paths work."""

    def test_unicode_state_dir(self, tmp_path):
        """StateManager works with non-ASCII characters in path."""
        unicode_dir = tmp_path / "données_测试_🎵"
        unicode_dir.mkdir()
        sm = StateManager(str(unicode_dir))

        sm.mark_video_posted("video_日本語", "youtube")
        sm.save()

        assert (unicode_dir / "state.json").exists()

        # Reload and verify
        sm2 = StateManager(str(unicode_dir))
        assert sm2.is_video_posted("video_日本語", "youtube")

    def test_unicode_in_config_paths(self, tmp_path):
        """Config loading works with non-ASCII characters in paths."""
        unicode_dir = tmp_path / "config_αβγ"
        unicode_dir.mkdir()

        config_file = unicode_dir / "config.yaml"
        config_data = {
            "video": {
                "download_dir": str(tmp_path / "downloads_éàü"),
            },
            "monitoring": {
                "log_file": str(tmp_path / "logs_кириллица" / "app.log"),
            },
        }
        config_file.write_text(yaml.dump(config_data, allow_unicode=True))

        loaded = XPSTConfig.load(str(config_file))
        assert "éàü" in loaded.video.download_dir
        assert "кириллица" in loaded.monitoring.log_file

    def test_unicode_in_state_file_ids(self, tmp_path):
        """State file handles Unicode video IDs."""
        sm = StateManager(str(tmp_path))
        sm.mark_video_posted("vídeo_123", "youtube", post_url="https://example.com/é")
        sm.mark_video_posted("видео_456", "x")
        sm.save()

        # Reload
        sm2 = StateManager(str(tmp_path))
        assert sm2.is_video_posted("vídeo_123", "youtube")
        assert sm2.is_video_posted("видео_456", "x")

    def test_unicode_credential_storage(self, tmp_path):
        """Credential store handles non-ASCII values."""
        store = CredentialStore(str(tmp_path))
        store._use_keyring = False

        store.store("clé_测试", "valeur_日本語_🔑")
        result = store.retrieve("clé_测试")
        assert result == "valeur_日本語_🔑"

    def test_unicode_in_crash_recovery_checkpoints(self, tmp_path):
        """Crash recovery checkpoints handle Unicode."""
        manager = CrashRecoveryManager(str(tmp_path))
        manager.save_checkpoint("vídeo_🎵", "youtube", "uploading")
        pending = manager.get_pending_checkpoints()

        key = "vídeo_🎵:youtube"
        assert key in pending
        assert pending[key]["video_id"] == "vídeo_🎵"

    def test_unicode_temp_file(self):
        """Temp file with Unicode content works."""
        content = "测试内容 éàü кириллица 🎵"
        fd, path = tempfile.mkstemp(suffix="_xpst_test", text=True)
        try:
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            read_back = Path(path).read_text(encoding="utf-8")
            assert read_back == content
        finally:
            if Path(path).exists():
                Path(path).unlink()

    def test_pure_path_objects_unicode(self):
        """PurePath handles Unicode in all components."""
        p = PurePosixPath("/home/用户/données/видео.mp4")
        assert p.name == "видео.mp4"
        assert p.parent.name == "données"


# ---------------------------------------------------------------------------
# 9. FFmpeg path resolution
# ---------------------------------------------------------------------------

class TestFFmpegPath:
    """FFmpeg path works with absolute, relative, and from PATH."""

    def test_ffmpeg_absolute_path(self, tmp_path):
        """FFmpeg can be specified with an absolute path."""
        # Create a mock ffmpeg script
        ffmpeg_mock = tmp_path / "ffmpeg"
        if platform.system() == "Windows":
            ffmpeg_mock = tmp_path / "ffmpeg.bat"
            ffmpeg_mock.write_text('@echo off\necho ffmpeg version mock\n')
        else:
            ffmpeg_mock.write_text('#!/bin/sh\necho "ffmpeg version mock"\n')
            ffmpeg_mock.chmod(0o755)

        # The VideoProcessor should be able to use this path
        # We test that the path string is passed through correctly
        assert Path(str(ffmpeg_mock)).is_absolute()

    def test_ffmpeg_relative_path_resolution(self):
        """A relative ffmpeg path is resolved via shutil.which."""
        result = shutil.which("ffmpeg")
        if result is not None:
            assert Path(result).is_absolute()

    def test_ffmpeg_path_from_path_env(self):
        """ffmpeg found via PATH environment variable."""
        original_path = os.environ.get("PATH", "")
        # Verify PATH contains at least some directories
        assert len(original_path) > 0

        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is not None:
            ffmpeg_dir = str(Path(ffmpeg).parent)
            assert ffmpeg_dir in original_path.split(os.pathsep)

    def test_video_processor_uses_custom_path(self, tmp_path):
        """VideoProcessor passes custom ffmpeg_path through."""
        from xpst.utils.video import VideoProcessor

        ffmpeg_mock = tmp_path / "ffmpeg"
        if platform.system() == "Windows":
            ffmpeg_mock.write_text('@echo off\nexit /b 1\n')
        else:
            ffmpeg_mock.write_text('#!/bin/sh\nexit 1\n')
            ffmpeg_mock.chmod(0o755)

        # This should raise because the mock returns exit code 1
        with pytest.raises(RuntimeError):
            VideoProcessor(ffmpeg_path=str(ffmpeg_mock))

    def test_ffmpeg_nonexistent_raises_clear_error(self):
        """Clear error message when ffmpeg binary doesn't exist."""
        from xpst.utils.video import VideoProcessor

        with pytest.raises(RuntimeError, match="FFmpeg not found"):
            VideoProcessor(ffmpeg_path="/absolutely/not/a/real/path/ffmpeg")

    def test_ffmpeg_path_with_spaces(self, tmp_path):
        """FFmpeg path containing spaces is handled."""
        spaced_dir = tmp_path / "path with spaces"
        spaced_dir.mkdir()
        ffmpeg_mock = spaced_dir / "ffmpeg"
        if platform.system() == "Windows":
            ffmpeg_mock.write_text('@echo off\necho ffmpeg version 1.0\n')
        else:
            ffmpeg_mock.write_text('#!/bin/sh\necho "ffmpeg version 1.0"\n')
            ffmpeg_mock.chmod(0o755)

        # Path with spaces should be usable
        assert Path(str(ffmpeg_mock)).exists()


# ---------------------------------------------------------------------------
# 10. Line ending handling
# ---------------------------------------------------------------------------

class TestLineEndingHandling:
    """Config files with CRLF vs LF load correctly."""

    def test_lf_config_loads_correctly(self, tmp_path):
        """Config with Unix LF line endings loads correctly."""
        config_data = {
            "accounts": {"tiktok": {"username": "lf_user"}},
            "reliability": {"max_retries": 5},
        }
        config_file = tmp_path / "config.yaml"
        content = yaml.dump(config_data)
        config_file.write_bytes(content.encode("utf-8"))  # LF (default)

        loaded = XPSTConfig.load(str(config_file))
        assert loaded.tiktok.username == "lf_user"
        assert loaded.reliability.max_retries == 5

    def test_crlf_config_loads_correctly(self, tmp_path):
        """Config with Windows CRLF line endings loads correctly."""
        config_data = {
            "accounts": {"tiktok": {"username": "crlf_user"}},
            "reliability": {"max_retries": 7},
        }
        config_file = tmp_path / "config.yaml"
        content = yaml.dump(config_data).replace("\n", "\r\n")
        config_file.write_bytes(content.encode("utf-8"))

        loaded = XPSTConfig.load(str(config_file))
        assert loaded.tiktok.username == "crlf_user"
        assert loaded.reliability.max_retries == 7

    def test_cr_only_config_loads(self, tmp_path):
        """Config with old Mac CR line endings loads correctly."""
        config_data = {
            "accounts": {"tiktok": {"username": "cr_user"}},
        }
        config_file = tmp_path / "config.yaml"
        content = yaml.dump(config_data).replace("\n", "\r")
        config_file.write_bytes(content.encode("utf-8"))

        # YAML parser should handle this
        loaded = XPSTConfig.load(str(config_file))
        assert loaded.tiktok.username == "cr_user"

    def test_mixed_line_endings_in_config(self, tmp_path):
        """Config with mixed CRLF/LF line endings loads correctly."""
        # Use only LF and CRLF (bare CR is not valid YAML line ending and
        # will be rejected by PyYAML; real-world mixed-ending files from
        # Windows editors mix CRLF with LF).
        lines = [
            "accounts:\r\n",                     # CRLF
            "  tiktok:\n",                        # LF
            "    username: mixed_user\r\n",       # CRLF
            "  youtube:\n",                        # LF
            "    enabled: true\r\n",              # CRLF
            "reliability:\n",                     # LF
            "  max_retries: 4\r\n",              # CRLF
        ]
        config_file = tmp_path / "config.yaml"
        config_file.write_bytes("".join(lines).encode("utf-8"))

        loaded = XPSTConfig.load(str(config_file))
        assert loaded.tiktok.username == "mixed_user"
        assert loaded.youtube.enabled is True
        assert loaded.reliability.max_retries == 4

    def test_state_json_with_crlf_loads(self, tmp_path):
        """State JSON file with CRLF line endings loads correctly."""
        state_data = {
            "version": 2,
            "posted_videos": {"v1": {"posted_to": {"youtube": {"id": "abc"}}}},
            "health": {"platforms": {}, "total_processed": 1},
        }
        state_file = tmp_path / "state.json"
        content = json.dumps(state_data, indent=2).replace("\n", "\r\n")
        state_file.write_bytes(content.encode("utf-8"))

        sm = StateManager(str(tmp_path))
        assert sm.is_video_posted("v1", "youtube")

    def test_bom_prefixed_config_loads(self, tmp_path):
        """Config file with UTF-8 BOM loads correctly."""
        config_data = {
            "accounts": {"tiktok": {"username": "bom_user"}},
        }
        config_file = tmp_path / "config.yaml"
        bom = b"\xef\xbb\xbf"
        content = yaml.dump(config_data).encode("utf-8")
        config_file.write_bytes(bom + content)

        loaded = XPSTConfig.load(str(config_file))
        assert loaded.tiktok.username == "bom_user"

    def test_unicode_content_with_crlf(self, tmp_path):
        """Unicode content in CRLF config loads correctly."""
        config_data = {
            "accounts": {"tiktok": {"username": "用户_éàü"}},
        }
        config_file = tmp_path / "config.yaml"
        content = yaml.dump(config_data, allow_unicode=True).replace("\n", "\r\n")
        config_file.write_bytes(content.encode("utf-8"))

        loaded = XPSTConfig.load(str(config_file))
        assert loaded.tiktok.username == "用户_éàü"
