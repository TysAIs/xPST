"""Windows-native config path contracts for CLI setup tests."""

import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from xpst.cli import main


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific config contract")
def test_non_tty_setup_persists_under_native_appdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    result = CliRunner().invoke(main, ["setup", "--json"])

    assert result.exit_code != 1
    assert (tmp_path / "xPST" / "setup_transaction.json").exists()
