"""MCP surface: a malicious tool argument must be refused like an untrusted input.

An MCP argument is attacker-influenced — an MCP client, or an LLM that has read
untrusted content (a video description, a webhook payload), chooses it. This
suite feeds the guard the classic hostile arguments: ``../../`` escapes, absolute
system paths, loopback/metadata URLs and non-http schemes.

These tests call the real ``xpst.mcp.server`` guard, not a reimplementation.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

mcp_server = pytest.importorskip("xpst.mcp.server")

_argument_shape_block = mcp_server._argument_shape_block


def _is_blocked(name: str, arguments: dict) -> bool:
    return _argument_shape_block(name, arguments) is not None


@pytest.fixture(autouse=True)
def _no_opt_out(monkeypatch):
    """The guard must be ON by default; pin that for every test here."""
    monkeypatch.delenv("XPST_MCP_ALLOW_ANY_PATH", raising=False)


class TestPathArguments:
    @pytest.mark.parametrize(
        "hostile",
        [
            "../../../../etc/passwd",
            "../../../.ssh/id_rsa",
            "/etc/passwd",
            "/etc/shadow",
            "~/.ssh/id_rsa",
            "video.mp4/../../../../../../etc/passwd",
        ],
    )
    def test_hostile_video_path_is_blocked(self, hostile, monkeypatch, tmp_path):
        monkeypatch.setenv("TMPDIR", str(tmp_path / "elsewhere"))
        assert _is_blocked("xpst_post", {"video_path": hostile, "caption": "x"})

    def test_hostile_carousel_entry_is_blocked(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMPDIR", str(tmp_path / "elsewhere"))
        assert _is_blocked(
            "xpst_post",
            {"video_path": str(tmp_path / "ok.mp4"), "carousel_paths": ["/etc/passwd"], "caption": "x"},
        )

    def test_hostile_schedule_add_path_is_blocked(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMPDIR", str(tmp_path / "elsewhere"))
        assert _is_blocked(
            "xpst_schedule_add",
            {"video_path": "../../../../etc/passwd", "caption": "x", "scheduled_time": "2030-01-01T00:00"},
        )

    def test_benign_path_under_a_configured_root_is_allowed(self, tmp_path, monkeypatch):
        """The config-dir root is honoured (injected here via XPST_MEDIA_ROOTS)."""
        monkeypatch.setenv("TMPDIR", str(tmp_path / "elsewhere"))
        library = tmp_path / "cfg" / "library"
        library.mkdir(parents=True)
        monkeypatch.setenv("XPST_MEDIA_ROOTS", str(tmp_path / "cfg"))
        assert not _is_blocked(
            "xpst_post", {"video_path": str(library / "clip.mp4"), "caption": "x"}
        )

    def test_benign_path_under_tmpdir_is_allowed(self, tmp_path):
        assert not _is_blocked(
            "xpst_post", {"video_path": str(tmp_path / "clip.mp4"), "caption": "x"}
        )

    def test_benign_path_under_windows_temp_is_allowed(self, monkeypatch, tmp_path):
        """Windows sets TEMP/TMP, never TMPDIR: the temp root must still be allowed.

        Reading only TMPDIR (or the cached ``tempfile.gettempdir()``) confines
        every legitimate ``%TEMP%\\...`` path — pytest's ``tmp_path`` included —
        out of its own temp root.
        """
        monkeypatch.delenv("TMPDIR", raising=False)
        monkeypatch.setenv("TEMP", str(tmp_path))
        monkeypatch.delenv("TMP", raising=False)
        assert not _is_blocked(
            "xpst_post", {"video_path": str(tmp_path / "clip.mp4"), "caption": "x"}
        )

    def test_non_path_arguments_are_not_touched(self):
        assert not _is_blocked("xpst_status", {})
        assert not _is_blocked("xpst_config_show", {})
        assert not _is_blocked("xpst_run", {"source": "tiktok", "max_posts": 3})


class TestURLArguments:
    @pytest.mark.parametrize(
        "hostile",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://127.0.0.1:8080/state",
            "http://localhost:8080/api/summary",
            "http://[::1]:8080/state",
            "http://10.0.0.1/hook",
            "http://192.168.1.1/hook",
            "file:///etc/passwd",
            "gopher://example.test/x",
        ],
    )
    def test_hostile_url_is_blocked(self, hostile):
        assert _is_blocked("kb_add", {"source": hostile, "workspace": "default"})

    def test_metadata_url_in_a_url_argument_is_blocked(self):
        assert _is_blocked("messenger_send", {"url": "http://169.254.169.254/x"})

    def test_public_url_is_allowed(self):
        assert not _is_blocked("kb_add", {"source": "https://93.184.216.34/article.html"})

    def test_bare_platform_name_is_not_treated_as_a_url(self):
        assert not _is_blocked("kb_add", {"source": "clip.mp4"})
        assert not _is_blocked("xpst_run", {"source": "youtube"})


class TestOptOut:
    def test_opt_out_env_var_disables_path_confinement(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMPDIR", str(tmp_path / "elsewhere"))
        monkeypatch.setenv("XPST_MCP_ALLOW_ANY_PATH", "1")
        assert not _is_blocked("xpst_post", {"video_path": "/etc/passwd", "caption": "x"})

    def test_media_roots_env_var_widens_allowed_roots(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMPDIR", str(tmp_path / "elsewhere"))
        external = tmp_path / "external-drive"
        external.mkdir()
        target = external / "clip.mp4"
        assert _is_blocked("xpst_post", {"video_path": str(target), "caption": "x"})
        monkeypatch.setenv("XPST_MEDIA_ROOTS", str(external))
        assert not _is_blocked("xpst_post", {"video_path": str(target), "caption": "x"})


class TestGuardIsWiredIntoDispatch:
    """The guard must actually be reached by the tool dispatcher."""

    def test_handle_call_tool_consults_the_argument_guard(self, monkeypatch, tmp_path):
        import asyncio

        monkeypatch.setenv("TMPDIR", str(tmp_path / "elsewhere"))
        monkeypatch.setenv("XPST_MCP_ALLOW_MUTATIONS", "1")

        def _sentinel(name, arguments):
            return mcp_server.CallToolResult(
                isError=True,
                content=[mcp_server.TextContent(type="text", text="SENTINEL_BLOCKED")],
            )

        monkeypatch.setattr(mcp_server, "_argument_shape_block", _sentinel)
        result = asyncio.run(
            mcp_server.handle_call_tool("xpst_post", {"video_path": "/etc/passwd", "caption": "x"})
        )
        assert result.isError is True
        assert "SENTINEL_BLOCKED" in result.content[0].text

    def test_source_calls_the_guard_before_engine_init(self):
        """Ordering proof: the guard call precedes get_server/engine creation."""
        source = Path(mcp_server.__file__).read_text(encoding="utf-8")
        guard_at = source.index("_argument_shape_block(name, arguments)")
        server_at = source.index('server = await get_server(initialize=name in engine_tools)')
        assert guard_at < server_at

    def test_dispatcher_uses_the_guard_by_name(self):
        source = Path(mcp_server.__file__).read_text(encoding="utf-8")
        assert "_argument_shape_block" in source
        assert os.path.basename(mcp_server.__file__) == "server.py"
