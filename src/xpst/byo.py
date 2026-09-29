"""Bring-your-own developer app: per-platform app credentials that stay local.

Why this module exists
----------------------
Meta's **Standard Access** means an app may be used, unreviewed, by people who
hold a role on the app. The person who creates their own Meta app *is* its
owner, so publishing to their own Instagram account, Threads profile or
Facebook Page needs **no App Review and no Business Verification** — the
gate that blocks every multi-tenant third-party tool simply does not apply.
TikTok's Content Posting API has the same self-serve shape: the user registers
their own client key/secret (with stricter ToS limits, documented in
docs/setup-byo-app.md).

So xPST ships no shared multi-tenant app and asks for none. The user creates a
provider app themselves and pastes its id/secret into the per-platform setup
screen (UI, ``xpst byo set``, MCP ``xpst_byo_app``, or an env var). The
credential:

* is stored ONLY under the user's config dir via the encrypted
  :class:`~xpst.utils.credentials.CredentialStore` (Fernet file store by
  default) — never synced, never committed, never placed in build output;
* is never echoed back in full — every surface sees a masked tail
  (``…1234``) plus boolean flags;
* is what flips a platform into the official API path: ``resolve_byo_app``
  returning a configured app is the single source of truth the sign-in
  providers, the canonical catalog and the UI all read from.

TikTok's app credentials predate this module (``accounts.tiktok.client_key``
+ the ``tiktok_client_secret`` store key); they are folded into the same
view so one screen covers every BYO platform.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "BYO_PLATFORMS",
    "ByoAppError",
    "byo_status",
    "byo_status_for_platform",
    "clear_byo_app",
    "mask_app_id",
    "resolve_byo_app",
    "store_byo_app",
    "validate_byo_app",
    "finalize_meta_oauth_code",
]

#: Meta's in-app sign-in redirect. Meta accepts ``http://localhost:<port>``
#: loopback redirects for apps in Development mode, exactly like Google does
#: for Desktop clients; a user may register any port, xPST just asks for
#: this one. Kept in one place because the setup screen, the sign-in
#: provider and the docs must all state the same value.
META_LOOPBACK_REDIRECT = "http://localhost:8888/callback"
#: The port/path the loopback transport binds so the URI above matches.
META_REDIRECT_PORT = 8888
META_REDIRECT_PATH = "/callback"

_APP_ID_RE = re.compile(r"^[0-9]{4,24}$")
_TIKTOK_KEY_RE = re.compile(r"^aw[0-9a-z]{6,32}$", re.IGNORECASE)
_SECRET_RE = re.compile(r"^[A-Za-z0-9_\-\.]{8,256}$")


class ByoAppError(ValueError):
    """A BYO app credential was refused, with per-field messages for the UI."""

    def __init__(self, message: str, *, errors: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.errors = dict(errors or {})


@dataclass(frozen=True)
class ByoPlatformSpec:
    """Everything one platform needs to support the BYO path.

    Attributes:
        platform: Canonical provider name.
        display_name: Label for setup screens.
        id_label / secret_label: Field labels (TikTok calls them client key).
        config_id_field / config_secret_field: Attribute names on the
            matching ``XPSTConfig`` account section (may be empty when the
            platform has no config pair, e.g. shared Meta storage).
        store_id_key / store_secret_key: CredentialStore keys (encrypted).
        redirect_uri: What the user must register on the provider app.
        scopes: OAuth scopes xPST requests for this platform.
        create_url / docs_url: Provider app-creation pointers.
        enables: One-sentence statement of what the credential unlocks.
        shared_meta: True when this platform reads the shared ``byo_meta``
            store keys (Instagram/Threads/Messenger are all products of one
            Meta app); False when it stores its own pair.
        configurable: False = status-only surface (the platform already has a
            dedicated connect flow), so setup screens hide its editor.
    """

    platform: str
    display_name: str
    id_label: str
    secret_label: str
    store_id_key: str
    store_secret_key: str
    redirect_uri: str
    scopes: tuple[str, ...]
    create_url: str
    docs_url: str
    enables: str
    config_id_field: str = ""
    config_secret_field: str = ""
    shared_meta: bool = False
    configurable: bool = True
    env_id_var: str = ""
    env_secret_var: str = ""
    env_fields: Mapping[str, str] = field(default_factory=dict)


_META_CREATE_URL = "https://developers.facebook.com/apps"
_META_BYO_DOCS = "https://developers.facebook.com/docs/graph-api/get-started/getting-access-tokens/about-user-access-tokens#standard-access"
_TIKTOK_CREATE_URL = "https://developers.tiktok.com/manage/apps"
_TIKTOK_DOCS = "https://developers.tiktok.com/doc/content-posting-api-get-started"

#: The single registry. Instagram and Threads share one Meta app (one Meta
#: developer app carries both products), so both read/write ``byo_meta_*``;
#: Messenger is a read-only status surface on the same keys; TikTok stores
#: its own pair and already has a destination connector.
BYO_PLATFORMS: dict[str, ByoPlatformSpec] = {
    "instagram": ByoPlatformSpec(
        platform="instagram",
        display_name="Instagram Reels",
        id_label="Meta App ID",
        secret_label="Meta App Secret",
        config_id_field="app_id",
        config_secret_field="app_secret",
        store_id_key="byo_meta_app_id",
        store_secret_key="byo_meta_app_secret",
        redirect_uri=META_LOOPBACK_REDIRECT,
        scopes=(
            "instagram_basic",
            "instagram_content_publish",
            "pages_show_list",
            "pages_read_engagement",
        ),
        create_url=_META_CREATE_URL,
        docs_url=_META_BYO_DOCS,
        enables=(
            "Publishing Reels to your own account through the official Instagram "
            "Platform API. As the app owner you stay in Meta's Standard Access: no "
            "App Review and no Business Verification."
        ),
        shared_meta=True,
        env_id_var="XPST_INSTAGRAM_APP_ID",
        env_secret_var="XPST_INSTAGRAM_APP_SECRET",
    ),
    "threads": ByoPlatformSpec(
        platform="threads",
        display_name="Threads",
        id_label="Meta App ID",
        secret_label="Meta App Secret",
        config_id_field="app_id",
        config_secret_field="app_secret",
        store_id_key="byo_meta_app_id",
        store_secret_key="byo_meta_app_secret",
        redirect_uri=META_LOOPBACK_REDIRECT,
        scopes=("threads_basic", "threads_content_publish"),
        create_url=_META_CREATE_URL,
        docs_url="https://developers.facebook.com/docs/threads/get-started/getting-started#auth",
        enables=(
            "Publishing posts to your own Threads account through the official "
            "Threads API. Same Meta app as Instagram; owner use is Standard "
            "Access: no App Review, no Business Verification."
        ),
        shared_meta=True,
        env_id_var="XPST_THREADS_APP_ID",
        env_secret_var="XPST_THREADS_APP_SECRET",
    ),
    "messenger": ByoPlatformSpec(
        platform="messenger",
        display_name="Messenger",
        id_label="Meta App ID",
        secret_label="Meta App Secret",
        store_id_key="messenger_app_id",
        store_secret_key="messenger_app_secret",
        redirect_uri="",
        scopes=(),
        create_url=_META_CREATE_URL,
        docs_url="https://developers.facebook.com/docs/messenger-platform/get-started",
        enables=(
            "Messenger auto-reply and DM features on your Page. Messenger keeps "
            "its own connect flow; this status line shows the same Meta app."
        ),
        configurable=False,
        env_id_var="XPST_MESSENGER_APP_ID",
        env_secret_var="XPST_MESSENGER_APP_SECRET",
    ),
    "facebook": ByoPlatformSpec(
        platform="facebook",
        display_name="Facebook Page",
        id_label="Meta App ID",
        secret_label="Meta App Secret",
        store_id_key="facebook_app_id",
        store_secret_key="facebook_app_secret",
        redirect_uri="https://localhost/",
        scopes=("pages_show_list", "pages_read_engagement", "pages_manage_posts"),
        create_url=_META_CREATE_URL,
        docs_url="https://developers.facebook.com/docs/facebook-login",
        enables=(
            "Page-scoped publishing through the Graph API. Facebook keeps its own "
            "``xpst auth facebook`` flow; this status line shows its stored app."
        ),
        configurable=False,
        env_id_var="XPST_FACEBOOK_APP_ID",
        env_secret_var="XPST_FACEBOOK_APP_SECRET",
    ),
    "tiktok": ByoPlatformSpec(
        platform="tiktok",
        display_name="TikTok",
        id_label="Client Key",
        secret_label="Client Secret",
        config_id_field="client_key",
        config_secret_field="client_secret",
        store_id_key="tiktok_client_key",
        store_secret_key="tiktok_client_secret",
        redirect_uri="http://localhost:8085/callback",
        scopes=(
            "video.publish",
            "video.upload",
            "user.info.stats",
        ),
        create_url=_TIKTOK_CREATE_URL,
        docs_url=_TIKTOK_DOCS,
        enables=(
            "Direct-posting to your own TikTok account through the official Content "
            "Posting API. Unaudited apps post SELF_ONLY (private) until TikTok "
            "audits the app — xPST never claims public reach it does not have."
        ),
        env_id_var="XPST_TIKTOK_CLIENT_KEY",
        env_secret_var="XPST_TIKTOK_CLIENT_SECRET",
    ),
}


def mask_app_id(app_id: str) -> str:
    """Never echo an identifier in full: show at most its last four chars."""
    text = str(app_id or "")
    if not text:
        return ""
    if len(text) <= 4:
        return "•" * len(text)
    return f"…{text[-4:]}"


def _account_field(config: Any, spec: ByoPlatformSpec, which: str) -> str:
    field_name = getattr(spec, which)
    if not field_name:
        return ""
    section = getattr(config, spec.platform, None)
    return str(getattr(section, field_name, "") or "")


def validate_byo_app(
    platform: str,
    app_id: str,
    app_secret: str,
    *,
    require_secret: bool = True,
) -> tuple[str, str]:
    """Validate and normalise one BYO credential pair.

    Returns the stripped (app_id, app_secret). Raises :class:`ByoAppError`
    with per-field messages so a form can label exactly what is wrong.
    """
    spec = BYO_PLATFORMS.get(str(platform).strip().lower())
    if spec is None:
        raise ByoAppError(f"Unknown platform for BYO setup: {platform}")
    app_id = str(app_id or "").strip()
    app_secret = str(app_secret or "").strip()
    errors: dict[str, str] = {}

    id_pattern = _TIKTOK_KEY_RE if spec.platform == "tiktok" else _APP_ID_RE
    if not app_id:
        errors["app_id"] = f"{spec.id_label} is required."
    elif not id_pattern.match(app_id):
        expected = "starts with 'aw' followed by letters/digits" if spec.platform == "tiktok" else "must be digits"
        errors["app_id"] = f"{spec.id_label} looks wrong ({expected})."

    if require_secret or app_secret:
        if not app_secret:
            errors["app_secret"] = f"{spec.secret_label} is required."
        elif not _SECRET_RE.match(app_secret):
            errors["app_secret"] = f"{spec.secret_label} looks wrong (8–256 chars, no spaces)."

    if errors:
        raise ByoAppError("Those app credentials were not accepted.", errors=errors)
    return app_id, app_secret


def _store_for(config: Any) -> Any:
    from pathlib import Path

    from xpst.utils.credentials import CredentialStore

    config_dir = str(getattr(config, "config_dir", "") or "~/.xpst")
    return CredentialStore(str(Path(config_dir).expanduser()))


def store_byo_app(
    config: Any,
    platform: str,
    app_id: str,
    app_secret: str,
    *,
    store: Any | None = None,
) -> dict[str, Any]:
    """Validate, store encrypted, and return the masked view of a BYO app.

    The secret is written ONLY to the CredentialStore (never to config.yaml
    by this path), so a plaintext config file keeps holding at most the
    non-secret app id.
    """
    spec = BYO_PLATFORMS.get(str(platform).strip().lower())
    if spec is None:
        raise ByoAppError(f"Unknown platform for BYO setup: {platform}")
    app_id, app_secret = validate_byo_app(spec.platform, app_id, app_secret)

    cred_store = store if store is not None else _store_for(config)
    try:
        cred_store.store(spec.store_id_key, app_id)
        cred_store.store(spec.store_secret_key, app_secret)
    except Exception as exc:  # noqa: BLE001 - surface WHY, never a half-store
        raise ByoAppError(
            f"Could not store the app credential encrypted: {type(exc).__name__}: {exc}"
        ) from None

    # The app id is not a secret; keeping it on the config section lets any
    # surface show WHICH app is in play without decrypting anything. The
    # secret deliberately does NOT go into config.yaml from this path.
    if spec.config_id_field:
        section = getattr(config, spec.platform, None)
        if section is not None:
            setattr(section, spec.config_id_field, app_id)
        save = getattr(config, "save", None)
        if callable(save):
            save()

    # An in-process overlay so a caller that stores then signs in within the
    # same process sees the secret without a re-read (resolve_* still reads
    # the store; this only mirrors it in memory).
    if spec.config_secret_field:
        section = getattr(config, spec.platform, None)
        if section is not None:
            setattr(section, spec.config_secret_field, app_secret)

    return byo_status_for_platform(config, spec.platform, store=store)


def clear_byo_app(config: Any, platform: str, *, store: Any | None = None) -> dict[str, Any]:
    """Remove a platform's stored app credentials (and the config mirror)."""
    spec = BYO_PLATFORMS.get(str(platform).strip().lower())
    if spec is None:
        raise ByoAppError(f"Unknown platform for BYO setup: {platform}")
    cred_store = store if store is not None else _store_for(config)
    removed = [key for key in (spec.store_id_key, spec.store_secret_key) if cred_store.delete(key)]

    if spec.config_id_field:
        section = getattr(config, spec.platform, None)
        if section is not None:
            setattr(section, spec.config_id_field, "")
            if spec.config_secret_field:
                setattr(section, spec.config_secret_field, "")
            save = getattr(config, "save", None)
            if callable(save):
                save()
    return {
        "platform": spec.platform,
        "configured": False,
        "removed": removed,
        "sign_in_available": False,
        "docs_url": spec.docs_url,
        "note": f"{spec.display_name} no longer has an app on this machine; sign-in is off until one is added.",
    }


