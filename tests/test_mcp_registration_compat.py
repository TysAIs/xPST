"""The served MCP surface must exist on whichever mcp SDK line is installed.

``mcp>=1.0.0,<3.0.0`` spans a breaking change: 1.x registers ``tools/list`` and
``tools/call`` with decorators on the ``Server`` instance, 2.x takes the same
handlers as constructor keywords receiving ``(request_context, params)``. These
tests are SDK-line neutral on purpose — they assert the *contract* (the server
advertises a tools capability and serves the registry) rather than one API
shape, so the suite is meaningful on either pin instead of only on the newest.
"""

from __future__ import annotations

import asyncio

import pytest

from xpst.mcp import server as mcp_server
from xpst.mcp.server import TOOLS, app, list_tools, result_is_error

mcp_server_pkg = pytest.importorskip("mcp")


def test_registration_probe_matches_the_installed_sdk() -> None:
    """The version probe must agree with the SDK actually importable."""
    from mcp.server import Server

    assert hasattr(Server, "list_tools") == mcp_server._SERVER_DECORATOR_API


def test_served_server_advertises_the_tools_capability() -> None:
    """A server that never registered its handlers advertises no tools."""
    options = app.create_initialization_options()
    assert options.server_name == "xpst-mcp"
    assert options.capabilities.tools is not None


def test_tools_list_serves_the_whole_registry() -> None:
    listed = asyncio.run(list_tools())
    served = [tool.name for tool in listed.tools]
    assert served == [tool.name for tool in TOOLS]
    assert len(served) == 42


@pytest.mark.skipif(
    mcp_server._SERVER_DECORATOR_API,
    reason="1.x registers handlers with decorators; the constructor path is 2.x only",
)
def test_mcp2_handler_receives_the_request_params_and_keeps_the_guard(monkeypatch) -> None:
    """The 2.x adapter must unwrap ``params`` into the guarded handler.

    An unwatched agent asking for a post is refused, so the refusal text is the
    proof that the registered handler reached the real guard rather than a
    pass-through stub.
    """
    from mcp.types import CallToolRequestParams

    for variable in ("XPST_MCP_READONLY", "XPST_MCP_REQUIRE_CONFIRM", "XPST_MCP_ALLOW_MUTATIONS"):
        monkeypatch.delenv(variable, raising=False)

    entry = app.get_request_handler("tools/call")
    assert entry is not None, "tools/call was never registered on the 2.x server"

    params = CallToolRequestParams(name="xpst_post", arguments={"video_path": "/tmp/clip.mp4"})
    result = asyncio.run(entry.handler(None, params))

    assert result_is_error(result)
    assert "XPST_MCP_ALLOW_MUTATIONS" in result.content[0].text
