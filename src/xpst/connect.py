"""
Streamlined account connection wizard for xPST.

Aims to connect all 4 platforms in under 5 minutes by:
- YouTube: Auto-opening browser for OAuth via InstalledAppFlow.run_local_server()
- Instagram: Username/password login via instagrapi (with 2FA support)
- X/Twitter: Official X API v2 (ban-safe, free 17 posts/day) or twikit cookies
- TikTok: Auto-extract browser cookies via yt-dlp --cookies-from-browser

Usage:
    xpst connect              # Connect all platforms
    xpst connect youtube      # Connect YouTube only
    xpst connect instagram    # Connect Instagram only
    xpst connect x            # Connect X/Twitter only
    xpst connect tiktok       # Connect TikTok only
    xpst connect --test       # Test all existing connections
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import secrets
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from xpst.config import XPSTConfig
from xpst.utils.credentials import CredentialStore
from xpst.utils.logger import get_logger
from xpst.utils.secure_io import write_text_0600

console = Console()
logger = get_logger(__name__)

CREDS_DIR_NAME = "credentials"


def _get_creds_dir(config: XPSTConfig) -> Path:
    """Get the credentials directory path, creating it if needed.

    Args:
        config: xPST configuration.

    Returns:
        Path to the ``credentials/`` subdirectory under config_dir.
    """

    creds_dir = Path(config.config_dir).expanduser() / CREDS_DIR_NAME
    creds_dir.mkdir(parents=True, exist_ok=True)
    return creds_dir


def _confirm(message: str, default: bool = True) -> bool:
    """Prompt the user for a yes/no confirmation with a default.

    Args:
        message: Question to display.
        default: Value to use if user presses Enter.

    Returns:
        True if confirmed, False otherwise.
    """

    suffix = " [Y/n]: " if default else " [y/N]: "
    try:
        response = console.input(f"[cyan]{message}{suffix}[/cyan]").strip().lower()
    except EOFError:
        # Piped/closed stdin (agent automation): the safe default is False —
        # treating EOF as "yes" caused side effects such as webbrowser.open()
        # firing during scripted runs. Interactive callers see the prompt as
        # usual; automated callers get a deterministic "no".
        return False
    if not response:
        return default
    return response in ("y", "yes")


def _input_secret(prompt: str) -> str:
    """Prompt for a secret value without echoing to terminal.

    Args:
        prompt: Prompt text to display.

    Returns:
        The entered secret string.
    """

    console.print(f"[cyan]{prompt}[/cyan]", end="")
    import getpass
    return getpass.getpass("")


# ──────────────────────────────────────────────
# YouTube OAuth (browser-based)
# ──────────────────────────────────────────────

def connect_youtube(config: XPSTConfig) -> bool:
    """
    Connect YouTube using OAuth2 browser flow.

    Flow:
    1. Check for client_secrets.json
    2. If missing, guide user to create it
    3. Use InstalledAppFlow.run_local_server() to open browser
    4. User authorizes in browser
    5. Token is saved automatically
    """
    console.print(Panel("[bold]YouTube Shorts Connection[/bold]", style="red"))
    creds_dir = _get_creds_dir(config)
    secrets_path = creds_dir / "youtube_client_secrets.json"
    token_path = creds_dir / "youtube_token.json"

    # Check for client_secrets.json
    if not secrets_path.exists():
        console.print(
            "[yellow]YouTube requires OAuth2 credentials from Google Cloud Console.[/yellow]\n"
            "You only need to do this once.\n"
        )
        console.print("[bold]Quick Setup (2 minutes):[/bold]")
        console.print("  1. Open: [link=https://console.cloud.google.com/apis/credentials]https://console.cloud.google.com/apis/credentials[/link]")
        console.print("  2. Create or select a project")
        console.print("  3. Click 'Create Credentials' → 'OAuth 2.0 Client ID'")
        console.print("  4. Application type: [bold]Desktop app[/bold]")
        console.print("  5. Download the JSON file")
        console.print(f"  6. Save it as: [bold]{secrets_path}[/bold]\n")

        if _confirm("Open Google Cloud Console in browser now?", default=True):
            import webbrowser
            webbrowser.open("https://console.cloud.google.com/apis/credentials")

        console.print(f"\n[dim]Waiting for client_secrets.json at:{secrets_path}[/dim]")
        console.print("[dim]Place the file there, then press Enter to continue...[/dim]")
        input()

        if not secrets_path.exists():
            console.print("[red]❌ File not found. Run [cyan]xpst connect youtube[/cyan] again when ready.[/red]")
            return False

    # Run OAuth flow with browser
    console.print("\n[bold]Opening browser for YouTube authorization...[/bold]")
    console.print("[dim]A browser window will open. Sign in and authorize xPST.[/dim]\n")

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow

        scopes = [
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly",
            "https://www.googleapis.com/auth/youtube.force-ssl",
        ]

        flow = InstalledAppFlow.from_client_secrets_file(
            str(secrets_path),
            scopes=scopes,
        )

        # run_local_server opens browser, handles redirect, returns credentials
        creds = flow.run_local_server(
            host="localhost",
            port=8085,
            open_browser=True,
            authorization_prompt_message="[bold]Opening browser for authorization...[/bold]",
            success_message="[green]✅ Authorization successful! You can close this tab.[/green]",
        )

        # Save token with owner-only perms (see SECURITY.md)
        write_text_0600(token_path, creds.to_json())

        # Store in keyring
        cred_store = CredentialStore(config.config_dir)
        try:
            cred_store.store("youtube_token", creds.to_json())
        except Exception as e:
            logger.debug("Unexpected error: %s", e)
            pass  # Keyring optional

        console.print("[green]✅ YouTube connected and token saved![/green]")
        return True

    except FileNotFoundError:
        console.print("[red]❌ client_secrets.json not found. Please download it first.[/red]")
        return False
    except Exception as e:
        logger.error(f"YouTube OAuth failed: {e}")
        console.print(f"[red]❌ YouTube connection failed: {e}[/red]")
        if "access_denied" in str(e).lower():
            console.print("[dim]Make sure your OAuth app is in 'Testing' mode or you're added as a test user.[/dim]")
        return False


# ──────────────────────────────────────────────
# Instagram (Graph API preferred, instagrapi fallback)
# ──────────────────────────────────────────────

def connect_instagram(config: XPSTConfig) -> bool:
    """
    Connect Instagram. Defaults to the official Graph API (ban-safe).
    Falls back to instagrapi session auth only if the user explicitly chooses it.

    Graph API flow:
    1. Ask for Meta Graph API access token + IG user ID
    2. Verify the token works
    3. Save to config

    Session flow (instagrapi, NOT recommended):
    1. Warn about ban risk
    2. Prompt for username and password
    3. Login via instagrapi.Client.login()
    4. Save session for persistence
    """
    console.print(Panel("[bold]Instagram Reels Connection[/bold]", style="magenta"))

    console.print(
        "\n[bold green]Recommended:[/bold green] Use the official Meta Graph API (ban-safe, ToS-compliant).\n"
        "[bold red]Not recommended:[/bold red] instagrapi session auth (risks account bans).\n"
    )
    console.print(
        "[dim]Graph API requires an Instagram Creator/Business account + Facebook Page.\n"
        "If you don't have those yet, see: https://developers.facebook.com/docs/instagram-api/getting-started[/dim]\n"
    )

    use_graph = _confirm("Use Graph API (recommended)?", default=True)

    if use_graph:
        return _connect_instagram_graph_api(config)
    else:
        console.print("[yellow]⚠️  instagrapi uses Instagram's private API and can get your account BANNED.[/yellow]")
        console.print("[yellow]   Instagram actively detects and blocks automated clients.[/yellow]\n")
        if not _confirm("Continue with instagrapi anyway?", default=False):
            console.print("[dim]Cancelled. Set up Graph API for ban-safe posting.[/dim]")
            return False
        return _connect_instagram_session(config)


GRAPH_API_BASE = "https://graph.facebook.com/v21.0"
GRAPH_EXPLORER_URL = "https://developers.facebook.com/tools/explorer/"


def _graph_api_verify_url(ig_user_id: str, access_token: str) -> str:
    """Build the Graph API URL used to verify a token against an IG account.

    Args:
        ig_user_id: Numeric Instagram business/creator account ID.
        access_token: Meta Graph API access token.

    Returns:
        Full ``https://graph.facebook.com/v21.0/<ig_user_id>?...`` verify URL.
    """
    params = urlencode(
        {
            "fields": "username,followers_count,media_count",
            "access_token": access_token,
        }
    )
    return f"{GRAPH_API_BASE}/{ig_user_id}?{params}"


def _verify_instagram_graph_token(ig_user_id: str, access_token: str, timeout: float = 15.0) -> dict | None:
    """Verify a Graph API token against an IG account.

    Args:
        ig_user_id: Numeric Instagram business/creator account ID.
        access_token: Meta Graph API access token.
        timeout: HTTP timeout in seconds.

    Returns:
        Parsed JSON payload on HTTP 200, or None on any failure.
    """
    try:
        r = httpx.get(_graph_api_verify_url(ig_user_id, access_token), timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        logger.debug("Unexpected error: %s", e)
        return None


def _fetch_ig_business_accounts(access_token: str, timeout: float = 15.0) -> list[dict]:
    """Enumerate the IG business accounts linked to a token's Facebook Pages.

    Uses ``/me/accounts`` and collects each Page's ``instagram_business_account``.

    Returns:
        List of dicts with keys ``id``, ``username``, ``followers_count``,
        ``media_count``, ``page_name``, ``page_id``.
    """
    try:
        r = httpx.get(
            f"{GRAPH_API_BASE}/me/accounts",
            params={
                "fields": "id,name,instagram_business_account{id,username,followers_count,media_count}",
                "access_token": access_token,
            },
            timeout=timeout,
        )
        if r.status_code != 200:
            return []
        accounts = []
        for page in r.json().get("data", []):
            ig = page.get("instagram_business_account") or {}
            if ig.get("id"):
                accounts.append(
                    {
                        "id": str(ig["id"]),
                        "username": ig.get("username", ""),
                        "followers_count": int(ig.get("followers_count") or 0),
                        "media_count": int(ig.get("media_count") or 0),
                        "page_name": page.get("name", ""),
                        "page_id": page.get("id", ""),
                    }
                )
        return accounts
    except Exception as e:
        logger.debug("Unexpected error: %s", e)
        return []


def _detect_ig_user_id(access_token: str, timeout: float = 15.0) -> tuple[str, str, int, int]:
    """Auto-detect the IG business account bound to a token.

    First tries ``/me`` — a token generated in the Graph API Explorer with the
    IG business account selected returns the account's ``id``/``username``
    directly. Otherwise enumerates ``/me/accounts`` for a Page-linked IG
    business account (prompts if several are found).

    Returns:
        ``(ig_user_id, username, followers_count, media_count)`` — empty string
        and zeros when nothing could be detected.
    """
    try:
        r = httpx.get(
            f"{GRAPH_API_BASE}/me",
            params={"fields": "id,username,followers_count,media_count", "access_token": access_token},
            timeout=timeout,
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("id") and data.get("username"):
                return (
                    str(data["id"]),
                    data.get("username", ""),
                    int(data.get("followers_count") or 0),
                    int(data.get("media_count") or 0),
                )
    except Exception as e:
        logger.debug("Unexpected error: %s", e)

    accounts = _fetch_ig_business_accounts(access_token, timeout=timeout)
    if len(accounts) == 1:
        acc = accounts[0]
        return (acc["id"], acc["username"], acc["followers_count"], acc["media_count"])
    if len(accounts) > 1:
        console.print("[bold]Multiple IG business accounts found:[/bold]")
        for i, acc in enumerate(accounts, 1):
            console.print(
                f"  [cyan]{i}.[/cyan] @{acc['username'] or '?'} "
                f"({acc['followers_count'] or '?'} followers) — Page: {acc['page_name'] or '?'}"
            )
        choice = console.input("[cyan]Which account? (number): [/cyan]").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(accounts):
            acc = accounts[int(choice) - 1]
            return (acc["id"], acc["username"], acc["followers_count"], acc["media_count"])

    return ("", "", 0, 0)


def _connect_instagram_graph_api(config: XPSTConfig) -> bool:
    """Connect via official Meta Graph API (OEM, self-verifying).

    Flow:
    1. Offer the Graph API Explorer in the browser to generate a token.
    2. Ask for the long-lived access token.
    3. Auto-detect the IG user ID via /me and /me/accounts (prompt fallback).
    4. Verify the token against the detected account.
    5. Save token + user ID to config and the CredentialStore.
    """
    console.print("\n[bold]Meta Graph API Setup[/bold]")
    console.print("[dim]You need a long-lived access token; xPST auto-detects your IG user ID.[/dim]\n")

    console.print("[bold]Quick setup:[/bold]")
    console.print("  1. Open: [link=https://developers.facebook.com/apps]https://developers.facebook.com/apps[/link]")
    console.print("  2. Create or select a Meta app")
    console.print("  3. Add the 'Instagram Graph API' product")
    console.print("  4. In the Graph API Explorer, pick your IG business account and generate a token")
    console.print("  5. Paste the token below — xPST verifies it and finds your IG user ID automatically\n")

    if _confirm("Open Meta Developer console in browser?", default=True):
        import webbrowser
        webbrowser.open("https://developers.facebook.com/apps")

    if _confirm("Generate token in browser (Graph API Explorer)?", default=True):
        import webbrowser
        webbrowser.open(GRAPH_EXPLORER_URL)

    access_token = _input_secret("Long-lived access token: ")
    if not access_token:
        console.print("[red]❌ Access token required.[/red]")
        return False

    # Auto-detect the IG user ID (prompt fallback stays)
    console.print("\n[bold]Auto-detecting your IG business account...[/bold]")
    ig_user_id, username, followers, media_count = _detect_ig_user_id(access_token)
    if not ig_user_id:
        console.print("[yellow]⚠️  Could not auto-detect the IG business account from this token.[/yellow]")
        console.print(
            "[dim]Make sure the token has instagram_basic scope and the account is a "
            "Business/Creator account linked to a Facebook Page.[/dim]"
        )
        ig_user_id = console.input("[cyan]Enter your IG user ID manually (numbers): [/cyan]").strip()
        if not ig_user_id:
            console.print("[red]❌ IG user ID required.[/red]")
            return False

    # Verify the token works against the detected/entered account
    console.print("\n[bold]Verifying token...[/bold]")
    data = _verify_instagram_graph_token(ig_user_id, access_token)
    if data is None:
        console.print("[red]❌ Token verification failed.[/red]")
        console.print("[dim]Make sure your token has instagram_basic + instagram_content_publish scopes.[/dim]")
        return False
    console.print(
        f"[green]✅ Connected as @{data.get('username') or username or '?'} "
        f"({data.get('followers_count', followers or '?')} followers, "
        f"{data.get('media_count', media_count or '?')} posts)[/green]"
    )

    # Save to config
    config.instagram.auth_mode = "graph_api"
    config.instagram.graph_access_token = access_token
    config.instagram.graph_ig_user_id = ig_user_id
    config.save()

    # Also store in encrypted credential store
    cred_store = CredentialStore(config.config_dir)
    try:
        cred_store.store("instagram_graph_token", access_token)
        cred_store.store("instagram_graph_user_id", ig_user_id)
    except Exception as e:
        logger.debug("Unexpected error: %s", e)

    console.print("[green]✅ Instagram Graph API configured (ban-safe)![/green]")
    return True


def _connect_instagram_session(config: XPSTConfig) -> bool:
    """Connect via instagrapi session auth (NOT recommended, ban risk)."""
    creds_dir = _get_creds_dir(config)
    session_path = creds_dir / "instagram_session.json"

    console.print("[dim]Enter your Instagram credentials. We'll save a session file so you don't need to re-enter them.[/dim]\n")

    username = console.input("[cyan]Instagram username: [/cyan]").strip()
    if not username:
        console.print("[red]❌ Username required.[/red]")
        return False

    password = _input_secret("Instagram password: ")
    if not password:
        console.print("[red]❌ Password required.[/red]")
        return False

    console.print("\n[bold]Connecting to Instagram...[/bold]")

    try:
        from instagrapi import Client

        client = Client()

        # Try loading existing settings first for stability
        if session_path.exists():
            try:
                with open(session_path, encoding="utf-8") as f:
                    existing = json.load(f)
                if "settings" in existing:
                    client.set_settings(existing["settings"])
            except Exception as e:
                logger.debug("Unexpected error: %s", e)
                pass

        # Attempt login
        try:
            client.login(username, password)
        except Exception as login_error:
            error_str = str(login_error).lower()

            # Check if 2FA is required
            if "two_factor" in error_str or "verification" in error_str or "challenge" in error_str:
                console.print("[yellow]⚠️  Two-factor authentication required.[/yellow]")
                console.print("[dim]Enter the code from your authenticator app (Google Authenticator, Authy, etc.)[/dim]\n")
                verification_code = console.input("[cyan]2FA code: [/cyan]").strip()

                if not verification_code:
                    console.print("[red]❌ Verification code required.[/red]")
                    return False

                try:
                    client.login(username, password, verification_code=verification_code)
                except Exception as e2:
                    console.print(f"[red]❌ 2FA login failed: {e2}[/red]")
                    return False
            elif "challenge" in error_str:
                # Instagram challenge (unusual login, SMS code, etc.)
                console.print("[yellow]⚠️  Instagram requires additional verification.[/yellow]")
                console.print("[dim]Check your Instagram app or email for a security code.[/dim]\n")
                code = console.input("[cyan]Security code: [/cyan]").strip()
                if not code:
                    console.print("[red]❌ Code required.[/red]")
                    return False
                try:
                    client.challenge_code_handler(username, code)
                    client.login(username, password)
                except Exception as e2:
                    console.print(f"[red]❌ Challenge verification failed: {e2}[/red]")
                    return False
            elif "password" in error_str or "credentials" in error_str:
                console.print("[red]❌ Invalid username or password.[/red]")
                return False
            else:
                raise login_error

        # Save session settings for persistence
        settings = client.get_settings()
        session_data = {
            "authorization_data": {
                "sessionid": settings.get("authorization_data", {}).get("sessionid", ""),
            },
            "settings": settings,
            "username": username,
            "connected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        # Owner-only perms on the session file (see SECURITY.md)
        write_text_0600(session_path, json.dumps(session_data, indent=2))

        # Store in keyring
        cred_store = CredentialStore(config.config_dir)
        try:
            cred_store.store_json("instagram_session", session_data)
        except Exception as e:
            logger.debug("Unexpected error: %s", e)
            pass

        # Set auth_mode to session since user chose this path
        config.instagram.auth_mode = "session"
        config.save()

        # Verify connection
        try:
            account = client.account_info()
            console.print(f"[green]✅ Connected as @{account.username} ({account.full_name})[/green]")
        except Exception as e:
            logger.debug("Unexpected error: %s", e)
            console.print("[green]✅ Instagram connected and session saved![/green]")

        return True

    except ImportError:
        console.print("[red]❌ instagrapi not installed. Run: pip install instagrapi[/red]")
        return False
    except Exception as e:
        logger.error(f"Instagram connection failed: {e}")
        console.print(f"[red]❌ Instagram connection failed: {e}[/red]")
        return False


# ──────────────────────────────────────────────
# X/Twitter (official X API v2 or twikit cookies)
# ──────────────────────────────────────────────

def connect_x(config: XPSTConfig) -> bool:
    """
    Connect X/Twitter via the official X API v2 (recommended) or twikit cookies.

    Flow:
    1. Ask which auth method: official X API v2 (ban-safe) or cookies (twikit)
    2. Option 1 (default): walk user through creating an X Developer app,
       prompt for API Key/Secret + Access Token/Secret or Bearer Token, verify
       via https://api.x.com/2/users/me, save config + CredentialStore
    3. Option 2: username/email/password login via twikit.Client.login()
    """
    console.print(Panel("[bold]X/Twitter Connection[/bold]", style="blue"))

    choice = console.input(
        "[cyan]Which auth method? [1] Official X API v2 (ban-safe, free 17 posts/day) "
        "[2] Cookies (twikit, unofficial) [/cyan]"
    ).strip()

    if choice == "2":
        return _connect_x_cookies(config)
    return _connect_x_api_v2(config)


def _connect_x_cookies(config: XPSTConfig) -> bool:
    """
    Connect X/Twitter using username/email/password via twikit (unofficial).

    Flow:
    1. Prompt for username, email, and password
    2. Login via twikit.Client.login()
    3. Cookies are auto-saved
    4. Test connection
    """
    creds_dir = _get_creds_dir(config)
    cookies_path = creds_dir / "x_cookies.json"

    console.print("[dim]Enter your X/Twitter credentials. No cookie export needed![/dim]\n")

    username = console.input("[cyan]X username (without @): [/cyan]").strip()
    if username.startswith("@"):
        username = username[1:]
    if not username:
        console.print("[red]❌ Username required.[/red]")
        return False

    email = console.input("[cyan]X email address: [/cyan]").strip()
    if not email:
        console.print("[red]❌ Email required (X needs it for login verification).[/red]")
        return False

    password = _input_secret("X password: ")
    if not password:
        console.print("[red]❌ Password required.[/red]")
        return False

    # Optional 2FA support. twikit.Client.login() accepts totp_secret;
    # xPST should forward it so accounts with 2FA don't get dead-ended.
    totp_secret = None
    if _confirm("Does your X account use 2FA? (authenticator app code/secret)", default=False):
        totp_secret = _input_secret("X 2FA secret (or code, no spaces): ")
    elif input("2FA secret empty — skip? [y/N] ").strip().lower().startswith("y"):
        totp_secret = ""

    console.print("\n[bold]Connecting to X/Twitter...[/bold]")

    try:
        import twikit

        cookies_path_str = str(cookies_path)

        async def _do_connect():
            """Login and verify X/Twitter in a single async context."""
            client = twikit.Client("en-US")
            await client.login(
                auth_info_1=username,
                auth_info_2=email,
                password=password,
                totp_secret=totp_secret or None,
                cookies_file=cookies_path_str,
            )

            # Verify
            screen_name = None
            try:
                user = await client.user()
                screen_name = user.screen_name
            except Exception as e:
                logger.debug("Unexpected error: %s", e)
                pass

            return screen_name

        screen_name = asyncio.run(_do_connect())

        # twikit wrote the cookies file itself; tighten it to owner-only.
        if cookies_path.exists():
            with contextlib.suppress(OSError):
                cookies_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

        # Store in keyring
        cred_store = CredentialStore(config.config_dir)
        try:
            if cookies_path.exists():
                cookies_data = json.loads(cookies_path.read_text())
                cred_store.store_json("x_cookies", cookies_data)
        except Exception as e:
            logger.debug("Unexpected error: %s", e)
            pass

        if screen_name:
            console.print(f"[green]✅ Connected as @{screen_name}[/green]")
        else:
            console.print("[green]✅ X/Twitter connected and cookies saved![/green]")

        return True

    except ImportError:
        console.print("[red]❌ twikit not installed. Run: pip install twikit[/red]")
        return False
    except Exception as e:
        logger.error(f"X connection failed: {e}")
        error_str = str(e).lower()
        if "password" in error_str or "credentials" in error_str:
            console.print("[red]❌ Invalid credentials. Check username, email, and password.[/red]")
        elif "suspended" in error_str:
            console.print("[red]❌ Account appears to be suspended.[/red]")
        elif "rate" in error_str:
            console.print("[red]❌ Rate limited. Try again later.[/red]")
        else:
            console.print(f"[red]❌ X connection failed: {e}[/red]")
        return False


# X API v2 (official) endpoints — ban-safe free tier (17 posts/day)
X_API_V2_ME_URL = "https://api.x.com/2/users/me"
X_DEV_PORTAL_URL = "https://developer.x.com/en/portal"


def _x_api_v2_verify_headers(bearer_token: str) -> dict[str, str]:
    """Build the Authorization header for the /2/users/me verification call.

    Args:
        bearer_token: X API v2 Bearer Token (or OAuth 1.0a user-context
            access token used as a bearer).

    Returns:
        ``{"Authorization": "Bearer <token>"}`` header dict.
    """
    return {"Authorization": f"Bearer {bearer_token}"}


def _verify_x_api_v2_creds(
    api_key: str,
    api_secret: str,
    access_token: str,
    access_token_secret: str,
    bearer_token: str,
    timeout: float = 15.0,
) -> tuple[bool, str]:
    """Verify X API v2 credentials against ``GET https://api.x.com/2/users/me``.

    Prefers the Bearer Token (app-only). When only the OAuth 1.0a user-context
    set (API Key/Secret + Access Token/Secret) is available, signs the request
    with authlib's ``OAuth1Client`` instead.

    Args:
        api_key: X API Key (Consumer Key).
        api_secret: X API Key Secret (Consumer Secret).
        access_token: X Access Token (OAuth 1.0a user context).
        access_token_secret: X Access Token Secret.
        bearer_token: X Bearer Token (app-only).
        timeout: HTTP timeout in seconds.

    Returns:
        ``(ok, message)`` — on success ``message`` is ``@username (id: ...)``,
        on failure a short error description.
    """
    try:
        if bearer_token:
            r = httpx.get(
                X_API_V2_ME_URL,
                headers=_x_api_v2_verify_headers(bearer_token),
                timeout=timeout,
            )
        else:
            from authlib.integrations.httpx_client import OAuth1Client

            client = OAuth1Client(
                client_id=api_key,
                client_secret=api_secret,
                token=access_token,
                token_secret=access_token_secret,
            )
            r = client.get(X_API_V2_ME_URL, timeout=timeout)
    except Exception as e:
        logger.debug("Unexpected error: %s", e)
        return False, str(e)

    if r.status_code == 200:
        data = r.json().get("data", {})
        return True, f"@{data.get('username', '?')} (id: {data.get('id', '?')})"
    return False, f"{r.status_code} {getattr(r, 'text', '')[:200]}"


def _connect_x_api_v2(config: XPSTConfig) -> bool:
    """Connect X via the official X API v2 (ban-safe, free 17 posts/day).

    Flow:
    1. Guide the user through creating an X Developer app.
    2. Prompt for API Key/Secret + Access Token/Secret or Bearer Token.
    3. Verify against https://api.x.com/2/users/me.
    4. Save ``auth_mode='api_v2'`` + credentials to config and the
       CredentialStore (key ``x_api_v2_creds``).
    """
    console.print("\n[bold]X API v2 Setup[/bold]")
    console.print(
        "[dim]Official X API v2 uses OAuth 1.0a user context (API Key/Secret + "
        "Access Token/Secret) or a Bearer Token. No cookies, no unofficial "
        "clients — ban-safe posting on the free tier (17 posts/day).[/dim]\n"
    )

    console.print("[bold]Quick setup:[/bold]")
    console.print(f"  1. Open: [link={X_DEV_PORTAL_URL}]{X_DEV_PORTAL_URL}[/link]")
    console.print("  2. Create an app (or reuse an existing one)")
    console.print("  3. Grant the app Read + Write permissions")
    console.print("  4. Under 'Keys and tokens', copy the API Key, API Key Secret, Access Token, and Access Token Secret")
    console.print("  5. (Optional) Copy the Bearer Token for app-only verification\n")

    if _confirm("Open X Developer Portal in browser now?", default=True):
        import webbrowser
        webbrowser.open(X_DEV_PORTAL_URL)

    api_key = _input_secret("API Key (Consumer Key): ")
    if not api_key:
        console.print("[red]❌ API Key required.[/red]")
        return False

    api_secret = _input_secret("API Key Secret (Consumer Secret): ")
    if not api_secret:
        console.print("[red]❌ API Key Secret required.[/red]")
        return False

    access_token = _input_secret("Access Token (leave empty if using Bearer Token only): ")
    access_token_secret = _input_secret("Access Token Secret (leave empty if using Bearer Token only): ")
    bearer_token = _input_secret("Bearer Token (optional): ")

    if not access_token and not bearer_token:
        console.print("[red]❌ Need either the Access Token + Secret (for posting) or a Bearer Token (for verification).[/red]")
        return False

    # Verify the credentials against /2/users/me
    console.print("\n[bold]Verifying credentials...[/bold]")
    ok, message = _verify_x_api_v2_creds(
        api_key,
        api_secret,
        access_token,
        access_token_secret,
        bearer_token,
    )
    if not ok:
        console.print(f"[red]❌ Verification failed: {message}[/red]")
        console.print("[dim]Double-check your keys/tokens in the X Developer Portal.[/dim]")
        return False
    console.print(f"[green]✅ Verified as {message}[/green]")

    # Save to config
    config.x.auth_mode = "api_v2"
    config.x.api_key = api_key
    config.x.api_secret = api_secret
    config.x.access_token = access_token
    config.x.access_token_secret = access_token_secret
    config.x.bearer_token = bearer_token
    config.save()

    # Store in encrypted credential store
    cred_store = CredentialStore(config.config_dir)
    try:
        cred_store.store_json(
            "x_api_v2_creds",
            {
                "api_key": api_key,
                "api_secret": api_secret,
                "access_token": access_token,
                "access_token_secret": access_token_secret,
                "bearer_token": bearer_token,
                "connected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
        )
    except Exception as e:
        logger.debug("Unexpected error: %s", e)

    console.print("[green]✅ X API v2 configured (ban-safe, official) — 17 free posts/day![/green]")
    if not access_token:
        console.print("[yellow]⚠️  No Access Token set — verification works, but posting needs the Access Token + Secret.[/yellow]")
    return True


# ──────────────────────────────────────────────
# TikTok (browser cookies via yt-dlp) — SOURCE
# ──────────────────────────────────────────────

# TikTok Content Posting API (Direct Post) — official OAuth endpoints
TIKTOK_AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize"
TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_USER_INFO_URL = "https://open.tiktokapis.com/v2/user/info/"
TIKTOK_OAUTH_SCOPES = "user.info.basic,video.publish,video.upload"
TIKTOK_DEFAULT_REDIRECT_URI = "http://localhost:8085/callback"


def generate_pkce_pair() -> tuple[str, str]:
    """Generate a PKCE code verifier + S256 code challenge pair.

    Per RFC 7636: the verifier is a high-entropy random string, and the
    challenge is ``BASE64URL-ENCODE(SHA256(ASCII(verifier)))`` with any
    trailing ``=`` padding stripped.

    Returns:
        ``(code_verifier, code_challenge)`` — keep the verifier and send it
        to the token endpoint; send the challenge on the authorize URL.
    """
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def build_tiktok_authorize_url(
    client_key: str,
    redirect_uri: str,
    code_challenge: str | None = None,
    state: str | None = None,
) -> str:
    """Build the TikTok OAuth authorization URL for a Content Posting API app.

    Args:
        client_key: TikTok app client key (developers.tiktok.com).
        redirect_uri: The redirect URI registered on the app.
        code_challenge: PKCE S256 code challenge (RFC 7636). TikTok requires
            PKCE for the Content Posting API — omitting it fails with
            ``errCode 10007`` — so callers should always pass one generated
            with :func:`generate_pkce_pair`.
        state: Optional CSRF ``state`` value to include in the URL.

    Returns:
        Full authorization URL with ``response_type=code``,
        ``code_challenge``/``code_challenge_method=S256`` when PKCE is used,
        and the ``user.info.basic,video.publish,video.upload`` scopes.
    """
    params: dict[str, str] = {
        "client_key": client_key,
        "response_type": "code",
        "scope": TIKTOK_OAUTH_SCOPES,
        "redirect_uri": redirect_uri,
    }
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    if state:
        params["state"] = state
    return f"{TIKTOK_AUTHORIZE_URL}?{urlencode(params, safe=',')}"


def _extract_tiktok_code(pasted: str) -> str:
    """Extract the OAuth ``code`` from a pasted redirect URL or raw code.

    The user may paste the full redirect URL (``.../callback?code=xxx&...``)
    or just the ``code`` value. Handles both.

    Args:
        pasted: The raw string pasted by the user.

    Returns:
        The extracted authorization code (possibly empty).
    """
    pasted = (pasted or "").strip()
    if "code=" in pasted:
        query = pasted.split("?", 1)[1] if "?" in pasted else pasted
        return parse_qs(query).get("code", [""])[0]
    return pasted


def exchange_tiktok_code(
    client_key: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str | None = None,
) -> dict:
    """Exchange a TikTok authorization code for access + refresh tokens.

    Args:
        client_key: TikTok app client key.
        client_secret: TikTok app client secret.
        code: Authorization code from the OAuth redirect.
        redirect_uri: Must match the one used in the authorize URL.
        code_verifier: PKCE code verifier used to build the authorize URL
            (from :func:`generate_pkce_pair`). Required when the authorize
            URL carried a ``code_challenge``.

    Returns:
        The parsed token response dict (``access_token``, ``refresh_token``...).

    Raises:
        ValueError: On HTTP error or missing ``access_token`` in the response.
    """
    import httpx

    data: dict[str, str] = {
        "client_key": client_key,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if code_verifier:
        data["code_verifier"] = code_verifier
    resp = httpx.post(
        TIKTOK_TOKEN_URL,
        data=data,
        timeout=30,
    )
    if resp.status_code != 200:
        raise ValueError(
            f"TIKTOK_TOKEN_EXCHANGE_FAILED: HTTP {resp.status_code}: {resp.text[:200]}"
        )
    data = resp.json()
    if not data.get("access_token"):
        raise ValueError(
            f"TIKTOK_TOKEN_EXCHANGE_FAILED: no access_token in response: {resp.text[:200]}"
        )
    return data


def _verify_tiktok_token(access_token: str) -> str | None:
    """Verify a TikTok access token via the user/info endpoint.

    Args:
        access_token: The OAuth access token to verify.

    Returns:
        Display name (fallback: username) of the connected account, or None
        if verification failed.
    """
    import httpx

    try:
        resp = httpx.get(
            TIKTOK_USER_INFO_URL,
            params={"fields": "open_id,union_id,avatar_url,display_name,username"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        if resp.status_code != 200:
            logger.warning(
                "TikTok user/info verification failed: HTTP %s: %s",
                resp.status_code,
                resp.text[:200],
            )
            return None
        user = resp.json().get("data", {}).get("user", {})
        return user.get("display_name") or user.get("username") or None
    except Exception as e:
        logger.debug("TikTok user/info verification error: %s", e)
        return None


def _connect_tiktok_upload_destination(config: XPSTConfig) -> bool:
    """Configure the official TikTok Content Posting API upload destination.

    Runs the OAuth 2.0 authorization-code flow against developers.tiktok.com:

    1. Prompt for the app's client_key / client_secret
    2. Build + open the authorize URL (scope: user.info.basic,video.publish,video.upload)
    3. Prompt the user to paste the ``code`` from the redirect
    4. Exchange the code for access_token + refresh_token
    5. Persist credentials (config + CredentialStore) and verify via user/info

    Args:
        config: xPST configuration to update in place.

    Returns:
        True on success, False otherwise.
    """
    console.print("\n[bold]TikTok Upload Destination (Content Posting API)[/bold]")
    console.print(
        "[dim]This enables official, ban-safe uploads. You need a TikTok app "
        "with the Content Posting API enabled (scopes: user.info.basic, "
        "video.publish, video.upload).[/dim]\n"
    )

    client_key = console.input("[cyan]TikTok app client key: [/cyan]").strip()
    if not client_key:
        console.print("[red]❌ Client key required.[/red]")
        return False

    client_secret = _input_secret("TikTok app client secret: ")
    if not client_secret:
        console.print("[red]❌ Client secret required.[/red]")
        return False

    redirect_uri = TIKTOK_DEFAULT_REDIRECT_URI
    if not _confirm(
        f"Use default redirect URI {TIKTOK_DEFAULT_REDIRECT_URI}? "
        "(say N if your app has a public/custom redirect)",
        default=True,
    ):
        redirect_uri = console.input("[cyan]Your app's redirect URI: [/cyan]").strip()
        if not redirect_uri:
            console.print("[red]❌ Redirect URI required.[/red]")
            return False

    code_verifier, code_challenge = generate_pkce_pair()
    authorize_url = build_tiktok_authorize_url(client_key, redirect_uri, code_challenge)
    console.print("\n[bold]Authorize in your browser:[/bold]")
    console.print(f"[link={authorize_url}]{authorize_url}[/link]\n")

    code: str | None = None

    # Preferred path: capture the redirect automatically on a local listener
    # (same pattern as the YouTube flow) so the user never pastes codes.
    from xpst.utils.oauth_local import LocalOAuthListener

    listener: LocalOAuthListener | None = None
    if redirect_uri.startswith("http://127.0.0.1") or redirect_uri.startswith("http://localhost"):
        try:
            listener = LocalOAuthListener(port=8085, path="/callback")
            listener.start()
            console.print(f"[dim]Listening for the redirect on {listener.redirect_uri} …[/dim]")
        except OSError as e:
            logger.debug("Local OAuth listener unavailable (%s) — falling back to paste", e)
            listener = None

    if listener is not None:
        try:
            import webbrowser

            webbrowser.open(authorize_url)
        except Exception as e:  # noqa: BLE001 — headless boxes have no browser
            logger.debug("Could not open browser: %s", e)

        try:
            result = listener.wait(timeout=300)
            if result.success and result.code:
                code = result.code
            else:
                console.print(f"[red]❌ Authorization failed: {result.error or 'unknown error'}[/red]")
        except TimeoutError:
            console.print(
                "[yellow]⚠ Did not catch the redirect in time — you can paste the "
                "redirect URL below instead.[/yellow]"
            )
        finally:
            listener.close()

    if code is None:
        if listener is not None:
            console.print("[dim]Paste the full redirect URL or just the ?code= value below.[/dim]")
        else:
            console.print(
                "[dim]After authorizing, you'll be redirected. Paste the full redirect "
                "URL or just the ?code= value below.[/dim]"
            )
        pasted = console.input("[cyan]Redirect URL or code: [/cyan]").strip()
        code = _extract_tiktok_code(pasted)

    if not code:
        console.print("[red]❌ Authorization code required.[/red]")
        return False

    console.print("\n[bold]Exchanging code for access tokens...[/bold]")
    try:
        token_data = exchange_tiktok_code(client_key, client_secret, code, redirect_uri, code_verifier)
    except Exception as e:
        console.print(f"[red]❌ Token exchange failed: {e}[/red]")
        return False

    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token", "")

    # Persist in config.yaml + encrypted CredentialStore
    config.tiktok.enabled = True
    config.tiktok.client_key = client_key
    config.tiktok.client_secret = client_secret
    config.tiktok.access_token = access_token
    config.tiktok.refresh_token = refresh_token
    config.save()

    cred_store = CredentialStore(config.config_dir)
    try:
        cred_store.store("tiktok_client_secret", client_secret)
        cred_store.store("tiktok_access_token", access_token)
        if refresh_token:
            cred_store.store("tiktok_refresh_token", refresh_token)
    except Exception as e:
        logger.debug("Unexpected error: %s", e)

    # Verify the token works
    display_name = _verify_tiktok_token(access_token)
    if display_name:
        console.print(f"[green]✅ TikTok upload destination connected as {display_name}![/green]")
    else:
        console.print("[green]✅ TikTok upload destination configured (tokens saved)![/green]")
        console.print(
            "[dim]   Could not verify user info — tokens will be used on first upload.[/dim]"
        )
    return True


def connect_tiktok(config: XPSTConfig) -> bool:
    """
    Configure TikTok source with browser cookie extraction.

    Flow:
    1. Check if yt-dlp is installed
    2. Enable cookies_from_browser in config
    3. Test by fetching a video
    """
    console.print(Panel("[bold]TikTok Source[/bold]", style="cyan"))
    console.print("[dim]TikTok doesn't require authentication for downloads.[/dim]")
    console.print("[dim]Using browser cookies enables higher quality (HD without watermarks).[/dim]\n")

    # Check yt-dlp
    import shutil
    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        console.print("[yellow]⚠️  yt-dlp not found. TikTok downloads will need it.[/yellow]")
        console.print("Install with: [cyan]pip install yt-dlp[/cyan]\n")

    # Get TikTok username
    current_username = config.tiktok.username
    if current_username:
        console.print(f"[dim]Current TikTok username: @{current_username}[/dim]")
        if not _confirm(f"Keep using @{current_username}?", default=True):
            current_username = ""

    if not current_username:
        username = console.input("[cyan]TikTok username to watch (without @): [/cyan]").strip()
        if username.startswith("@"):
            username = username[1:]
        if not username:
            console.print("[red]❌ Username required.[/red]")
            return False
        current_username = username

    # Enable browser cookies
    if _confirm("Enable browser cookies for HD quality? (recommended)", default=True):
        config.tiktok.cookies_from_browser = True
        console.print("[dim]Will auto-extract cookies from your browser (Chrome, Safari, Firefox, etc.)[/dim]")

    # Save username
    config.tiktok.username = current_username
    config.save()

    console.print(f"[green]✅ TikTok source configured for @{current_username}[/green]")
    if config.tiktok.cookies_from_browser:
        console.print("[dim]   Browser cookies will be used automatically for downloads.[/dim]")

    # Optional: official upload destination via the TikTok Content Posting API.
    # The source/cookies path above is untouched; this only adds the OAuth
    # destination flow when the user opts in.
    if _confirm("Configure upload destination (Content Posting API)?", default=False):
        return _connect_tiktok_upload_destination(config)

    return True


# ──────────────────────────────────────────────
# Threads (Meta Threads API — long-lived access token)
# ──────────────────────────────────────────────

def connect_threads(config: XPSTConfig) -> bool:
    """
    Connect Threads via the Meta Threads API.

    Flow:
    1. Prompt for the Threads user ID and a long-lived access token
    2. Store credentials in config + encrypted credential store
    """
    console.print(Panel("[bold]Threads Connection[/bold]", style="white"))
    console.print(
        "[dim]Threads uses the Meta Threads API. You need a long-lived access "
        "token and your numeric Threads user ID.[/dim]\n"
    )
    console.print("[bold]Quick setup:[/bold]")
    console.print("  1. Go to: [link=https://developers.facebook.com/apps]https://developers.facebook.com/apps[/link]")
    console.print("  2. Create or select an app and add the 'Threads API' use case")
    console.print("  3. Generate a long-lived access token")
    console.print("  4. Get your Threads user ID from the Graph API Explorer\n")

    if _confirm("Open Meta Developer console in browser?", default=True):
        import webbrowser
        webbrowser.open("https://developers.facebook.com/apps")

    threads_user_id = console.input("[cyan]Threads user ID (numbers): [/cyan]").strip()
    if not threads_user_id:
        console.print("[red]❌ Threads user ID required.[/red]")
        return False

    access_token = _input_secret("Long-lived access token: ")
    if not access_token:
        console.print("[red]❌ Access token required.[/red]")
        return False

    config.threads.enabled = True
    config.threads.graph_access_token = access_token
    config.threads.threads_user_id = threads_user_id
    config.save()

    cred_store = CredentialStore(config.config_dir)
    try:
        cred_store.store("threads_access_token", access_token)
        cred_store.store("threads_user_id", threads_user_id)
    except Exception as e:
        logger.debug("Unexpected error: %s", e)

    console.print("[green]✅ Threads configured![/green]")
    return True

# ──────────────────────────────────────────────
# Messenger (static Page Access Token)
# ──────────────────────────────────────────────

def connect_messenger(config: XPSTConfig) -> bool:
    """Connect Messenger via a static Page Access Token (no refresh flow).

    Flow:
    1. Prompt for the Page Access Token (+ optional App Secret / verify token)
    2. Enable the account and persist credentials in the encrypted store
    """
    console.print(Panel("[bold]Messenger Connection[/bold]", style="blue"))
    console.print(
        "[dim]Messenger uses a static Page Access Token (long-lived) plus an App "
        "Secret for webhook signature verification. Both are stored encrypted in "
        "the CredentialStore.[/dim]\n"
    )
    console.print("[bold]Quick setup:[/bold]")
    console.print("  1. Go to: [link=https://developers.facebook.com/apps]https://developers.facebook.com/apps[/link]")
    console.print("  2. Select your app → Messenger → Settings → generate a Page Access Token")
    console.print("  3. Copy your App ID + App Secret from App → Settings → Basic")
    console.print("  4. Pick a webhook verify token (any string) and set it in the webhook config\n")

    if _confirm("Open Meta Developer console in browser?", default=True):
        import webbrowser
        webbrowser.open("https://developers.facebook.com/apps")

    page_token = _input_secret("Page Access Token (starts EAAG...): ")
    if not page_token:
        console.print("[red]❌ Page Access Token required.[/red]")
        return False

    app_secret = _input_secret("App Secret (optional, for webhook signatures): ")
    verify_token = console.input("[cyan]Webhook verify token (any string, optional): [/cyan]").strip()
    page_id = console.input("[cyan]Page ID (numeric, optional): [/cyan]").strip()
    app_id = console.input("[cyan]App ID (numeric, optional): [/cyan]").strip()

    config.messenger.enabled = True
    config.messenger.page_access_token = page_token
    if app_secret:
        config.messenger.app_secret = app_secret
    if verify_token:
        config.messenger.verify_token = verify_token
    if page_id:
        config.messenger.page_id = page_id
    if app_id:
        config.messenger.app_id = app_id
    config.save()

    cred_store = CredentialStore(config.config_dir)
    try:
        cred_store.store("messenger_page_token", page_token)
        if app_secret:
            cred_store.store("messenger_app_secret", app_secret)
    except Exception as e:
        logger.debug("Unexpected error: %s", e)

    console.print("[green]✅ Messenger configured![/green]")
    console.print(
        "[dim]Auto-reply is OFF by default. Enable it via config: "
        "accounts.messenger.auto_reply: true (plus reply_rules).[/dim]"
    )
    return True


# ──────────────────────────────────────────────
# Test connections
# ──────────────────────────────────────────────

async def test_connections(config: XPSTConfig) -> dict[str, bool]:
    """
    Test all configured platform connections.

    Returns dict of platform_name -> success_bool
    """
    results = {}

    console.print(Panel("[bold]Testing Connections[/bold]", style="blue"))

    # YouTube
    if config.youtube.enabled:
        try:
            token_file = Path(config.youtube.token_file).expanduser()
            if not token_file.exists():
                console.print("  ⚠️  YouTube: No token found")
                results["youtube"] = False
            else:
                from google.auth.transport.requests import Request
                from google.oauth2.credentials import Credentials
                from googleapiclient.discovery import build

                creds = Credentials.from_authorized_user_file(str(token_file))
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    # Save refreshed token with owner-only perms (see SECURITY.md)
                    write_text_0600(token_file, creds.to_json())

                service = build("youtube", "v3", credentials=creds)
                response = service.channels().list(part="snippet", mine=True).execute()
                channels = response.get("items", [])
                if channels:
                    name = channels[0]["snippet"]["title"]
                    console.print(f"  ✅ YouTube: {name}")
                    results["youtube"] = True
                else:
                    console.print("  ⚠️  YouTube: No channel found")
                    results["youtube"] = False
        except Exception as e:
            console.print(f"  ❌ YouTube: {str(e)[:80]}")
            results["youtube"] = False

    # Instagram — supports both Graph API and instagrapi session
    if config.instagram.enabled:
        try:
            if config.instagram.auth_mode == "graph_api":
                token = config.instagram.graph_access_token
                ig_user_id = config.instagram.graph_ig_user_id
                if not token or not ig_user_id:
                    # Try encrypted store
                    cred_store = CredentialStore(config.config_dir)
                    token = cred_store.retrieve("instagram_graph_token") or ""
                    ig_user_id = cred_store.retrieve("instagram_graph_user_id") or ""
                if not token or not ig_user_id:
                    console.print("  ⚠️  Instagram: No Graph API token configured")
                    results["instagram"] = False
                else:
                    import httpx
                    r = httpx.get(
                        f"https://graph.facebook.com/v21.0/{ig_user_id}",
                        params={"fields": "username", "access_token": token},
                        timeout=10,
                    )
                    if r.status_code == 200:
                        console.print(f"  ✅ Instagram: @{r.json().get('username', '?')} (Graph API)")
                        results["instagram"] = True
                    else:
                        console.print(f"  ❌ Instagram: Token invalid ({r.status_code})")
                        results["instagram"] = False
            else:
                # Session mode — check encrypted store first, then file
                cred_store = CredentialStore(config.config_dir)
                stored = cred_store.retrieve_json("instagram_session")
                session_file = Path(config.instagram.session_file).expanduser()
                if not stored and not session_file.exists():
                    console.print("  ⚠️  Instagram: No session found")
                    results["instagram"] = False
                else:
                    console.print("  ✅ Instagram: Session file present (instagrapi mode)")
                    results["instagram"] = True
        except Exception as e:
            console.print(f"  ❌ Instagram: {str(e)[:80]}")
            results["instagram"] = False

    # X/Twitter — check encrypted store first, then file
    if config.x.enabled:
        try:
            cred_store = CredentialStore(config.config_dir)
            stored_cookies = cred_store.retrieve_json("x_cookies")
            cookies_file = Path(config.x.cookies_file).expanduser()
            if not stored_cookies and not cookies_file.exists():
                console.print("  ⚠️  X/Twitter: No cookies found")
                results["x"] = False
            else:
                # Try actual verification
                import twikit
                client = twikit.Client("en-US")
                if stored_cookies:
                    import json as _json
                    client.load_cookies(_json.dumps(stored_cookies))
                else:
                    client.load_cookies(str(cookies_file))
                try:
                    user = await client.user()
                    console.print(f"  ✅ X/Twitter: @{user.screen_name}")
                    results["x"] = True
                except Exception:
                    console.print("  ⚠️  X/Twitter: Cookies present but may be expired")
                    results["x"] = True  # Mark as present, just expired
        except Exception as e:
            console.print(f"  ❌ X/Twitter: {str(e)[:80]}")
            results["x"] = False

    # TikTok (source only)
    try:
        import shutil
        if shutil.which("yt-dlp"):
            console.print("  ✅ TikTok: yt-dlp available")
            results["tiktok"] = True
        else:
            console.print("  ⚠️  TikTok: yt-dlp not installed")
            results["tiktok"] = False
    except Exception as e:
        logger.debug("Unexpected error: %s", e)
        results["tiktok"] = False

    return results


# ──────────────────────────────────────────────
# Main connect wizard
# ──────────────────────────────────────────────

def run_connect(platforms: list[str] | None = None, test_only: bool = False) -> bool:
    """
    Run the connection wizard.

    Args:
        platforms: List of platforms to connect (None = all)
        test_only: If True, only test existing connections

    Returns:
        True if all selected platforms connected successfully
    """
    config = XPSTConfig.load()
    _get_creds_dir(config)

    console.print()
    console.print(Panel.fit(
        "[bold blue]xPST Account Connection[/bold blue]\n"
        "Connect your social media accounts in minutes\n\n"
        "[dim]Each platform will be connected and tested automatically.[/dim]",
        border_style="blue",
    ))
    console.print()

    if test_only:
        results = asyncio.run(test_connections(config))
        # Filter results to requested platforms
        if platforms:
            results = {p: ok for p, ok in results.items() if p in platforms}
        console.print()
        if all(results.values()):
            console.print("[bold green]✅ All connections healthy![/bold green]")
        else:
            failed = [p for p, ok in results.items() if not ok]
            console.print(f"[yellow]⚠️  Issues with: {', '.join(failed)}[/yellow]")
        return all(results.values())

    # Determine which platforms to connect
    all_platforms = ["tiktok", "youtube", "instagram", "x", "threads", "messenger"]
    target_platforms = platforms or all_platforms

    # Enable platforms in config
    for p in target_platforms:
        if p != "tiktok":
            getattr(config, p).enabled = True

    results = {}
    platform_connectors = {
        "tiktok": connect_tiktok,
        "youtube": connect_youtube,
        "instagram": connect_instagram,
        "x": connect_x,
        "threads": connect_threads,
        "messenger": connect_messenger,
    }

    for platform in target_platforms:
        connector = platform_connectors.get(platform)
        if not connector:
            console.print(f"[yellow]Unknown platform: {platform}[/yellow]")
            continue

        try:
            results[platform] = connector(config)
        except KeyboardInterrupt:
            console.print(f"\n[yellow]Skipped {platform}[/yellow]")
            results[platform] = False
        except Exception as e:
            logger.error(f"Connection failed for {platform}: {e}")
            console.print(f"[red]❌ {platform.title()} connection error: {e}[/red]")
            results[platform] = False

        console.print()  # Spacing

    # Save config
    config.save()

    # Summary
    console.print(Panel("[bold]Connection Summary[/bold]", style="blue"))
    table = Table(show_header=True, header_style="bold")
    table.add_column("Platform")
    table.add_column("Status")

    for platform in target_platforms:
        if results.get(platform):
            table.add_row(platform.title(), "[green]✅ Connected[/green]")
        else:
            table.add_row(platform.title(), "[red]❌ Failed/Skipped[/red]")

    console.print(table)
    console.print()

    # Show next steps
    connected = [p for p, ok in results.items() if ok]
    failed = [p for p, ok in results.items() if not ok]

    if connected:
        console.print("[bold]Next steps:[/bold]")
        console.print("  • [cyan]xpst health[/cyan]        — Verify all connections")
        console.print("  • [cyan]xpst watch[/cyan]         — Start auto-posting")
        console.print("  • [cyan]xpst post -v VID -c 'cap'[/cyan] — Manual post")
        console.print()

    if failed:
        console.print("[yellow]To retry failed connections:[/yellow]")
        for p in failed:
            console.print(f"  • [cyan]xpst connect {p}[/cyan]")
        console.print()

    return len(failed) == 0


# ── Disconnect ────────────────────────────────────────────────────────────────

# CredentialStore keys per platform (see connect_* writers above). Only the
# ACCOUNT credentials are removed — app-level artifacts such as
# youtube_client_secrets.json are kept so reconnecting doesn't require
# re-downloading them.
_PLATFORM_CRED_KEYS: dict[str, tuple[str, ...]] = {
    "youtube": ("youtube_token",),
    "instagram": ("instagram_graph_token", "instagram_graph_user_id"),
    "x": ("x_cookies",),
    "tiktok": ("tiktok_client_secret", "tiktok_access_token", "tiktok_refresh_token"),
    "threads": ("threads_access_token", "threads_user_id"),
    "messenger": ("messenger_page_token", "messenger_app_secret"),
}

# Raw credential FILES per platform (written alongside the CredentialStore).
_PLATFORM_CRED_FILES: dict[str, tuple[str, ...]] = {
    "youtube": ("youtube_token.json",),
    "instagram": ("instagram_session.json",),
    "x": ("x_cookies.json",),
}


def disconnect_platform(
    platform: str,
    config: XPSTConfig | None = None,
) -> dict[str, Any]:
    """Disconnect a platform: remove stored credentials and disable it.

    QA-wave contract: disconnect is the inverse of connect. It removes the
    platform's stored account credentials (CredentialStore keys + raw
    session/token files + ``sessions/`` artifacts), flips
    ``accounts.<platform>.enabled`` to False, and persists the config. It
    never touches posted content or local state.

    Args:
        platform: One of tiktok, youtube, x, instagram, threads, messenger.
        config: Optional pre-loaded config (loaded from disk when omitted).

    Returns:
        dict with ``success``, ``platform``, ``removed`` (list of removed
        artifact names), and ``disabled`` (config flag flipped).
    """
    if platform not in _PLATFORM_CRED_KEYS:
        return {
            "success": False,
            "platform": platform,
            "removed": [],
            "disabled": False,
            "error": f"Unknown platform: {platform}",
        }

    if config is None:
        config = XPSTConfig.load()
    if config is None:  # pragma: no cover - defensive: load() contract
        raise RuntimeError("XPSTConfig.load() returned None")

    removed: list[str] = []

    cred_store = CredentialStore(config.config_dir)
    for key in _PLATFORM_CRED_KEYS[platform]:
        if cred_store.retrieve(key) is not None:
            if cred_store.delete(key):
                removed.append(key)

    creds_dir = Path(config.config_dir).expanduser() / CREDS_DIR_NAME
    for name in _PLATFORM_CRED_FILES.get(platform, ()):
        path = creds_dir / name
        if path.exists():
            try:
                path.unlink()
                removed.append(name)
            except OSError as exc:
                logger.warning("disconnect: could not remove %s: %s", path, exc)

    # Session artifacts written by SessionManager (cookie jars, token
    # refresh caches) under <config_dir>/sessions/.
    sessions_dir = Path(config.config_dir).expanduser() / "sessions"
    if sessions_dir.is_dir():
        for path in sessions_dir.glob(f"*{platform}*"):
            try:
                path.unlink()
                removed.append(f"sessions/{path.name}")
            except OSError as exc:
                logger.warning("disconnect: could not remove %s: %s", path, exc)

    # Disable the platform in config and persist.
    disabled = False
    accounts = getattr(config, "accounts", None)
    account = getattr(accounts, platform, None) if accounts is not None else None
    if account is not None and getattr(account, "enabled", False):
        account.enabled = False
        disabled = True
    elif account is None:
        # Fall back to the flat attribute (config.youtube.enabled etc.).
        section = getattr(config, platform, None)
        if section is not None and getattr(section, "enabled", False):
            section.enabled = False
            disabled = True
    try:
        config.save()
    except Exception as exc:  # pragma: no cover - disk-failure path
        logger.error("disconnect: config.save() failed: %s", exc)

    return {
        "success": True,
        "platform": platform,
        "removed": removed,
        "disabled": disabled,
    }
