"""Unit tests for scripts/verify_linux_binary.py pure logic (W3-2).

These exercise the artifact-not-found and digest paths without launching any
binary, so they run on any platform/CI.
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_linux_binary.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("verify_linux_binary", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sha256_digest_matches_hashlib(tmp_path):
    mod = _load_module()
    f = tmp_path / "artifact.bin"
    payload = b"xpst-linux-binary-bytes" * 1000
    f.write_bytes(payload)
    assert mod.sha256_digest(f) == hashlib.sha256(payload).hexdigest()


def test_verify_missing_artifact_is_not_ok(tmp_path):
    mod = _load_module()
    result = mod.verify_linux_binary(tmp_path / "does-not-exist", seconds=1)
    assert result["ok"] is False
    assert "not found" in result["error"].lower()


def test_main_returns_nonzero_for_missing_artifact(monkeypatch, tmp_path, capsys):
    mod = _load_module()
    monkeypatch.setattr("sys.argv", ["verify_linux_binary.py", "--path", str(tmp_path / "nope"), "--json"])
    rc = mod.main()
    assert rc == 1
    out = capsys.readouterr().out
    assert '"ok": false' in out.lower()
