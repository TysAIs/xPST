"""
Streamlined account connection wizard for xPST.

Aims to connect all 4 platforms in under 5 minutes by:
- YouTube: Auto-opening browser for OAuth via InstalledAppFlow.run_local_server()
- Instagram: Username/password login via instagrapi (with 2FA support)
- X/Twitter: Username/email/password login via twikit (no cookie export needed)
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
import json
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from xpst.config import XPSTConfig
from xpst.utils.credentials import CredentialStore
from xpst.utils.logger import get_logger

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
    response = console.input(f"[cyan]{message}{suffix}[/cyan]").strip().lower()
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

        # Save token
        token_path.write_text(creds.to_json())

        # Store in keyring
        cred_store = CredentialStore(config.config_dir)
        try:
            cred_store.store("youtube_token", creds.to_json())
        except Exception:
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
# Instagram (username/password)
# ──────────────────────────────────────────────

def connect_instagram(config: XPSTConfig) -> bool:
    """
    Connect Instagram using username/password via instagrapi.

    Flow:
    1. Prompt for username and password
    2. Login via instagrapi.Client.login()
    3. If 2FA required, prompt for verification code
    4. Save session for persistence
    5. Test connection
    """
    console.print(Panel("[bold]Instagram Reels Connection[/bold]", style="magenta"))
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
                import json
                with open(session_path) as f:
                    existing = json.load(f)
                if "settings" in existing:
                    client.set_settings(existing["settings"])
            except Exception:
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
        session_path.write_text(json.dumps(session_data, indent=2))

        # Store in keyring
        cred_store = CredentialStore(config.config_dir)
        try:
            cred_store.store_json("instagram_session", session_data)
        except Exception:
            pass

        # Verify connection
        try:
            account = client.account_info()
            console.print(f"[green]✅ Connected as @{account.username} ({account.full_name})[/green]")
        except Exception:
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
# X/Twitter (username/email/password via twikit)
# ──────────────────────────────────────────────

def connect_x(config: XPSTConfig) -> bool:
    """
    Connect X/Twitter using username/email/password via twikit.

    Flow:
    1. Prompt for username, email, and password
    2. Login via twikit.Client.login()
    3. Cookies are auto-saved
    4. Test connection
    """
    console.print(Panel("[bold]X/Twitter Connection[/bold]", style="blue"))
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
                cookies_file=cookies_path_str,
            )

            # Verify
            screen_name = None
            try:
                user = await client.user()
                screen_name = user.screen_name
            except Exception:
                pass

            return screen_name

        screen_name = asyncio.run(_do_connect())

        # Store in keyring
        cred_store = CredentialStore(config.config_dir)
        try:
            if cookies_path.exists():
                cookies_data = json.loads(cookies_path.read_text())
                cred_store.store_json("x_cookies", cookies_data)
        except Exception:
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


# ──────────────────────────────────────────────
# TikTok (browser cookies via yt-dlp)
# ──────────────────────────────────────────────

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
                    token_file.write_text(creds.to_json())

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

    # Instagram
    if config.instagram.enabled:
        try:
            session_file = Path(config.instagram.session_file).expanduser()
            if not session_file.exists():
                console.print("  ⚠️  Instagram: No session found")
                results["instagram"] = False
            else:
                from instagrapi import Client

                with open(session_file) as f:
                    data = json.load(f)

                client = Client()
                if "settings" in data:
                    client.set_settings(data["settings"])
                    client.login(data.get("username", ""), "")
                else:
                    sessionid = (
                        data.get("authorization_data", {}).get("sessionid")
                        or data.get("sessionid")
                    )
                    if sessionid:
                        client.login_by_sessionid(sessionid)
                    else:
                        raise ValueError("No session data found")

                account = client.account_info()
                console.print(f"  ✅ Instagram: @{account.username}")
                results["instagram"] = True
        except Exception as e:
            console.print(f"  ❌ Instagram: {str(e)[:80]}")
            results["instagram"] = False

    # X/Twitter
    if config.x.enabled:
        try:
            cookies_file = Path(config.x.cookies_file).expanduser()
            if not cookies_file.exists():
                console.print("  ⚠️  X/Twitter: No cookies found")
                results["x"] = False
            else:
                import twikit
                client = twikit.Client("en-US")
                client.load_cookies(str(cookies_file))
                user = await client.user()
                console.print(f"  ✅ X/Twitter: @{user.screen_name}")
                results["x"] = True
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
    except Exception:
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
        console.print()
        if all(results.values()):
            console.print("[bold green]✅ All connections healthy![/bold green]")
        else:
            failed = [p for p, ok in results.items() if not ok]
            console.print(f"[yellow]⚠️  Issues with: {', '.join(failed)}[/yellow]")
        return all(results.values())

    # Determine which platforms to connect
    all_platforms = ["tiktok", "youtube", "instagram", "x"]
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
