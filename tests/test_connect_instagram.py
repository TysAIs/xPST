"""Tests for the Instagram Graph API connect flow (connect.py).

All network calls are mocked — no live Meta API calls.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx

from xpst.connect import (
    GRAPH_API_BASE,
    _detect_ig_user_id,
    _fetch_ig_business_accounts,
    _graph_api_verify_url,
    _verify_instagram_graph_token,
)

if TYPE_CHECKING:
    import pytest

IG_USER_ID = "17841400000000000"
PAGE_TOKEN = "EAAG-test-page-token"


class FakeResponse:
    """Minimal response-like object backed by a real httpx.Response."""

    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload
        self.request = httpx.Request("GET", GRAPH_API_BASE)
        self._response = httpx.Response(status_code, json=payload, request=self.request)

    def json(self) -> dict:
        return dict(self._payload)

    @property
    def text(self) -> str:
        return json.dumps(self._payload)


class TestGraphApiVerifyUrlBuilder:
    """The graph_api verify URL builder must produce a well-formed v21.0 URL."""

    def test_builds_full_url_with_query_params(self) -> None:
        url = _graph_api_verify_url(IG_USER_ID, PAGE_TOKEN)
        assert url.startswith(f"{GRAPH_API_BASE}/{IG_USER_ID}?")
        assert "fields=username%2Cfollowers_count%2Cmedia_count" in url
        assert f"access_token={PAGE_TOKEN}" in url

    def test_url_does_not_leak_token_in_path(self) -> None:
        url = _graph_api_verify_url(IG_USER_ID, PAGE_TOKEN)
        path, _, query = url.partition("?")
        assert PAGE_TOKEN not in path
        assert PAGE_TOKEN in query


class TestVerifyInstagramGraphToken:
    """Token verification via the mocked httpx.get."""

    def test_success_returns_parsed_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = {
            "id": IG_USER_ID,
            "username": "brand.ig",
            "followers_count": 1234,
            "media_count": 56,
        }
        captured: dict = {}

        def fake_get(url: str, timeout: float = 15.0) -> FakeResponse:
            captured["url"] = url
            captured["timeout"] = timeout
            return FakeResponse(payload)

        monkeypatch.setattr("xpst.connect.httpx.get", fake_get)
        data = _verify_instagram_graph_token(IG_USER_ID, PAGE_TOKEN)

        assert data == payload
        # The mocked call must hit the URL built by _graph_api_verify_url.
        assert captured["url"] == _graph_api_verify_url(IG_USER_ID, PAGE_TOKEN)
        assert captured["timeout"] == 15.0

    def test_http_error_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_get(url: str, timeout: float = 15.0) -> FakeResponse:
            return FakeResponse({"error": {"message": "Invalid OAuth access token"}}, 400)

        monkeypatch.setattr("xpst.connect.httpx.get", fake_get)
        assert _verify_instagram_graph_token(IG_USER_ID, "bad-token") is None

    def test_exception_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_get(url: str, timeout: float = 15.0) -> FakeResponse:
            raise httpx.ConnectError("boom", request=httpx.Request("GET", url))

        monkeypatch.setattr("xpst.connect.httpx.get", fake_get)
        assert _verify_instagram_graph_token(IG_USER_ID, PAGE_TOKEN) is None


class TestDetectIgUserId:
    """Auto-detection of the IG business account from a token."""

    def test_detects_via_me_when_token_is_ig_scoped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explorer token generated for the IG account: /me returns IG fields."""
        calls: list[dict] = []

        def fake_get(url: str, params: dict | None = None, timeout: float = 15.0) -> FakeResponse:
            calls.append({"url": url, "params": params})
            if url.endswith("/me"):
                return FakeResponse(
                    {
                        "id": IG_USER_ID,
                        "username": "brand.ig",
                        "followers_count": 99,
                        "media_count": 7,
                    }
                )
            return FakeResponse({"data": []})

        monkeypatch.setattr("xpst.connect.httpx.get", fake_get)
        ig_user_id, username, followers, media = _detect_ig_user_id(PAGE_TOKEN)

        assert ig_user_id == IG_USER_ID
        assert username == "brand.ig"
        assert followers == 99
        assert media == 7
        assert calls[0]["url"] == f"{GRAPH_API_BASE}/me"
        assert calls[0]["params"]["fields"] == "id,username,followers_count,media_count"

    def test_detects_via_page_accounts_when_me_is_user_scoped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """User token: /me has no username → fall through to /me/accounts."""

        def fake_get(url: str, params: dict | None = None, timeout: float = 15.0) -> FakeResponse:
            if url.endswith("/me"):
                return FakeResponse({"id": "10220000000000000"})  # user id, no IG fields
            assert url.endswith("/me/accounts")
            return FakeResponse(
                {
                    "data": [
                        {
                            "id": "page_123",
                            "name": "Brand Page",
                            "instagram_business_account": {
                                "id": IG_USER_ID,
                                "username": "brand.ig",
                                "followers_count": 5,
                                "media_count": 2,
                            },
                        }
                    ]
                }
            )

        monkeypatch.setattr("xpst.connect.httpx.get", fake_get)
        ig_user_id, username, followers, media = _detect_ig_user_id(PAGE_TOKEN)

        assert ig_user_id == IG_USER_ID
        assert username == "brand.ig"
        assert followers == 5
        assert media == 2

    def test_returns_empty_when_no_account_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_get(url: str, params: dict | None = None, timeout: float = 15.0) -> FakeResponse:
            if url.endswith("/me"):
                return FakeResponse({"id": "10220000000000000"})
            return FakeResponse({"data": []})

        monkeypatch.setattr("xpst.connect.httpx.get", fake_get)
        assert _detect_ig_user_id(PAGE_TOKEN) == ("", "", 0, 0)

    def test_returns_empty_on_api_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_get(url: str, params: dict | None = None, timeout: float = 15.0) -> FakeResponse:
            return FakeResponse({"error": {"message": "Invalid OAuth access token"}}, 400)

        monkeypatch.setattr("xpst.connect.httpx.get", fake_get)
        assert _detect_ig_user_id("bad-token") == ("", "", 0, 0)


