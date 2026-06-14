"""Tests for clean-install smoke helper."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.clean_install_smoke import (
    _select_artifacts,
    find_sdist,
    find_wheel,
    json_from_output,
    run_clean_install_smoke,
    venv_xpst_mcp,
    write_smoke_config,
    write_smoke_kb_store,
)


def test_find_wheel_returns_newest_xpst_wheel(tmp_path):
    old = tmp_path / "xpst-0.1.0-py3-none-any.whl"
    new = tmp_path / "xpst-0.2.0-py3-none-any.whl"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    os.utime(old, (1, 1))
    os.utime(new, (2, 2))

    assert find_wheel(tmp_path) == new


def test_find_wheel_errors_when_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        find_wheel(tmp_path)


def test_find_sdist_returns_newest_xpst_sdist(tmp_path):
    old = tmp_path / "xpst-0.1.0.tar.gz"
    new = tmp_path / "xpst-0.2.0.tar.gz"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    os.utime(old, (1, 1))
    os.utime(new, (2, 2))

    assert find_sdist(tmp_path) == new


def test_select_artifacts_supports_both(tmp_path):
    wheel = tmp_path / "xpst-0.1.0-py3-none-any.whl"
    sdist = tmp_path / "xpst-0.1.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")

    assert _select_artifacts(tmp_path, "both") == [wheel, sdist]


def test_json_from_output_ignores_log_prefix():
    output = "INFO noisy log line\n{\"ok\": true}\n"

    assert json_from_output(output) == {"ok": True}


def test_write_smoke_config_uses_temp_paths(tmp_path):
    config_path = write_smoke_config(tmp_path)

    text = config_path.read_text(encoding="utf-8")
    assert "smoke_creator" in text
    assert str(Path.home()) not in text
    assert "enabled: false" in text
    assert json.dumps(str(tmp_path)) not in text


def test_venv_xpst_mcp_points_to_console_script(tmp_path):
    path = venv_xpst_mcp(tmp_path / "venv")

    assert path.name in {"xpst-mcp", "xpst-mcp.exe"}
    assert "xpst-mcp" in str(path)


def test_write_smoke_kb_store_seeds_substring_query_nugget(tmp_path):
    config_path = write_smoke_config(tmp_path)

    store_path = write_smoke_kb_store(config_path)

    data = json.loads(store_path.read_text(encoding="utf-8"))
    nugget = data["packaged-stdio-smoke"]
    assert store_path == tmp_path / "xpst-home" / "knowledge" / "default" / "nuggets.json"
    assert "packaged stdio smoke" in nugget["point"]
    assert nugget["embedding"] == []
    assert nugget["source_video_id"] == "clean-install-smoke"


def test_clean_install_smoke_disables_pip_cache(tmp_path, monkeypatch):
    artifact = tmp_path / "dist" / "xpst-0.1.0.tar.gz"
    artifact.parent.mkdir()
    artifact.write_bytes(b"sdist")
    commands = []

    class FakeBuilder:
        def __init__(self, *args, **kwargs):
            pass

        def create(self, venv_dir):
            (venv_dir / "Scripts").mkdir(parents=True)

    def fake_run_command(cmd, env):
        commands.append(cmd)
        return SimpleNamespace(returncode=1, stdout="", stderr="stop after install")

    monkeypatch.setattr("scripts.clean_install_smoke.venv.EnvBuilder", FakeBuilder)
    monkeypatch.setattr("scripts.clean_install_smoke.run_command", fake_run_command)

    with pytest.raises(RuntimeError):
        run_clean_install_smoke(artifact.parent, tmp_path / "work", artifact="sdist")

    assert "--no-cache-dir" in commands[0]
