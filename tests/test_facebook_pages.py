"""Facebook Page connector tests: Page discovery, token exchange, and refusal.

Every Meta Graph call here goes through ``httpx.MockTransport`` — no live
Facebook traffic, no tokens, no personal profiles. The stubbed responses use
Meta's real payload shapes (``/me/accounts`` with inline Page tokens, the
``oauth/access_token`` exchange, ``paging.next``) so the parsing that runs in
production is the parsing under test.

What is asserted, in the order the acceptance criteria state it:
- ``xpst auth facebook`` completes against a BYO Meta app (code exchange and
  pasted-token paths), storing the Page list + Page token encrypted;
- a missing Page is a clear, Page-naming error — never a personal-profile
  attempt (no ``/me/feed`` publish is ever issued);
- the provider declares its real capabilities (manifest == content contract).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from click.testing import CliRunner

import xpst.platforms.facebook as facebook_module
from xpst.cli import main
from xpst.config import XPSTConfig
from xpst.connect import FACEBOOK_ENV_VARS, disconnect_platform, facebook_auth
from xpst.content import (
    DESTINATION_CONTENT_PROFILES,
    ContentType,
    UnsupportedContentTypeError,
    content_support_status,
    validate_destination_content,
)
from xpst.platforms.base import DeleteOutcome, PlatformRegistry
from xpst.platforms.facebook import (
    FACEBOOK_CRED_KEYS,
    FacebookError,
    FacebookGraphClient,
    FacebookNotConfiguredError,
    FacebookPage,
    FacebookPageNotFoundError,
    FacebookPageRequiredError,
    FacebookPageUploader,
    public_page_listing,
    select_page,
)
from xpst.provider_truth import build_canonical_status, canonical_provider_catalog
from xpst.utils.credentials import CredentialStore

USER_TOKEN = "EAAG-user-token-not-a-real-secret"
PAGE_TOKEN = "EAAG-page-token-not-a-real-secret"
APP_ID = "1234567890123456"
APP_SECRET = "app-secret-not-real"
PAGE_A = "111111111111111"
PAGE_B = "222222222222222"
USER_ID = "999999999999999"


# ── Stubbed Graph API ───────────────────────────────────────────────────────


class GraphStub:
    """A fake graph.facebook.com: records every request, replays canned JSON.

    Args:
        pages: ``/me/accounts`` entries (raw Graph shape, Page tokens inline).
        user: ``/me`` payload.
        page_info: ``GET /{page_id}`` payload (name/followers).
        code_token: token returned by the authorization-code exchange.
        long_lived_token: token returned by ``fb_exchange_token``.
        empty_accounts: make ``/me/accounts`` return no Pages.
        error: ``(path_fragment, status, payload)`` to force one failing route.
        next_page: when set, the first ``/me/accounts`` response advertises this
            ``paging.next`` URL and the second returns the remaining Pages.
    """

    def __init__(
        self,
        *,
        pages: list[dict[str, Any]] | None = None,
        user: dict[str, Any] | None = None,
        page_info: dict[str, Any] | None = None,
        code_token: str = USER_TOKEN,
        long_lived_token: str = "EAAG-long-lived-user-token",
        empty_accounts: bool = False,
        error: tuple[str, int, dict[str, Any]] | None = None,
        next_page: str | None = None,
        inline_page_token: bool = True,
    ) -> None:
        self.pages = pages if pages is not None else [self.page_entry(PAGE_A, "Tyler's Test Page")]
        self.user = user if user is not None else {"id": USER_ID, "name": "Tyler"}
        self.page_info = page_info or {"id": PAGE_A, "name": "Tyler's Test Page", "followers_count": 42}
        self.code_token = code_token
        self.long_lived_token = long_lived_token
        self.empty_accounts = empty_accounts
        self.error = error
        self.next_page = next_page
        self.inline_page_token = inline_page_token
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def page_entry(page_id: str, name: str, token: str = PAGE_TOKEN) -> dict[str, Any]:
        return {
            "id": page_id,
            "name": name,
            "category": "Software",
            "tasks": ["MANAGE", "CREATE_CONTENT"],
            "link": f"https://www.facebook.com/{page_id}",
            "access_token": token,
        }

    # ── transport plumbing ────────────────────────────────────────────────

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self, **kwargs: Any) -> FacebookGraphClient:
        return FacebookGraphClient(client=httpx.Client(transport=self.transport()), **kwargs)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        params = dict(url.params)
        fields = dict(params)
        body = request.content or b""
        content_type = request.headers.get("content-type", "")
        if body and "application/x-www-form-urlencoded" in content_type:
            fields.update(
                {key: values[0] for key, values in parse_qs(body.decode("utf-8", "replace")).items()}
            )
        elif body and "multipart/form-data" in content_type:
            # Crude but sufficient: pull `name="x"\r\n\r\nVALUE` pairs out of the
            # multipart body so assertions can see the form fields we send.
            import re

            for name, value in re.findall(
                r'name="([^"]+)"\r\n\r\n([^\r]*)\r\n', body.decode("utf-8", "replace")
            ):
                fields[name] = value
        self.calls.append({"method": request.method, "path": url.path, "params": params, "fields": fields})

        if self.error is not None:
            fragment, status, payload = self.error
            if fragment in url.path:
                return httpx.Response(status, json=payload, request=request)

        path = url.path
        if path.endswith("/oauth/access_token"):
            if fields.get("grant_type") == "fb_exchange_token":
                return httpx.Response(200, json={"access_token": self.long_lived_token}, request=request)
            if "code" in fields:
                return httpx.Response(200, json={"access_token": self.code_token}, request=request)
            return httpx.Response(400, json={"error": {"message": "bad exchange"}}, request=request)

        if path.endswith("/me/accounts"):
            if self.empty_accounts:
                return httpx.Response(200, json={"data": []}, request=request)
            if self.next_page and "after=" not in str(url):
                first, rest = self.pages[:1], self.pages[1:]
                self._rest = rest
                return httpx.Response(
                    200,
                    json={"data": first, "paging": {"next": self.next_page}},
                    request=request,
                )
            rest = getattr(self, "_rest", None)
            if rest is not None:
                self._rest = None
                return httpx.Response(200, json={"data": rest}, request=request)
            return httpx.Response(200, json={"data": self.pages}, request=request)

        if path.endswith("/me"):
            return httpx.Response(200, json=self.user, request=request)

        page_id = path.rsplit("/", 1)[-1]
        if "access_token" in fields.get("fields", ""):
            token = PAGE_TOKEN if self.inline_page_token else ""
            return httpx.Response(200, json={"access_token": token, "id": page_id}, request=request)
        if path.endswith("/videos") or path.endswith("/photos") or path.endswith("/feed"):
            if request.method == "POST":
                return httpx.Response(200, json={"id": "post-1"}, request=request)
        if request.method == "DELETE":
            return httpx.Response(200, json={"success": True}, request=request)
        if page_id.isdigit():
            info = dict(self.page_info)
            info.setdefault("id", page_id)
            return httpx.Response(200, json=info, request=request)
        return httpx.Response(404, json={"error": {"message": "unknown route"}}, request=request)


@pytest.fixture
def stub_client(monkeypatch):
    """Return a factory that makes FacebookGraphClient instances hit a stub."""

    def make(stub: GraphStub, **kwargs: Any) -> FacebookGraphClient:
        monkeypatch.setattr(
            facebook_module, "_http_client", lambda proxy, timeout: httpx.Client(transport=stub.transport())
        )
        return FacebookGraphClient(**kwargs)

    return make


@pytest.fixture
def stub_graph_for_auth(monkeypatch):
    """Point the auth flow's internally-built client at a stub transport."""

    def install(stub: GraphStub) -> None:
        monkeypatch.setattr(
            facebook_module, "_http_client", lambda proxy, timeout: httpx.Client(transport=stub.transport())
        )

    return install


