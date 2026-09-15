"""Path confinement: ``../../`` escapes are rejected.

Covers every direction a path can arrive from — MCP tool arguments, persisted
schedule/state entries, and API parameters — with the same helper the product
code now uses.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from xpst.utils.path_guard import (
    PathConfinementError,
    confine_media_path,
    confine_path,
    default_media_roots,
)


@pytest.fixture()
def roots(tmp_path):
    allowed = tmp_path / "library"
    allowed.mkdir()
    return (allowed,)


class TestRelativeEscapes:
    @pytest.mark.parametrize(
        "escape",
        [
            "../secret.txt",
            "../../secret.txt",
            "../../../../../../etc/passwd",
            "./../../secret.txt",
            "sub/../../secret.txt",
            "sub/../../../secret.txt",
        ],
    )
    def test_dotdot_escape_rejected(self, roots, escape):
        with pytest.raises(PathConfinementError):
            confine_path(roots[0] / escape, roots)

    def test_absolute_escape_rejected(self, roots):
        with pytest.raises(PathConfinementError):
            confine_path("/etc/passwd", roots)

    def test_sibling_directory_rejected(self, roots, tmp_path):
        with pytest.raises(PathConfinementError):
            confine_path(tmp_path / "somewhere-else" / "f.mp4", roots)

    def test_prefix_collision_rejected(self, roots, tmp_path):
        """``library-evil`` must not pass just because it starts with ``library``."""
        evil = tmp_path / "library-evil"
        evil.mkdir()
        with pytest.raises(PathConfinementError):
            confine_path(evil / "f.mp4", roots)


class TestAcceptedPaths:
    def test_path_inside_root_accepted(self, roots):
        target = roots[0] / "video.mp4"
        target.write_bytes(b"x")
        assert confine_path(target, roots) == target.resolve()

    def test_nested_path_inside_root_accepted(self, roots):
        nested = roots[0] / "a" / "b"
        nested.mkdir(parents=True)
        target = nested / "video.mp4"
        target.write_bytes(b"x")
        assert confine_path(target, roots, must_exist=True) == target.resolve()

    def test_the_root_itself_is_accepted(self, roots):
        assert confine_path(roots[0], roots) == roots[0].resolve()

    def test_missing_file_allowed_when_not_required(self, roots):
        assert confine_path(roots[0] / "new.mp4", roots).name == "new.mp4"

    def test_missing_file_rejected_when_required(self, roots):
        with pytest.raises(PathConfinementError):
            confine_path(roots[0] / "new.mp4", roots, must_exist=True)


class TestInputHardening:
    @pytest.mark.parametrize("bad", ["", "   ", None])
    def test_empty_path_rejected(self, roots, bad):
        with pytest.raises(PathConfinementError):
            confine_path(bad, roots)

    def test_nul_byte_rejected(self, roots):
        with pytest.raises(PathConfinementError):
            confine_path(f"{roots[0]}/video.mp4\x00.txt", roots)

    def test_symlink_escaping_the_root_rejected(self, roots, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        link = roots[0] / "link.txt"
        try:
            link.symlink_to(outside / "secret.txt")
        except (OSError, NotImplementedError):  # pragma: no cover - Windows without privilege
            pytest.skip("symlinks unavailable")
        with pytest.raises(PathConfinementError):
            confine_path(link, roots)

    def test_symlinked_root_inside_allowed(self, roots, tmp_path):
        """A symlink that stays inside the root is fine."""
        nested = roots[0] / "nested"
        nested.mkdir()
        target = nested / "video.mp4"
        target.write_bytes(b"x")
        link = roots[0] / "alias.mp4"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):  # pragma: no cover
            pytest.skip("symlinks unavailable")
        assert confine_path(link, roots) == target.resolve()


class TestMediaDefaults:
    def test_config_dir_is_a_root(self, tmp_path):
        roots = default_media_roots(str(tmp_path / "cfg"))
        assert (tmp_path / "cfg").resolve() in roots

    def test_tmpdir_is_a_root(self):
        roots = default_media_roots("~/.xpst")
        # Mirror the platform truth: TMPDIR on POSIX, TEMP/TMP on Windows.
        override = os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP")
        expected = Path(override) if override else Path(tempfile.gettempdir())
        assert any(r.resolve() == expected.resolve() for r in roots)

    def test_windows_temp_env_is_honoured(self, monkeypatch, tmp_path):
        """``TEMP``/``TMP`` (never ``TMPDIR``) is the Windows temp contract."""
        monkeypatch.delenv("TMPDIR", raising=False)
        monkeypatch.setenv("TEMP", str(tmp_path))
        monkeypatch.delenv("TMP", raising=False)
        assert any(r.resolve() == tmp_path.resolve() for r in default_media_roots("~/.xpst"))

    def test_tmp_env_is_read_when_temp_is_unset(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TMPDIR", raising=False)
        monkeypatch.delenv("TEMP", raising=False)
        monkeypatch.setenv("TMP", str(tmp_path))
        assert any(r.resolve() == tmp_path.resolve() for r in default_media_roots("~/.xpst"))

    def test_confine_media_path_rejects_a_walk_up(self, tmp_path):
        with pytest.raises(PathConfinementError):
            confine_media_path("/etc/passwd", str(tmp_path / "cfg"))

    def test_confine_media_path_accepts_a_file_under_config_dir(self, tmp_path):
        cfg = tmp_path / "cfg"
        library = cfg / "library"
        library.mkdir(parents=True)
        target = library / "clip.mp4"
        target.write_bytes(b"x")
        assert confine_media_path(target, str(cfg), must_exist=True) == target.resolve()

    def test_extra_roots_are_honoured(self, tmp_path, monkeypatch):
        # Repoint TMPDIR so pytest's tmp_path is NOT already an allowed root
        # (otherwise this test would prove nothing — everything under TMPDIR is
        # allowed by default).
        monkeypatch.setenv("TMPDIR", str(tmp_path / "elsewhere"))
        cfg = tmp_path / "cfg"
        extra = tmp_path / "external-disk"
        extra.mkdir()
        target = extra / "clip.mp4"
        target.write_bytes(b"x")
        with pytest.raises(PathConfinementError):
            confine_media_path(target, str(cfg), must_exist=True)
        assert (
            confine_media_path(target, str(cfg), extra_roots=(extra,), must_exist=True)
            == target.resolve()
        )

    def test_media_roots_env_override_is_honoured(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path / "elsewhere"))
        cfg = tmp_path / "cfg"
        extra = tmp_path / "external-disk"
        extra.mkdir()
        target = extra / "clip.mp4"
        target.write_bytes(b"x")
        monkeypatch.setenv("XPST_MEDIA_ROOTS", str(extra))
        assert confine_media_path(target, str(cfg), must_exist=True) == target.resolve()
