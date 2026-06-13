"""Tests for clean-install smoke helper."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts import clean_install_smoke as smoke
from scripts.clean_install_smoke import _select_artifacts, find_sdist, find_wheel, json_from_output, write_smoke_config


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


def test_smoke_artifact_uses_install_timeout(monkeypatch, tmp_path):
    artifact = tmp_path / "xpst-0.1.0-py3-none-any.whl"
    artifact.write_bytes(b"wheel")
    install_timeouts: list[int] = []

    class DummyBuilder:
        def __init__(self, **_kwargs):
            pass

        def create(self, venv_dir: Path) -> None:
            scripts_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
            scripts_dir.mkdir(parents=True)
            (scripts_dir / ("python.exe" if os.name == "nt" else "python")).write_text("", encoding="utf-8")
            (scripts_dir / ("xpst.exe" if os.name == "nt" else "xpst")).write_text("", encoding="utf-8")

    def fake_run_command(cmd, env, timeout=120):
        if "pip" in cmd:
            install_timeouts.append(timeout)
        stdout = "{\"ok\": true, \"xpst\": \"0.1.0\"}" if "version" in cmd else "{\"ok\": true}"
        if "diagnostics" in cmd:
            output_path = Path(cmd[cmd.index("--output") + 1])
            import zipfile

            with zipfile.ZipFile(output_path, "w") as archive:
                archive.writestr("README.txt", "smoke")
                archive.writestr("diagnostics.json", "{}")
        return smoke.subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.setattr(smoke.venv, "EnvBuilder", DummyBuilder)
    monkeypatch.setattr(smoke, "run_command", fake_run_command)

    result = smoke._smoke_artifact(artifact, tmp_path / "work", install_timeout=777)

    assert result["ok"] is True
    assert install_timeouts == [777]