def resolve_byo_app(
    config: Any, platform: str, *, store: Any | None = None
) -> tuple[str, str, str] | None:
    """Return ``(app_id, app_secret, source)`` for a platform's BYO app.

    Precedence: the encrypted CredentialStore first (the UI/API/MCP write
    path), then the config file fields, then process env (which config load
    has already applied to the config fields). ``None`` means the platform
    has no app on this machine.
    """
    spec = BYO_PLATFORMS.get(str(platform).strip().lower())
    if spec is None:
        return None

    # Only read the encrypted store when the config names its home (or a
    # store was injected). A bare/partial config object must not silently
    # probe the real ~/.xpst of whoever runs the process.
    cred_store = store if store is not None else (
        _store_for(config) if getattr(config, "config_dir", "") else None
    )
    app_id = app_secret = ""
    if cred_store is not None:
        try:
            app_id = str(cred_store.retrieve(spec.store_id_key) or "")
            app_secret = str(cred_store.retrieve(spec.store_secret_key) or "")
        except Exception:  # noqa: BLE001 - an unreadable store must not crash status
            app_id = app_secret = ""
    if app_id and app_secret:
        return app_id, app_secret, "encrypted-store"

    config_id = _account_field(config, spec, "config_id_field")
    config_secret = _account_field(config, spec, "config_secret_field")
    if config_id and config_secret:
        return config_id, config_secret, "config"
    if config_id and not app_id:
        # id without secret: still surface it so status can explain what is
        # half-configured; callers treat secret presence as the gate.
        return config_id, "", "config-incomplete"
    return None


