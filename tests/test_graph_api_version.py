"""The Meta Graph API version is pinned in exactly one place.

Meta removes a Graph API version roughly two years after it lands (v19.0 on
2026-05-21, v20.0 on 2026-09-24, v21.0 on 2027-01-21 — see
https://developers.facebook.com/docs/graph-api/changelog). A version literal
copied into a platform module keeps working until the removal day and then every
Instagram / Facebook / Messenger call breaks at once, which is exactly the state
this suite exists to prevent:

* the pinned version must be a version Meta currently serves (not one of the
  known-removed pins);
* every Graph host URL in ``src/`` must be built by ``xpst.utils.graph_api`` —
  a new ``graph.facebook.com/vNN`` literal fails here;
* ``XPST_GRAPH_API_BASE`` / ``XPST_GRAPH_API_VERSION`` still retarget the
  family, and the helpers stay version-consistent across hosts.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from xpst.utils import graph_api

if TYPE_CHECKING:
    import pytest

SRC = Path(__file__).resolve().parent.parent / "src"

#: Pins Meta had already removed when this guard was written. A pin equal to one
#: of these means the bump was reverted or never landed.
_REMOVED_VERSIONS = frozenset({"v18.0", "v19.0", "v20.0"})

#: ``graph.facebook.com/v26.0/...`` style literals, i.e. a host followed by a
#: version segment outside the helper module.
_VERSIONED_HOST_LITERAL = re.compile(
    r"(?:graph|graph-video|rupload)\.facebook\.com/[^\s\"']*?v\d+\.\d+"
)
_FACEBOOK_DIALOG_LITERAL = re.compile(r"facebook\.com/v\d+\.\d+/dialog/oauth")


def _python_sources() -> list[Path]:
    return sorted(
        path
        for path in SRC.rglob("*.py")
        if path.name != "graph_api.py"
    )


def test_pinned_version_is_not_a_removed_version() -> None:
    assert graph_api.GRAPH_API_VERSION.startswith("v")
    assert graph_api.GRAPH_API_VERSION not in _REMOVED_VERSIONS


def test_no_module_hardcodes_a_graph_api_version() -> None:
    offenders: list[str] = []
    for path in _python_sources():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _VERSIONED_HOST_LITERAL.search(line) or _FACEBOOK_DIALOG_LITERAL.search(line):
                offenders.append(f"{path.relative_to(SRC.parent)}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "Graph API URLs must be built by xpst.utils.graph_api, not by version "
        "literals:\n" + "\n".join(offenders)
    )


def test_base_and_video_and_upload_hosts_share_one_version() -> None:
    base = graph_api.graph_api_base()
    version = graph_api.graph_api_version()
    assert base == f"{graph_api.GRAPH_API_HOST}/{version}"
    assert graph_api.graph_api_url("me") == f"{base}/me"
    assert graph_api.graph_video_api_url("123").startswith(
        f"{graph_api.GRAPH_VIDEO_API_HOST}/{version}/"
    )
    assert graph_api.graph_upload_url("456").startswith(
        f"{graph_api.GRAPH_UPLOAD_HOST}/ig-api-upload/{version}/"
    )
    assert graph_api.graph_login_dialog_url() == f"{graph_api.GRAPH_LOGIN_HOST}/{version}/dialog/oauth"


def test_env_base_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XPST_GRAPH_API_BASE", "https://graph.facebook.com/v23.0/")
    assert graph_api.graph_api_base() == "https://graph.facebook.com/v23.0"
    assert graph_api.graph_api_url("me") == "https://graph.facebook.com/v23.0/me"


def test_env_version_override_retargets_every_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XPST_GRAPH_API_BASE", raising=False)
    monkeypatch.setenv("XPST_GRAPH_API_VERSION", "v25.0")
    assert graph_api.graph_api_url("me") == "https://graph.facebook.com/v25.0/me"
    assert graph_api.graph_upload_url("1").split("/")[4] == "v25.0"
    assert graph_api.graph_login_dialog_url().split("/")[3] == "v25.0"


def test_explicit_version_bypasses_the_base_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single ``XPST_GRAPH_API_BASE`` cannot describe two versions at once."""
    monkeypatch.setenv("XPST_GRAPH_API_BASE", "https://graph.facebook.com/v23.0")
    assert graph_api.graph_api_url("me", version="v21.0") == (
        "https://graph.facebook.com/v21.0/me"
    )


def test_facebook_client_and_messenger_use_the_pin() -> None:
    from xpst.platforms import facebook, messenger

    client = facebook.FacebookGraphClient()
    try:
        assert client.url("me/accounts") == graph_api.graph_api_url("me/accounts")
        assert client.url("123/videos", video=True).startswith(
            f"{graph_api.GRAPH_VIDEO_API_HOST}/{graph_api.GRAPH_API_VERSION}/"
        )
    finally:
        client.close()

    assert messenger.MESSENGER_API_VERSION == graph_api.GRAPH_API_VERSION
    assert messenger.COMMENT_API_VERSION == graph_api.GRAPH_API_VERSION
    assert f"{messenger.MESSENGER_API_BASE}/{messenger.MESSENGER_API_VERSION}" == (
        graph_api.graph_api_base()
    )


def test_instagram_and_connect_build_on_the_pin() -> None:
    import xpst.connect as connect
    from xpst.platforms import instagram_account

    bases = {"connect": connect.GRAPH_API_BASE, "instagram_account": instagram_account.GRAPH_API_BASE}
    assert set(bases.values()) == {graph_api.graph_api_base()}, bases
