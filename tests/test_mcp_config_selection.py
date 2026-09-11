"""MCP uses the CLI-selected config path rather than a detached default."""

from unittest.mock import patch

from click.testing import CliRunner

from xpst.cli import main


def test_mcp_start_forwards_global_config_path() -> None:
    runner = CliRunner()
    with patch("xpst.mcp.cli_main") as cli_main:
        result = runner.invoke(main, ["--config", "/tmp/selected-xpst/config.yaml", "mcp", "start"])

    assert result.exit_code == 0
    cli_main.assert_called_once_with("/tmp/selected-xpst/config.yaml")


def test_mcp_group_forwards_global_config_path() -> None:
    runner = CliRunner()
    with patch("xpst.mcp.cli_main") as cli_main:
        result = runner.invoke(main, ["--config", "/tmp/selected-xpst/config.yaml", "mcp"])

    assert result.exit_code == 0
    cli_main.assert_called_once_with("/tmp/selected-xpst/config.yaml")