class TestFetchIgBusinessAccounts:
    def test_collects_instagram_business_accounts_from_pages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_get(url: str, params: dict | None = None, timeout: float = 15.0) -> FakeResponse:
            assert params is not None
            assert "instagram_business_account" in params["fields"]
            return FakeResponse(
                {
                    "data": [
                        {
                            "id": "page_1",
                            "name": "Page One",
                            "instagram_business_account": {
                                "id": "17841400000000001",
                                "username": "one.ig",
                                "followers_count": 10,
                                "media_count": 1,
                            },
                        },
                        {"id": "page_2", "name": "Page Two"},  # no linked IG account
                        {
                            "id": "page_3",
                            "name": "Page Three",
                            "instagram_business_account": {
                                "id": "17841400000000002",
                                "username": "three.ig",
                                "followers_count": 20,
                                "media_count": 2,
                            },
                        },
                    ]
                }
            )

        monkeypatch.setattr("xpst.connect.httpx.get", fake_get)
        accounts = _fetch_ig_business_accounts(PAGE_TOKEN)

        assert len(accounts) == 2
        assert accounts[0]["id"] == "17841400000000001"
        assert accounts[0]["username"] == "one.ig"
        assert accounts[0]["page_name"] == "Page One"
        assert accounts[1]["id"] == "17841400000000002"
        assert accounts[1]["media_count"] == 2

    def test_returns_empty_on_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_get(url: str, params: dict | None = None, timeout: float = 15.0) -> FakeResponse:
            return FakeResponse({"error": {}}, 500)

        monkeypatch.setattr("xpst.connect.httpx.get", fake_get)
        assert _fetch_ig_business_accounts("bad-token") == []