def byo_status_for_platform(
    config: Any, platform: str, *, store: Any | None = None
) -> dict[str, Any]:
    """Masked, secret-free status for one platform (safe for every surface)."""
    spec = BYO_PLATFORMS[str(platform).strip().lower()]
    resolved = resolve_byo_app(config, spec.platform, store=store)
    configured = bool(resolved and resolved[1])
    app_id_masked = mask_app_id(resolved[0]) if resolved else ""
    return {
        "platform": spec.platform,
        "display_name": spec.display_name,
        "configurable": spec.configurable,
        "configured": configured,
        "half_configured": bool(resolved and not resolved[1]),
        "app_id_masked": app_id_masked,
        "source": resolved[2] if resolved else "",
        "id_label": spec.id_label,
        "secret_label": spec.secret_label,
        "redirect_uri": spec.redirect_uri,
        "scopes": list(spec.scopes),
        "create_url": spec.create_url,
        "docs_url": spec.docs_url,
        "enables": spec.enables,
        "sign_in_available": configured and spec.configurable,
        "env_vars": [v for v in (spec.env_id_var, spec.env_secret_var) if v],
    }


def byo_status(config: Any, *, store: Any | None = None) -> dict[str, Any]:
    """Every BYO platform's status — the payload GET /api/byo and the
    ``xpst byo status`` command serve. Values are masked by construction."""
    cred_store = store
    if cred_store is None and getattr(config, "config_dir", ""):
        cred_store = _store_for(config)
    platforms = {
        name: byo_status_for_platform(config, name, store=cred_store)
        for name in BYO_PLATFORMS
    }
    return {
        "platforms": platforms,
        "meta_redirect_uri": META_LOOPBACK_REDIRECT,
        "note": (
            "One Meta app covers Instagram, Threads and Messenger: enter it once "
            "and both platforms pick it up. Secrets live only in the encrypted "
            "store under the xPST config dir and are never echoed back."
        ),
    }