@pytest.fixture
def config(tmp_path: Path) -> XPSTConfig:
    cfg = XPSTConfig(config_dir=str(tmp_path))
    cfg.facebook.enabled = False
    return cfg


def _clear_facebook_env(monkeypatch) -> None:
    for var in FACEBOOK_ENV_VARS.values():
        monkeypatch.delenv(var, raising=False)


def _cli_json(result) -> dict[str, Any]:
    """Parse the JSON document from a CLI run (stdout, banner-tolerant)."""
    text = getattr(result, "stdout", "") or result.output
    start = text.find("{")
    assert start >= 0, f"no JSON document in CLI output: {text!r}"
    return json.loads(text[start:])


# ── OAuth: Facebook Login for Business ──────────────────────────────────────


def test_build_login_url_requests_the_page_scopes() -> None:
    url = FacebookGraphClient.build_login_url(APP_ID, "https://localhost/", state="csrf-1")
    assert url.startswith("https://www.facebook.com/v21.0/dialog/oauth?")
    query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
    assert query["client_id"] == APP_ID
    assert query["response_type"] == "code"
    assert query["state"] == "csrf-1"
    assert "pages_show_list" in query["scope"]
    assert "pages_manage_posts" in query["scope"]
    assert query["redirect_uri"] == "https://localhost/"


