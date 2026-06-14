"""
Interactive first-time setup wizard for xPST.

Guides users through:
1. System requirements check (ffmpeg, Python version)
2. Directory structure creation
3. Platform authentication (step by step)
4. Config generation
5. Connectivity testing
"""

import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from xpst.config import XPSTConfig
from xpst.utils.logger import get_logger
from xpst.utils.platform import get_config_dir, get_ffmpeg_name

console = Console()
logger = get_logger(__name__)

CONFIG_DIR = get_config_dir()
REQUIRED_DIRS = [
    "credentials",
    "downloads",
    "logs",
    "backups",
]


def check_ffmpeg() -> bool:
    """Check if ffmpeg is installed and accessible on PATH.

    Returns:
        True if ffmpeg binary is found, False otherwise.
    """

    return shutil.which(get_ffmpeg_name()) is not None


def check_python_version() -> tuple[bool, str]:
    """Check if the Python version meets xPST requirements (>=3.10).

    Returns:
        Tuple of (meets_requirement, version_string).
    """

    major, minor = sys.version_info[:2]
    version_str = f"{major}.{minor}"
    ok = (major == 3 and minor >= 10)
    return ok, version_str


def check_yt_dlp() -> str | None:
    """Check if yt-dlp is installed and return its version.

    Returns:
        Version string if installed, None otherwise.
    """

    try:
        result = subprocess.run(
            ["yt-dlp", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    try:
        return package_version("yt-dlp")
    except PackageNotFoundError:
        pass
    return None


def create_directory_structure() -> Path:
    """Create the xPST config directory structure with required subdirectories.

    Creates: ``credentials/``, ``downloads/``, ``logs/``, ``backups/``.

    Returns:
        Path to the created config directory.
    """

    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)

    for subdir in REQUIRED_DIRS:
        (config_dir / subdir).mkdir(exist_ok=True)

    return config_dir


def check_system_requirements() -> bool:
    """Check all system requirements and display results to console.

    Checks: Python version >= 3.10, FFmpeg installed, yt-dlp installed.

    Returns:
        True if all requirements met, False if any are missing.
    """

    console.print(Panel("[bold]System Requirements Check[/bold]", style="blue"))
    all_ok = True

    # Python version
    py_ok, py_ver = check_python_version()
    if py_ok:
        console.print(f"  ✅ Python {py_ver}")
    else:
        console.print(f"  ❌ Python {py_ver} (requires >= 3.10)")
        all_ok = False

    # ffmpeg
    if check_ffmpeg():
        try:
            result = subprocess.run(
                [get_ffmpeg_name(), "-version"],
                capture_output=True, text=True, timeout=10,
            )
            first_line = result.stdout.split("\n")[0] if result.stdout else "unknown"
            console.print(f"  ✅ FFmpeg: {first_line}")
        except Exception as e:
            logger.debug("FFmpeg version check failed: %s", e)
            console.print("  ✅ FFmpeg: installed")
    else:
        console.print("  ❌ FFmpeg not found")
        console.print("     Install with: brew install ffmpeg (macOS) / apt install ffmpeg (Linux)")
        all_ok = False

    # yt-dlp
    ytdlp_ver = check_yt_dlp()
    if ytdlp_ver:
        console.print(f"  ✅ yt-dlp {ytdlp_ver}")
    else:
        console.print("  ⚠️  yt-dlp not found (will be installed with pip)")

    console.print()
    return all_ok


def prompt_tiktok_username() -> str:
    """Prompt the user for their TikTok username.

    Returns:
        Username string (without @ prefix).
    """

    console.print(Panel("[bold]TikTok Source[/bold]", style="cyan"))
    console.print("Enter the TikTok username whose videos you want to cross-post.")
    console.print("[dim]This is the source of your content — new videos will be downloaded and posted to other platforms.[/dim]\n")
    username = console.input("[cyan]TikTok username (without @): [/cyan]").strip()
    if username.startswith("@"):
        username = username[1:]
    return username


def prompt_platform_enable() -> dict[str, bool]:
    """Prompt the user to choose which platforms to enable.

    Returns:
        Dict mapping platform name to enabled boolean.
    """

    console.print(Panel("[bold]Enable Platforms[/bold]", style="cyan"))
    console.print("Choose which platforms to post to:\n")

    platforms = {}

    platforms["youtube"] = _confirm("Enable YouTube Shorts?", default=True)
    platforms["x"] = _confirm("Enable X/Twitter?", default=True)
    platforms["instagram"] = _confirm("Enable Instagram Reels?", default=True)

    console.print()
    return platforms


def prompt_youtube_setup(creds_dir: Path) -> dict:
    """Guide the user through YouTube OAuth credential setup.

    Args:
        creds_dir: Path to the credentials directory.

    Returns:
        Dict with ``configured`` key indicating success.
    """

    console.print(Panel("[bold]YouTube Setup[/bold]", style="red"))

    secrets_path = creds_dir / "youtube_client_secrets.json"
    if secrets_path.exists():
        console.print("  ✅ YouTube client_secrets.json found!")
        console.print("  [dim]Run [cyan]xpst connect youtube[/cyan] to complete OAuth login.[/dim]")
        return {"configured": True}

    console.print(
        "YouTube requires OAuth2 credentials from Google Cloud Console.\n\n"
        "[bold]Quick Setup:[/bold]\n"
        "  1. Go to https://console.cloud.google.com/apis/credentials\n"
        "  2. Create or select a project\n"
        "  3. Click 'Create Credentials' → 'OAuth 2.0 Client ID'\n"
        "  4. Application type: 'Desktop app'\n"
        "  5. Download the JSON and save as:\n"
        f"     [bold]{secrets_path}[/bold]\n\n"
        "[dim]Then run [cyan]xpst connect youtube[/cyan] — it opens the browser for you.[/dim]"
    )

    skip = _confirm("Skip YouTube for now? (you can set it up later)", default=True)
    return {"configured": skip is False}


def prompt_x_setup(creds_dir: Path) -> dict:
    """Guide the user through X/Twitter cookie setup.

    Args:
        creds_dir: Path to the credentials directory.

    Returns:
        Dict with ``configured`` key indicating success.
    """

    console.print(Panel("[bold]X/Twitter Setup[/bold]", style="blue"))

    cookies_path = creds_dir / "x_cookies.json"
    if cookies_path.exists():
        console.print("  ✅ X cookies file found!")
        return {"configured": True}

    console.print(
        "X/Twitter can be connected with just your username, email, and password.\n\n"
        "[bold]Quickest path:[/bold]\n"
        "  Run [cyan]xpst connect x[/cyan] and enter your credentials.\n"
        "  No cookie export needed!\n\n"
        "[dim]Alternatively, export cookies manually with a browser extension.[/dim]"
    )

    skip = _confirm("Skip X/Twitter for now?", default=True)
    return {"configured": skip is False}


def prompt_instagram_setup(creds_dir: Path) -> dict:
    """Guide the user through Instagram session setup.

    Args:
        creds_dir: Path to the credentials directory.

    Returns:
        Dict with ``configured`` key indicating success.
    """

    console.print(Panel("[bold]Instagram Setup[/bold]", style="magenta"))

    session_path = creds_dir / "instagram_session.json"
    if session_path.exists():
        console.print("  ✅ Instagram session file found!")
        return {"configured": True}

    console.print(
        "Instagram can be connected with just your username and password.\n\n"
        "[bold]Quickest path:[/bold]\n"
        "  Run [cyan]xpst connect instagram[/cyan]\n"
        "  Enter username + password (supports 2FA).\n"
        "  Session is saved automatically — no DevTools needed!\n"
    )

    skip = _confirm("Skip Instagram for now?", default=True)
    return {"configured": skip is False}


def test_connectivity(config: XPSTConfig) -> bool:
    """Test connectivity to all enabled platforms.

    Checks credential file presence and yt-dlp availability without
    making actual API calls.

    Args:
        config: Loaded xPST configuration.

    Returns:
        True if all enabled platforms have credentials present.
    """

    console.print(Panel("[bold]Connectivity Test[/bold]", style="blue"))
    console.print("[dim]Testing platform connectivity...[/dim]\n")

    all_ok = True

    # TikTok source (yt-dlp)
    ytdlp_ver = check_yt_dlp()
    if ytdlp_ver:
        console.print(f"  ✅ TikTok source (yt-dlp {ytdlp_ver})")
    else:
        console.print("  ⚠️  TikTok source: yt-dlp not installed")
        all_ok = False

    # YouTube
    if config.youtube.enabled:
        secrets_path = Path(config.youtube.client_secrets).expanduser()
        if secrets_path.exists():
            console.print("  ✅ YouTube: credentials file present")
        else:
            console.print("  ⚠️  YouTube: credentials file missing (run 'xpst auth youtube')")
            all_ok = False

    # X/Twitter
    if config.x.enabled:
        cookies_path = Path(config.x.cookies_file).expanduser()
        if cookies_path.exists():
            console.print("  ✅ X/Twitter: cookies file present")
        else:
            console.print("  ⚠️  X/Twitter: cookies file missing (run 'xpst auth x')")
            all_ok = False

    # Instagram
    if config.instagram.enabled:
        session_path = Path(config.instagram.session_file).expanduser()
        if session_path.exists():
            console.print("  ✅ Instagram: session file present")
        else:
            console.print("  ⚠️  Instagram: session file missing (run 'xpst auth instagram')")
            all_ok = False

    console.print()
    return all_ok


def run_setup() -> XPSTConfig:
    """
    Run the full interactive setup wizard.

    Returns:
        Configured XPSTConfig instance
    """
    console.print()
    console.print(Panel.fit(
        "[bold blue]xPST Setup Wizard[/bold blue]\n"
        "Enterprise-grade cross-posting for short-form video\n\n"
        "[dim]This wizard will guide you through first-time configuration.[/dim]",
        border_style="blue",
    ))
    console.print()

    # Step 1: System requirements
    sys_ok = check_system_requirements()
    if not sys_ok:
        console.print("[yellow]⚠️  Some requirements are missing. You can still continue.[/yellow]\n")
        if not _confirm("Continue anyway?", default=True):
            console.print("[red]Setup aborted. Please install missing requirements and try again.[/red]")
            sys.exit(4)  # config error

    # Step 2: Create directories
    config_dir = create_directory_structure()
    creds_dir = config_dir / "credentials"
    console.print(Panel("[bold]Directory Structure[/bold]", style="green"))
    console.print(f"  ✅ Config directory: {config_dir}")
    for subdir in REQUIRED_DIRS:
        console.print(f"     ├── {subdir}/")
    console.print()

    # Step 3: TikTok source
    tiktok_username = prompt_tiktok_username()

    # Step 4: Platform selection
    platforms = prompt_platform_enable()

    # Step 5: Platform-specific setup
    setup_results = {}
    if platforms.get("youtube"):
        setup_results["youtube"] = prompt_youtube_setup(creds_dir)
    if platforms.get("x"):
        setup_results["x"] = prompt_x_setup(creds_dir)
    if platforms.get("instagram"):
        setup_results["instagram"] = prompt_instagram_setup(creds_dir)

    # Step 6: Generate config
    console.print(Panel("[bold]Generating Configuration[/bold]", style="green"))

    config = XPSTConfig()
    config.tiktok.username = tiktok_username
    config.youtube.enabled = platforms.get("youtube", False)
    config.x.enabled = platforms.get("x", False)
    config.instagram.enabled = platforms.get("instagram", False)
    config.config_dir = str(config_dir)

    # Save config
    config.save()
    console.print(f"  ✅ Config saved to: {config_dir}/config.yaml")

    # Step 7: Connectivity test
    test_connectivity(config)

    # Step 8: Summary
    console.print(Panel("[bold green]Setup Complete![/bold green]", style="green"))
    console.print()
    console.print("[bold]Configuration:[/bold]")
    console.print(f"  • Config file: {config_dir}/config.yaml")
    console.print(f"  • Credentials: {creds_dir}/")
    console.print(f"  • Logs: {config_dir}/logs/")
    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print("  1. [cyan]xpst health[/cyan]       — Test platform connectivity")
    console.print("  2. [cyan]xpst run[/cyan]          — One-time check and post")
    console.print("  3. [cyan]xpst watch[/cyan]        — Continuous monitoring mode")
    console.print("  4. [cyan]xpst post -v VIDEO -c 'caption'[/cyan] — Manual post")
    console.print()

    # Show what's still needed
    incomplete = []
    for platform, result in setup_results.items():
        if not result.get("configured"):
            incomplete.append(platform)

    if incomplete:
        console.print("[yellow]⚠️  Platforms needing setup:[/yellow]")
        for p in incomplete:
            console.print(f"  • {p.title()}: run [cyan]xpst auth {p}[/cyan]")
        console.print()

    return config


def _confirm(message: str, default: bool = True) -> bool:
    """Ask for yes/no confirmation."""
    suffix = " [Y/n]: " if default else " [y/N]: "
    response = console.input(f"[cyan]{message}{suffix}[/cyan]").strip().lower()
    if not response:
        return default
    return response in ("y", "yes")