# ── OAuth plumbing shared by the Meta platforms ──────────────────


def finalize_meta_oauth_code(
    config: Any,
    platform: str,
    *,
    code: str,
    redirect_uri: str,
    store: Any | None = None,
    client_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Redeem a Meta authorization code for the configured BYO app.

    Sequence (every call is a plain Graph API GET the app owner can verify):
    code -> short-lived user token -> long-lived user token -> ``GET /me``
    proof, then, for Instagram, a best-effort Page -> IG account binding via
    ``GET /me/accounts``. The user token is stored encrypted under the
    platform's graph-token keys; the response is step-by-step truth, never a
    bare success flag.

    ``client_factory`` exists for tests: a stub Graph client is injected
    instead of the network one.
    """
    spec = BYO_PLATFORMS.get(str(platform).strip().lower())
    if spec is None or not spec.shared_meta:
        raise ByoAppError(f"{platform} is not a Meta BYO platform.")
    resolved = resolve_byo_app(config, spec.platform, store=store)
    if resolved is None or not resolved[1]:
        raise ByoAppError(
            "NO_META_APP: finish the BYO app setup first, then sign in again.",
            errors={"app_id": "No Meta App ID/Secret is stored on this machine."},
        )
    app_id, app_secret = resolved[0], resolved[1]

    if client_factory is None:
        from xpst.platforms.facebook import FacebookGraphClient

        client_factory = lambda: FacebookGraphClient(  # noqa: E731 - tiny factory
            proxy=getattr(getattr(config, "facebook", None), "proxy", None)
        )
    client = client_factory()
    steps: list[dict[str, Any]] = []

    def record(step: str, ok: bool, detail: str = "") -> None:
        steps.append({"step": step, "ok": ok, "detail": detail})

    try:
        try:
            payload = client.exchange_code_for_user_token(app_id, app_secret, redirect_uri, code)
        except Exception as exc:  # noqa: BLE001 - provider message is the point
            raise ByoAppError(f"META_CODE_EXCHANGE_FAILED: {exc}") from None
        user_token = str(payload.get("access_token", "") or "")
        if not user_token:
            raise ByoAppError("META_CODE_EXCHANGE_FAILED: provider returned no access token.")
        record("code_exchange", True)

        try:
            extended = client.extend_user_token(app_id, app_secret, user_token)
            long_lived = str(extended.get("access_token", "") or "")
            if long_lived:
                user_token = long_lived
                record("token_extension", True, "long-lived user token (~60 days)")
            else:
                record("token_extension", False, "provider returned no extended token")
        except Exception as exc:  # noqa: BLE001 - short-lived token still works
            record("token_extension", False, str(exc))

        try:
            me = client.whoami(user_token)
        except Exception as exc:  # noqa: BLE001
            raise ByoAppError(f"META_TOKEN_UNVERIFIED: {exc}") from None
        account = {"id": str(me.get("id", "")), "name": str(me.get("name", ""))}
        record("token_verified", True, f"user {account['id']}")

        ig_user_id = ""
        page_token = ""
        if spec.platform == "instagram":
            try:
                pages = client.list_pages(user_token)
            except Exception as exc:  # noqa: BLE001 - binding is best-effort
                record("ig_binding", False, str(exc))
            else:
                record("pages_listed", True, f"{len(pages)} Page(s)")
                for page in list(pages)[:5]:
                    token = str(getattr(page, "access_token", "") or "")
                    if not token:
                        continue
                    try:
                        page_info = client.get(
                            str(page.id),
                            {"fields": "instagram_business_account", "access_token": token},
                            action="Instagram account lookup",
                        )
                    except Exception:  # noqa: BLE001 - keep trying other Pages
                        continue
                    binding = page_info.get("instagram_business_account")
                    if isinstance(binding, Mapping) and binding.get("id"):
                        ig_user_id = str(binding["id"])
                        page_token = token
                        break
                record(
                    "ig_binding",
                    bool(ig_user_id),
                    f"IG account {ig_user_id}" if ig_user_id else "no Page carries a linked IG account yet",
                )

        cred_store = store if store is not None else _store_for(config)
        section = getattr(config, spec.platform, None)
        if spec.platform == "instagram":
            cred_store.store("instagram_graph_token", user_token)
            if ig_user_id:
                # The Page token scoped to the IG account is what publishing
                # calls use; the user token remains the recovery credential.
                cred_store.store("instagram_graph_user_id", ig_user_id)
                publish_token = page_token or user_token
                cred_store.store("instagram_graph_publish_token", publish_token)
                if section is not None:
                    section.graph_access_token = publish_token
                    section.graph_ig_user_id = ig_user_id
        else:
            cred_store.store("threads_access_token", user_token)
            try:
                import httpx

                me_r = httpx.get(
                    "https://graph.threads.net/v1.0/me",
                    params={"fields": "id,username", "access_token": user_token},
                    timeout=15,
                )
                me_data = me_r.json() if me_r.status_code == 200 else {}
                threads_user_id = str(me_data.get("id", "") or "")
                if threads_user_id:
                    cred_store.store("threads_user_id", threads_user_id)
                    account["threads_user_id"] = threads_user_id
                record(
                    "threads_profile",
                    bool(threads_user_id),
                    f"Threads id {threads_user_id}" if threads_user_id else f"HTTP {me_r.status_code}",
                )
            except Exception as exc:  # noqa: BLE001 - profile lookup is best-effort
                record("threads_profile", False, str(exc))
            if section is not None:
                section.graph_access_token = user_token
        save = getattr(config, "save", None)
        if callable(save):
            save()
        record("stored", True, "encrypted CredentialStore only")
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    return {
        "platform": spec.platform,
        "ok": True,
        "steps": steps,
        "account": account,
        "ig_user_id": ig_user_id if spec.platform == "instagram" else "",
        "app_id_masked": mask_app_id(app_id),
    }
