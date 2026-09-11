"""Windows CLI config-directory isolation contract."""

import sys
from pathlib import Path

import pytest

from xpst.utils import platform


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific path contract")
def test_windows_config_dir_uses_appdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    appdata = tmp_path / "roaming"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(platform.sys, "platform", "win32")

    assert platform.get_config_dir() == appdata / "xPST"