def test_build_login_url_prefers_a_login_configuration_over_scopes() -> None:
    url = FacebookGraphClient.build_login_url(APP_ID, "https://localhost/", config_id="cfg-7")
    assert "config_id=cfg-7" in url
    assert "scope=" not in url


def test_code_exchange_returns_a_user_token(stub_client) -> None:
    stub = GraphStub()
    client = stub_client(stub)
    payload = client.exchange_code_for_user_token(APP_ID, APP_SECRET, "https://localhost/", "CODE123")
    assert payload["access_token"] == USER_TOKEN
    call = stub.calls[0]
    assert call["params"]["code"] == "CODE123"
    assert call["params"]["redirect_uri"] == "https://localhost/"
    assert call["params"]["client_id"] == APP_ID


def test_code_exchange_error_surfaces_facebooks_own_message(stub_client) -> None:
    stub = GraphStub(
        error=(
            "/oauth/access_token",
            400,
            {"error": {"message": "Invalid verification code format.", "code": 100}},
        )
    )
    client = stub_client(stub)
    with pytest.raises(FacebookError) as excinfo:
        client.exchange_code_for_user_token(APP_ID, APP_SECRET, "https://localhost/", "BAD")
    assert "Invalid verification code format." in str(excinfo.value)
    assert "code 100" in str(excinfo.value)


def test_extend_user_token_uses_fb_exchange_token(stub_client) -> None:
    stub = GraphStub()
    client = stub_client(stub)
    payload = client.extend_user_token(APP_ID, APP_SECRET, USER_TOKEN)
    assert payload["access_token"] == "EAAG-long-lived-user-token"
    assert stub.calls[0]["params"]["grant_type"] == "fb_exchange_token"
    assert stub.calls[0]["params"]["fb_exchange_token"] == USER_TOKEN


@pytest.mark.parametrize(
    ("facebook_code", "expected_token"),
    [
        (190, "FACEBOOK_INVALID_TOKEN"),
        (102, "FACEBOOK_SESSION_INVALID"),
        (200, "FACEBOOK_PERMISSION_DENIED"),
        (10, "FACEBOOK_PERMISSION_DENIED"),
        (4, "FACEBOOK_RATE_LIMITED"),
        (613, "FACEBOOK_RATE_LIMITED"),
        (999, "FACEBOOK_ERROR"),  # unknown code stays generic, never invented
    ],
)
def test_graph_error_codes_map_to_stable_tokens(stub_client, facebook_code, expected_token) -> None:
    """Agents read ``error.code``; a rate limit must not look like a bad token."""
    stub = GraphStub(error=("/me", 400, {"error": {"message": "nope", "code": facebook_code}}))
    client = stub_client(stub)
    with pytest.raises(FacebookError) as excinfo:
        client.whoami(USER_TOKEN)
    assert excinfo.value.code == expected_token


def test_graph_error_message_carries_facebooks_code(stub_client) -> None:
    stub = GraphStub(
        error=("/me", 400, {"error": {"message": "Invalid OAuth access token.", "code": 190}})
    )
    client = stub_client(stub)
    with pytest.raises(FacebookError) as excinfo:
        client.whoami(USER_TOKEN)
    assert "Invalid OAuth access token." in str(excinfo.value)
    assert "code 190" in str(excinfo.value)


# ── Page discovery ──────────────────────────────────────────────────────────


def test_list_pages_parses_me_accounts_including_page_tokens(stub_client) -> None:
    stub = GraphStub(pages=[GraphStub.page_entry(PAGE_A, "Page A"), GraphStub.page_entry(PAGE_B, "Page B")])
    client = stub_client(stub)
    pages = client.list_pages(USER_TOKEN)
    assert [page.id for page in pages] == [PAGE_A, PAGE_B]
    assert all(page.access_token == PAGE_TOKEN for page in pages)
    assert pages[0].name == "Page A"
    assert pages[0].can_publish is True
    call = stub.calls[0]
    assert call["path"].endswith("/me/accounts")
    assert "access_token" in call["params"]["fields"]
    assert call["params"]["access_token"] == USER_TOKEN


