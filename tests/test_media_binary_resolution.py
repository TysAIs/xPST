"""Resolution order for ffmpeg/ffprobe: env override > system > fetched.

The desktop bundle no longer ships ffmpeg, so where a binary comes from is now
load-bearing:

  1. ``XPST_FFMPEG_PATH`` / ``XPST_FFPROBE_PATH`` — explicit user override;
  2. a system install (PATH, then the well-known GUI-launch locations);
  3. a copy xPST fetched on first use into ``<config dir>/bin``.

Each step is exercised with the others neutralised, so the tests do not depend
on what happens to be installed on the host.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from xpst.utils.platform import (
    get_media_bin_dir,
    resolve_ffmpeg_path,
    resolve_ffprobe_path,
    system_media_dirs,
)

ENV_VARS = (
    "XPST_FFMPEG_PATH",
    "XPST_FFPROBE_PATH",
    "XPST_MEDIA_BIN_DIR",
    "XPST_CONFIG_DIR",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def no_path_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("xpst.utils.platform.shutil.which", lambda name: None)


def _fake_binary(path: Path, body: bytes = b"#!/bin/sh\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(0o755)
    return path


@pytest.mark.parametrize(
    ("resolver", "env_var", "name"),
    [
        (resolve_ffmpeg_path, "XPST_FFMPEG_PATH", "ffmpeg"),
        (resolve_ffprobe_path, "XPST_FFPROBE_PATH", "ffprobe"),
    ],
)
def test_env_override_beats_system_and_fetched(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    resolver,
    env_var: str,
    name: str,
) -> None:
    override = _fake_binary(tmp_path / "override" / name)
    system = tmp_path / "system"
    monkeypatch.setattr("xpst.utils.platform.system_media_dirs", lambda: [system])
    _fake_binary(system / name)
    fetched_dir = tmp_path / "fetched"
    _fake_binary(fetched_dir / name)
    monkeypatch.setenv("XPST_MEDIA_BIN_DIR", str(fetched_dir))
    monkeypatch.setenv(env_var, str(override))
    monkeypatch.setattr("xpst.utils.platform.shutil.which", lambda _name: str(system / name))

    assert resolver() == str(override)


@pytest.mark.parametrize(
    ("resolver", "env_var", "name"),
    [
        (resolve_ffmpeg_path, "XPST_FFMPEG_PATH", "ffmpeg"),
        (resolve_ffprobe_path, "XPST_FFPROBE_PATH", "ffprobe"),
    ],
)
def test_system_install_beats_fetched_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    no_path_lookup: None,
    resolver,
    env_var: str,
    name: str,
) -> None:
    system_dir = tmp_path / "system"
    system = _fake_binary(system_dir / name)
    monkeypatch.setattr("xpst.utils.platform.system_media_dirs", lambda: [system_dir])
    fetched_dir = tmp_path / "fetched"
    _fake_binary(fetched_dir / name)
    monkeypatch.setenv("XPST_MEDIA_BIN_DIR", str(fetched_dir))

    assert resolver() == str(system)


@pytest.mark.parametrize(
    ("resolver", "env_var", "name"),
    [
        (resolve_ffmpeg_path, "XPST_FFMPEG_PATH", "ffmpeg"),
        (resolve_ffprobe_path, "XPST_FFPROBE_PATH", "ffprobe"),
    ],
)
def test_fetched_copy_used_when_no_env_and_no_system(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    no_path_lookup: None,
    resolver,
    env_var: str,
    name: str,
) -> None:
    """The machine-without-ffmpeg case: only the fetched copy exists."""
    monkeypatch.setattr("xpst.utils.platform.system_media_dirs", lambda: [tmp_path / "empty"])
    fetched_dir = tmp_path / "fetched"
    fetched = _fake_binary(fetched_dir / name)
    monkeypatch.setenv("XPST_MEDIA_BIN_DIR", str(fetched_dir))

    assert resolver() == str(fetched)


@pytest.mark.parametrize(
    ("resolver", "name"),
    [(resolve_ffmpeg_path, "ffmpeg"), (resolve_ffprobe_path, "ffprobe")],
)
def test_nothing_available_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    no_path_lookup: None,
    resolver,
    name: str,
) -> None:
    monkeypatch.setattr("xpst.utils.platform.system_media_dirs", lambda: [tmp_path / "empty"])
    monkeypatch.setenv("XPST_MEDIA_BIN_DIR", str(tmp_path / "also-empty"))

    assert resolver() is None


@pytest.mark.parametrize(
    ("resolver", "name"),
    [(resolve_ffmpeg_path, "ffmpeg"), (resolve_ffprobe_path, "ffprobe")],
)
def test_fetched_copy_must_be_executable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    no_path_lookup: None,
    resolver,
    name: str,
) -> None:
    """A file that is not executable is not a usable binary."""
    monkeypatch.setattr("xpst.utils.platform.system_media_dirs", lambda: [tmp_path / "empty"])
    fetched_dir = tmp_path / "fetched"
    not_executable = fetched_dir / name
    not_executable.parent.mkdir(parents=True)
    not_executable.write_bytes(b"not a binary")
    not_executable.chmod(0o644)
    monkeypatch.setenv("XPST_MEDIA_BIN_DIR", str(fetched_dir))

    assert resolver() is None


def test_missing_env_override_falls_through(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_path_lookup: None) -> None:
    monkeypatch.setenv("XPST_FFMPEG_PATH", "/nonexistent/ffmpeg")
    monkeypatch.setattr("xpst.utils.platform.system_media_dirs", lambda: [tmp_path / "empty"])
    monkeypatch.setenv("XPST_MEDIA_BIN_DIR", str(tmp_path / "fetched"))
    fetched = _fake_binary(tmp_path / "fetched" / "ffmpeg")

    assert resolve_ffmpeg_path() == str(fetched)


def test_system_media_dirs_include_gui_launch_locations() -> None:
    dirs = system_media_dirs()
    home = Path.home()
    assert home / "bin" in dirs
    assert Path("/opt/homebrew/bin") in dirs
    assert Path("/usr/local/bin") in dirs
    assert home / ".local" / "bin" in dirs


def test_media_bin_dir_honours_explicit_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XPST_MEDIA_BIN_DIR", str(tmp_path / "custom-bin"))

    assert get_media_bin_dir() == tmp_path / "custom-bin"


def test_media_bin_dir_follows_relocated_config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An isolated profile (XPST_CONFIG_DIR) owns its own bin dir."""
    monkeypatch.setenv("XPST_CONFIG_DIR", str(tmp_path / "profile"))

    assert get_media_bin_dir() == tmp_path / "profile" / "bin"


def test_media_bin_dir_defaults_under_config_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("xpst.utils.platform.get_config_dir", lambda: Path("/tmp/xpst-config"))

    assert get_media_bin_dir() == Path("/tmp/xpst-config/bin")
