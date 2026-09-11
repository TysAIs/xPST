"""Windows config-root fallback regression."""

import sys
from pathlib import Path

import pytest

from xpst.utils import platform


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific path contract")
def test_windows_config_dir_uses_userprofile_when_appdata_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    monkeypatch.setattr(platform.sys, "platform", "win32")

    assert platform.get_config_dir() == tmp_path / "profile" / "AppData" / "Roaming" / "xPST"