def test_list_pages_follows_pagination(stub_client) -> None:
    next_url = f"https://graph.facebook.com/v21.0/me/accounts?after=CURSOR&access_token={USER_TOKEN}"
    stub = GraphStub(
        pages=[GraphStub.page_entry(PAGE_A, "Page A"), GraphStub.page_entry(PAGE_B, "Page B")],
        next_page=next_url,
    )
    client = stub_client(stub)
    pages = client.list_pages(USER_TOKEN)
    assert [page.id for page in pages] == [PAGE_A, PAGE_B]
    assert len(stub.calls) == 2


def test_list_pages_without_a_token_is_not_configured(stub_client) -> None:
    client = stub_client(GraphStub())
    with pytest.raises(FacebookNotConfiguredError):
        client.list_pages("")


def test_list_pages_with_no_pages_requires_a_page_never_a_profile(stub_client) -> None:
    client = stub_client(GraphStub(empty_accounts=True))
    with pytest.raises(FacebookPageRequiredError) as excinfo:
        client.list_pages(USER_TOKEN)
    message = str(excinfo.value)
    assert message.startswith("FACEBOOK_PAGE_REQUIRED")
    assert "personal profile" in message
    assert "no publishing API" in message


def test_public_page_listing_never_exposes_a_page_token(stub_client) -> None:
    stub = GraphStub()
    client = stub_client(stub)
    pages = client.list_pages(USER_TOKEN)
    listing = public_page_listing(pages)
    assert listing[0]["has_page_token"] is True
    assert "access_token" not in json.dumps(listing)
    assert PAGE_TOKEN not in json.dumps(listing)


def test_page_token_for_requests_the_token_when_not_inline(stub_client) -> None:
    stub = GraphStub(inline_page_token=True)
    client = stub_client(stub)
    token = client.page_token_for(PAGE_A, USER_TOKEN)
    assert token == PAGE_TOKEN
    assert stub.calls[0]["params"]["fields"] == "access_token"


def test_page_token_for_missing_token_is_an_explicit_error(stub_client) -> None:
    stub = GraphStub(inline_page_token=False)
    client = stub_client(stub)
    with pytest.raises(FacebookError) as excinfo:
        client.page_token_for(PAGE_A, USER_TOKEN)
    assert "FACEBOOK_PAGE_TOKEN_MISSING" in str(excinfo.value)


# ── Page selection ──────────────────────────────────────────────────────────


def test_select_page_uses_the_only_page() -> None:
    only = FacebookPage(id=PAGE_A, name="Only Page", access_token=PAGE_TOKEN)
    assert select_page([only]) is only


def test_select_page_honours_an_explicit_id() -> None:
    pages = [
        FacebookPage(id=PAGE_A, name="A", access_token=PAGE_TOKEN),
        FacebookPage(id=PAGE_B, name="B", access_token=PAGE_TOKEN),
    ]
    assert select_page(pages, PAGE_B).id == PAGE_B


def test_select_page_rejects_an_unknown_id_and_lists_the_available_pages() -> None:
    pages = [FacebookPage(id=PAGE_A, name="A", access_token=PAGE_TOKEN)]
    with pytest.raises(FacebookPageNotFoundError) as excinfo:
        select_page(pages, "555")
    message = str(excinfo.value)
    assert message.startswith("FACEBOOK_PAGE_NOT_FOUND")
    assert PAGE_A in message
    assert "never publishes as a personal profile" in message


def test_select_page_refuses_to_guess_between_several_pages() -> None:
    pages = [
        FacebookPage(id=PAGE_A, name="A", access_token=PAGE_TOKEN),
        FacebookPage(id=PAGE_B, name="B", access_token=PAGE_TOKEN),
    ]
    with pytest.raises(FacebookPageRequiredError) as excinfo:
        select_page(pages)
    message = str(excinfo.value)
    assert "administers 2 Pages" in message
    assert "never guesses" in message


def test_select_page_with_no_pages_is_an_explicit_requirement() -> None:
    with pytest.raises(FacebookPageRequiredError):
        select_page([])


# ── Auth flow: xpst auth facebook ───────────────────────────────────────────


