"""Regression: X's Sep-2026 home-page manifest vs the twikit compat patches.

QA-2026-09-28 D1: twikit 2.3.3's stock ``ON_DEMAND_FILE_REGEX`` expects the
legacy ``"ondemand.s":"<hash>"`` pair. X's Sep-2026 home page lists chunk
names positionally (``,59924:"ondemand.s"``) with a separate id→hash map, so
the stock regex finds nothing, ``get_indices`` raises, ``ClientTransaction``
never assigns ``.key``, and every X analytics/posting call fails with
``'ClientTransaction' object has no attribute 'key'``.

These fixtures are the CAPTURED live pages from that QA round (sanitized:
logged-out user block removed, PII scan clean). The test replays the patched
extraction against them, so a future twikit/X manifest change fails here
instead of surfacing as silent-zero analytics.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from xpst.platforms.twikit_compat import apply_twikit_patches

FIXTURES = Path(__file__).parent / "fixtures"
HOME_HTML = FIXTURES / "x_home_2026-09.html"
ONDEMAND_JS = FIXTURES / "x_ondemand_s_2026-09.js"


def test_fixtures_exist_and_are_pii_clean() -> None:
    html = HOME_HTML.read_text(encoding="utf-8")
    js = ONDEMAND_JS.read_text(encoding="utf-8")
    # The captured manifest must be present and the capture must not carry
    # account data (repo rule: zero personal data).
    assert re.search(r',\d+:"ondemand\.s"', html)
    assert "twitter-site-verification" in html
    for token in ("tysn", "itxji", "Tyler", "sessionid", "ct0"):
        assert token not in html
        assert token not in js


def test_stock_twikit_regex_fails_on_the_captured_manifest() -> None:
    """Pin the upstream break: the stock regex cannot see the Sep-2026 map."""
    stock = re.compile(
        r"""['|\"]{1}ondemand\.s['|\"]{1}:\s*['|\"]{1}([\w]*)['|\"]{1}""",
        flags=(re.VERBOSE | re.MULTILINE),
    )
    html = HOME_HTML.read_text(encoding="utf-8")
    assert stock.search(html) is None


def test_patched_extraction_resolves_bundle_url_from_captured_html() -> None:
    """The patched get_indices finds the hashed bundle from the real HTML."""
    assert apply_twikit_patches()
    import importlib

    tx = importlib.import_module("twikit.x_client_transaction.transaction")

    html = HOME_HTML.read_text(encoding="utf-8")
    match = tx.ON_DEMAND_FILE_REGEX.search(html)
    assert match is not None, "patched regex must match the captured manifest"
    regex = re.compile(tx.ON_DEMAND_HASH_PATTERN.format(match.group(1)))
    hash_match = regex.search(html)
    assert hash_match is not None, "patched hash map lookup must succeed"
    url = f"https://abs.twimg.com/responsive-web/client-web/ondemand.s.{hash_match.group(1)}a.js"
    assert url.endswith("ondemand.s.896505c62629178da.js")


def test_patched_get_indices_returns_indices_without_network() -> None:
    """End-to-end get_indices() against a fake session serving the fixture JS."""
    assert apply_twikit_patches()
    import importlib

    from bs4 import BeautifulSoup

    tx = importlib.import_module("twikit.x_client_transaction.transaction")

    class _Response:
        def __init__(self, text: str) -> None:
            self.text = text

    class _Session:
        def __init__(self, js: str) -> None:
            self.js = js
            self.requested: list[str] = []

        async def request(self, method: str, url: str, headers=None):  # noqa: ANN001, ANN002
            self.requested.append(url)
            return _Response(self.js)

    html = HOME_HTML.read_text(encoding="utf-8")
    session = _Session(ONDEMAND_JS.read_text(encoding="utf-8"))
    client_transaction = tx.ClientTransaction()
    soup = BeautifulSoup(html, "html.parser")

    row_index, key_indices = asyncio.run(
        client_transaction.get_indices(soup, session, {})
    )
    assert session.requested == [
        "https://abs.twimg.com/responsive-web/client-web/ondemand.s.896505c62629178da.js"
    ]
    # The captured bundle yields the (17, [18, 46, 40]) pattern live in QA.
    assert row_index == 17
    assert key_indices and all(isinstance(i, int) for i in key_indices)


def test_analytics_import_applies_the_patches() -> None:
    """Owning the regression: the analytics path itself must apply the patches.

    D1's root cause was that only the uploader module patched twikit while the
    collector imported neither. The collector's twikit path calls
    apply_twikit_patches() before creating a client, so importing ONLY
    xpst.analytics (never xpst.platforms.x) and running that call must leave
    the patched regex + patched get_indices active.
    """
    import importlib

    analytics = importlib.import_module("xpst.analytics")
    compat = importlib.import_module("xpst.platforms.twikit_compat")
    assert compat.apply_twikit_patches() is True
    tx = importlib.import_module("twikit.x_client_transaction.transaction")
    assert "ondemand" in tx.ON_DEMAND_FILE_REGEX.pattern
    assert tx.ClientTransaction.get_indices.__name__ == "_patched_get_indices"
    # The collector module itself must own the wiring: its twikit path names
    # the compat helper (grep-level proof the fix cannot silently regress to
    # uploader-only patching).
    import inspect

    source = inspect.getsource(analytics.AnalyticsCollector._collect_x_twikit)
    assert "apply_twikit_patches" in source