def test_facebook_auth_stores_page_and_token_encrypted(config, stub_graph_for_auth, monkeypatch) -> None:
    stub = GraphStub()
    stub_graph_for_auth(stub)
    _clear_facebook_env(monkeypatch)
    monkeypatch.setenv("XPST_FACEBOOK_USER_TOKEN", USER_TOKEN)
    monkeypatch.setenv("XPST_FACEBOOK_APP_ID", APP_ID)
    monkeypatch.setenv("XPST_FACEBOOK_APP_SECRET", APP_SECRET)

    report = facebook_auth(config, as_json=True)

    assert report["success"] is True
    assert report["page"]["id"] == PAGE_A
    assert report["user"]["id"] == USER_ID
    assert report["auth_mode"] == "user_token"
    assert [page["id"] for page in report["pages"]] == [PAGE_A]

    # The user token was extended and the Page token verified before storing.
    steps = {step["step"]: step["ok"] for step in report["steps"]}
    assert steps["token_extension"] is True
    assert steps["user_token_verified"] is True
    assert steps["pages_listed"] is True
    assert steps["page_token_verified"] is True
    assert steps["stored"] is True

    # Encrypted credential store is the real credential.
    store = CredentialStore(config.config_dir)
    assert store.retrieve(FACEBOOK_CRED_KEYS["page_id"]) == PAGE_A
    assert store.retrieve(FACEBOOK_CRED_KEYS["page_access_token"]) == PAGE_TOKEN
    assert store.retrieve(FACEBOOK_CRED_KEYS["app_secret"]) == APP_SECRET

    # Config carries the selection and is enabled.
    assert config.facebook.enabled is True
    assert config.facebook.page_id == PAGE_A
    assert config.facebook.page_access_token == PAGE_TOKEN
    assert config.facebook.app_id == APP_ID
    assert (Path(config.config_dir) / "config.yaml").exists()


def test_facebook_auth_never_returns_a_token_in_its_report(config, stub_graph_for_auth, monkeypatch) -> None:
    stub_graph_for_auth(GraphStub())
    _clear_facebook_env(monkeypatch)
    monkeypatch.setenv("XPST_FACEBOOK_USER_TOKEN", USER_TOKEN)

    report = facebook_auth(config, as_json=True)
    serialized = json.dumps(report)
    assert PAGE_TOKEN not in serialized
    assert USER_TOKEN not in serialized
    assert APP_SECRET not in serialized


def test_facebook_auth_code_exchange_path(config, stub_graph_for_auth, monkeypatch) -> None:
    stub = GraphStub(code_token=USER_TOKEN)
    stub_graph_for_auth(stub)
    _clear_facebook_env(monkeypatch)
    monkeypatch.setenv("XPST_FACEBOOK_APP_ID", APP_ID)
    monkeypatch.setenv("XPST_FACEBOOK_APP_SECRET", APP_SECRET)
    monkeypatch.setenv(
        "XPST_FACEBOOK_OAUTH_CODE",
        "https://localhost/?code=AQD-test-code#_=_",
    )

    report = facebook_auth(config, as_json=True)
    assert report["success"] is True
    assert report["auth_mode"] == "oauth_code"
    steps = {step["step"]: step["ok"] for step in report["steps"]}
    assert steps["code_exchange"] is True
    exchanged = [call for call in stub.calls if call["fields"].get("code")]
    assert exchanged and exchanged[0]["fields"]["code"] == "AQD-test-code"


def test_facebook_auth_code_mode_requires_app_credentials(config, stub_graph_for_auth, monkeypatch) -> None:
    stub_graph_for_auth(GraphStub())
    _clear_facebook_env(monkeypatch)
    monkeypatch.setenv("XPST_FACEBOOK_OAUTH_CODE", "AQD-test-code")

    report = facebook_auth(config, as_json=True)
    assert report["success"] is False
    assert report["error"].startswith("FACEBOOK_APP_REQUIRED")
    assert config.facebook.enabled is False


def test_facebook_auth_without_credentials_fails_cleanly_not_interactively(
    config, stub_graph_for_auth, monkeypatch
) -> None:
    """No TTY, no env token: a precise error, no prompt, no stored credential."""
    stub_graph_for_auth(GraphStub())
    _clear_facebook_env(monkeypatch)

    report = facebook_auth(config, as_json=True, interactive=False)
    assert report["success"] is False
    assert report["error"].startswith("FACEBOOK_CREDENTIALS_REQUIRED")
    assert "XPST_FACEBOOK_USER_TOKEN" in report["hint"]
    assert config.facebook.page_id == ""
    assert CredentialStore(config.config_dir).retrieve(FACEBOOK_CRED_KEYS["page_access_token"]) is None


def test_facebook_auth_with_no_pages_stores_nothing_and_never_tries_a_profile(
    config, stub_graph_for_auth, monkeypatch
) -> None:
    stub = GraphStub(empty_accounts=True)
    stub_graph_for_auth(stub)
    _clear_facebook_env(monkeypatch)
    monkeypatch.setenv("XPST_FACEBOOK_USER_TOKEN", USER_TOKEN)

    report = facebook_auth(config, as_json=True)
    assert report["success"] is False
    assert report["error"].startswith("FACEBOOK_PAGE_REQUIRED")
    assert report["pages"] == []
    assert config.facebook.enabled is False
    assert config.facebook.page_id == ""
    # The personal-profile publish endpoint is never touched.
    assert all("/me/feed" not in call["path"] for call in stub.calls)
    assert all(not call["path"].endswith("/me/videos") for call in stub.calls)


def test_facebook_auth_unknown_page_id_is_a_clear_error(config, stub_graph_for_auth, monkeypatch) -> None:
    stub_graph_for_auth(GraphStub(pages=[GraphStub.page_entry(PAGE_A, "Page A")]))
    _clear_facebook_env(monkeypatch)
    monkeypatch.setenv("XPST_FACEBOOK_USER_TOKEN", USER_TOKEN)
    monkeypatch.setenv("XPST_FACEBOOK_PAGE_ID", "555555555555")

    report = facebook_auth(config, as_json=True)
    assert report["success"] is False
    assert report["error"].startswith("FACEBOOK_PAGE_NOT_FOUND")
    assert PAGE_A in report["error"]
    assert config.facebook.enabled is False


def test_facebook_auth_selects_the_requested_page(config, stub_graph_for_auth, monkeypatch) -> None:
    stub = GraphStub(
        pages=[
            GraphStub.page_entry(PAGE_A, "Page A"),
            GraphStub.page_entry(PAGE_B, "Page B", token="EAAG-page-b-token"),
        ],
        page_info={"id": PAGE_B, "name": "Page B", "followers_count": 7},
    )
    stub_graph_for_auth(stub)
    _clear_facebook_env(monkeypatch)
    monkeypatch.setenv("XPST_FACEBOOK_USER_TOKEN", USER_TOKEN)
    monkeypatch.setenv("XPST_FACEBOOK_PAGE_ID", PAGE_B)

    report = facebook_auth(config, as_json=True)
    assert report["success"] is True
    assert report["page"]["id"] == PAGE_B
    assert len(report["pages"]) == 2
    assert CredentialStore(config.config_dir).retrieve(FACEBOOK_CRED_KEYS["page_access_token"]) == (
        "EAAG-page-b-token"
    )


def test_facebook_auth_rejects_a_page_token_for_another_page(config, stub_graph_for_auth, monkeypatch) -> None:
    """A token that resolves to a different Page is a mismatch, not a success."""
    stub = GraphStub(page_info={"id": "777777777777", "name": "Someone else"})
    stub_graph_for_auth(stub)
    _clear_facebook_env(monkeypatch)
    monkeypatch.setenv("XPST_FACEBOOK_USER_TOKEN", USER_TOKEN)

    report = facebook_auth(config, as_json=True)
    assert report["success"] is False
    assert report["error"].startswith("FACEBOOK_PAGE_MISMATCH")
    assert config.facebook.enabled is False


def test_facebook_auth_invalid_user_token_is_reported(config, stub_graph_for_auth, monkeypatch) -> None:
    stub = GraphStub(
        error=("/me", 400, {"error": {"message": "Invalid OAuth access token.", "code": 190}})
    )
    stub_graph_for_auth(stub)
    _clear_facebook_env(monkeypatch)
    monkeypatch.setenv("XPST_FACEBOOK_USER_TOKEN", "expired")

    report = facebook_auth(config, as_json=True)
    assert report["success"] is False
    assert report["error"].startswith("FACEBOOK_USER_TOKEN_INVALID")
    assert "Invalid OAuth access token." in report["error"]


# ── Adapter: upload / health / delete ───────────────────────────────────────


def _configured(config: XPSTConfig) -> FacebookPageUploader:
    config.facebook.enabled = True
    config.facebook.page_id = PAGE_A
    config.facebook.page_access_token = PAGE_TOKEN
    return FacebookPageUploader(config)


def test_upload_publishes_to_the_page_not_the_profile(config, stub_client) -> None:
    import asyncio

    stub = GraphStub()
    uploader = _configured(config)
    uploader._client = lambda: stub.client()  # type: ignore[method-assign]

    video = Path(config.config_dir) / "clip.mp4"
    video.write_bytes(b"not-a-real-video")

    result = asyncio.run(uploader.upload(video, "hello page"))
    assert result.success is True
    assert result.post_id == "post-1"
    assert result.post_url.startswith(f"https://www.facebook.com/{PAGE_A}/")
    assert result.metadata["page_id"] == PAGE_A

    publish_calls = [call for call in stub.calls if call["method"] == "POST"]
    assert publish_calls, "the publish call must have been issued"
    assert all(call["path"].endswith(f"/{PAGE_A}/videos") for call in publish_calls)
    assert all("/me/" not in call["path"] for call in publish_calls)
    assert all(call["fields"]["access_token"] == PAGE_TOKEN for call in publish_calls)


def test_upload_without_credentials_is_a_typed_error(config) -> None:
    import asyncio

    uploader = FacebookPageUploader(config)
    result = asyncio.run(uploader.upload(Path("/tmp/does-not-exist.mp4"), "x"))
    assert result.success is False
    assert result.error.startswith("FACEBOOK_NOT_CONFIGURED")


def test_check_health_verifies_the_page_identity(config, stub_client) -> None:
    import asyncio

    stub = GraphStub()
    uploader = _configured(config)
    uploader._client = lambda: stub.client()  # type: ignore[method-assign]

    health = asyncio.run(uploader.check_health())
    assert health.authenticated is True
    assert health.session_valid is True
    assert health.details["page_id"] == PAGE_A
    assert health.details["page_scoped"] is True


def test_check_health_flags_a_page_token_mismatch(config, stub_client) -> None:
    import asyncio

    stub = GraphStub(page_info={"id": "777777777777", "name": "Other"})
    uploader = _configured(config)
    uploader._client = lambda: stub.client()  # type: ignore[method-assign]

    health = asyncio.run(uploader.check_health())
    assert health.authenticated is False
    assert "FACEBOOK_PAGE_MISMATCH" in (health.error or "")


def test_check_health_without_credentials_is_not_authenticated(config) -> None:
    import asyncio

    health = asyncio.run(FacebookPageUploader(config).check_health())
    assert health.authenticated is False
    assert (health.error or "").startswith("FACEBOOK_NOT_CONFIGURED")


def test_delete_hard_deletes_a_page_post(config, stub_client) -> None:
    import asyncio

    stub = GraphStub()
    uploader = _configured(config)
    uploader._client = lambda: stub.client()  # type: ignore[method-assign]

    result = asyncio.run(uploader.delete("post-1"))
    assert result.outcome is DeleteOutcome.DELETED
    assert result.ok is True


def test_delete_failure_is_pending_not_a_fake_success(config, stub_client) -> None:
    import asyncio

    stub = GraphStub(error=("/post-1", 403, {"error": {"message": "Permissions error", "code": 200}}))
    uploader = _configured(config)
    uploader._client = lambda: stub.client()  # type: ignore[method-assign]

    result = asyncio.run(uploader.delete("post-1"))
    assert result.outcome is DeleteOutcome.PENDING
    assert result.ok is False


def test_adapter_reads_the_page_token_from_the_encrypted_store(config) -> None:
    import asyncio

    store = CredentialStore(config.config_dir)
    store.store(FACEBOOK_CRED_KEYS["page_id"], PAGE_A)
    store.store(FACEBOOK_CRED_KEYS["page_access_token"], PAGE_TOKEN)

    class Manager:
        async def get_facebook_page_credentials(self):
            return {"page_id": PAGE_A, "page_access_token": PAGE_TOKEN}

    uploader = FacebookPageUploader(config)
    uploader._session_manager = Manager()
    assert asyncio.run(uploader._credentials()) == (PAGE_A, PAGE_TOKEN)


# ── Capability declarations ─────────────────────────────────────────────────


def test_manifest_declares_only_what_the_engine_can_publish() -> None:
    PlatformRegistry.auto_discover()
    manifest = FacebookPageUploader(XPSTConfig()).manifest
    assert manifest.name == "facebook"
    assert manifest.auth_mode.value == "oauth"
    assert manifest.extra["content"] == ("video",)
    assert manifest.extra["page_scoped"] is True
    capabilities = {capability.value for capability in manifest.capabilities}
    assert {"upload", "delete", "health", "official_api", "oauth"} <= capabilities


def test_manifest_declared_content_matches_the_content_contract() -> None:
    profile = DESTINATION_CONTENT_PROFILES["facebook"]
    assert profile.declared_labels == ("video",)
    assert profile.declared == frozenset({ContentType.VIDEO})
    assert profile.implemented == frozenset({ContentType.VIDEO})
    assert profile.declared_but_unimplemented == frozenset()


def test_content_matrix_declares_facebook_video_only() -> None:
    assert content_support_status("facebook", ContentType.VIDEO) == "supported"
    for unsupported in (ContentType.IMAGE, ContentType.CAROUSEL, ContentType.TEXT, ContentType.THREAD):
        assert content_support_status("facebook", unsupported) == "unsupported"


def test_unsupported_content_type_is_refused_by_name() -> None:
    with pytest.raises(UnsupportedContentTypeError) as excinfo:
        validate_destination_content("facebook", ContentType.TEXT)
    assert "facebook" in str(excinfo.value)


def test_provider_truth_registers_facebook_as_a_page_scoped_destination(config) -> None:
    status = build_canonical_status(config)
    assert "facebook" in status
    assert set(status["facebook"]["roles"]) == {"video_destination"}
    catalog = canonical_provider_catalog(config)
    entry = catalog["by_name"]["facebook"]
    assert entry["is_official_api"] is True
    assert entry["docs_url"] == "https://developers.facebook.com/docs/pages-api"
    assert "upload" in entry["capabilities"]


def test_provider_truth_reports_facebook_disabled_until_enabled(config) -> None:
    config.facebook.enabled = False
    assert build_canonical_status(config)["facebook"]["state"] == "disabled"
    config.facebook.enabled = True
    config.facebook.page_id = PAGE_A
    config.facebook.page_access_token = PAGE_TOKEN
    assert build_canonical_status(config)["facebook"]["state"] == "ready"


# ── CLI + disconnect ────────────────────────────────────────────────────────


def test_cli_auth_facebook_json_completes(tmp_path: Path, stub_graph_for_auth, monkeypatch) -> None:
    stub_graph_for_auth(GraphStub())
    _clear_facebook_env(monkeypatch)
    monkeypatch.setenv("XPST_FACEBOOK_USER_TOKEN", USER_TOKEN)
    config_file = tmp_path / "config.yaml"
    XPSTConfig(config_dir=str(tmp_path)).save(str(config_file))

    result = CliRunner().invoke(main, ["--config", str(config_file), "auth", "facebook", "--json"])
    assert result.exit_code == 0, result.output
    payload = _cli_json(result)
    assert payload["success"] is True
    assert payload["page"]["id"] == PAGE_A
    assert PAGE_TOKEN not in result.output


def test_cli_auth_facebook_exits_non_zero_when_no_page(
    tmp_path: Path, stub_graph_for_auth, monkeypatch
) -> None:
    stub_graph_for_auth(GraphStub(empty_accounts=True))
    _clear_facebook_env(monkeypatch)
    monkeypatch.setenv("XPST_FACEBOOK_USER_TOKEN", USER_TOKEN)
    config_file = tmp_path / "config.yaml"
    XPSTConfig(config_dir=str(tmp_path)).save(str(config_file))

    result = CliRunner().invoke(main, ["--config", str(config_file), "auth", "facebook", "--json"])
    assert result.exit_code != 0
    payload = _cli_json(result)
    assert payload["error"].startswith("FACEBOOK_PAGE_REQUIRED")


def test_cli_auth_rejects_an_unknown_platform() -> None:
    result = CliRunner().invoke(main, ["auth", "friendster"])
    assert result.exit_code != 0
    assert "Unknown platform" in result.output


def test_disconnect_facebook_removes_credentials_and_disables(config) -> None:
    store = CredentialStore(config.config_dir)
    store.store(FACEBOOK_CRED_KEYS["page_id"], PAGE_A)
    store.store(FACEBOOK_CRED_KEYS["page_access_token"], PAGE_TOKEN)
    config.facebook.enabled = True
    config.facebook.page_id = PAGE_A
    config.facebook.page_access_token = PAGE_TOKEN

    result = disconnect_platform("facebook", config)

    assert result["success"] is True
    assert result["disabled"] is True
    assert config.facebook.enabled is False
    assert store.retrieve(FACEBOOK_CRED_KEYS["page_access_token"]) is None
    assert store.retrieve(FACEBOOK_CRED_KEYS["page_id"]) is None


def test_session_manager_round_trips_facebook_credentials(config) -> None:
    import asyncio

    from xpst.utils.sessions import SessionManager

    manager = SessionManager(config.config_dir)
    assert asyncio.run(manager.get_facebook_page_credentials()) is None
    asyncio.run(
        manager.store_facebook_credentials(PAGE_A, PAGE_TOKEN, user_token=USER_TOKEN, app_id=APP_ID)
    )
    stored = asyncio.run(manager.get_facebook_page_credentials())
    assert stored == {"page_id": PAGE_A, "page_access_token": PAGE_TOKEN}
