"""
CLI interface for XPST

Provides commands for:
- `xpst run` - One-time check and post
- `xpst watch` - Monitor mode (every 15 minutes)
- `xpst post` - Manual post
- `xpst health` - Test connectivity to all platforms
- `xpst status` - Health check
- `xpst auth` - Authentication setup
- `xpst backfill` - Retry failed posts
- `xpst logs` - View logs
- `xpst setup` - Interactive first-time setup wizard
- `xpst update` - Update dependencies
- `xpst version` - Show version and dependency info

Uses Click for CLI framework with rich for beautiful output.
"""

import asyncio
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from xpst.config import XPSTConfig
from xpst.engine import CrossPostEngine, CrossPostResult
from xpst.state import StateManager
from xpst.utils.credentials import CredentialStore
from xpst.utils.logger import get_logger, setup_logging
from xpst.utils.quota import QuotaManager
from xpst.utils.sessions import SessionManager

console = Console()
logger = get_logger(__name__)


def load_config(config_path: str | None = None) -> XPSTConfig:
    """Load configuration from file with user-friendly error handling.

    Args:
        config_path: Optional path to config YAML file. Defaults to
            ``~/.xpst/config.yaml``.

    Returns:
        Validated XPSTConfig instance.

    Raises:
        SystemExit: On configuration errors (displays error and exits).
    """

    try:
        return XPSTConfig.load(config_path)
    except ValueError as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]Failed to load config:[/red] {e}")
        sys.exit(1)


@click.group()
@click.option("--config", "-c", help="Path to config file")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
@click.pass_context
def main(ctx: click.Context, config: str | None, verbose: bool):
    """
    XPST - Enterprise-grade cross-posting for short-form video

    Automatically distribute TikTok videos to YouTube Shorts, X/Twitter,
    and Instagram Reels.
    """
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config
    ctx.obj["verbose"] = verbose

    # Setup logging
    log_level = "DEBUG" if verbose else "INFO"
    setup_logging(log_level=log_level)


# ──────────────────────────────────────────────
# Setup Wizard
# ──────────────────────────────────────────────

@main.command()
def setup():
    """Interactive first-time setup wizard"""
    from xpst.setup import run_setup
    run_setup()


# ──────────────────────────────────────────────
# Update Commands
# ──────────────────────────────────────────────

@main.command()
@click.option("--check", "check_only", is_flag=True, help="Check for updates without installing")
def update(check_only: bool):
    """Update XPST dependencies to latest versions"""
    from xpst.updater import check_updates, display_update_status, update_all

    if check_only:
        console.print("[bold blue]Checking for updates...[/bold blue]\n")
        packages = check_updates()
    else:
        console.print("[bold blue]Updating dependencies...[/bold blue]\n")
        packages = update_all(check_only=False)

    console.print()
    display_update_status(packages)

    # Summary
    updatable = [p for p in packages if p.updatable]
    if updatable:
        console.print(f"\n[yellow]{len(updatable)} package(s) can be updated.[/yellow]")
        if check_only:
            console.print("[dim]Run 'xpst update' to install updates.[/dim]")
    else:
        console.print("\n[green]All packages are up to date![/green]")


@main.command()
def version():
    """Show XPST version and all dependency versions"""
    from xpst.updater import display_version_info
    display_version_info()


# ──────────────────────────────────────────────
# Core Commands
# ──────────────────────────────────────────────

@main.command()
@click.option("--bidirectional", "-b", is_flag=True, help="Check ALL sources (not just TikTok) for bidirectional cross-posting")
@click.pass_context
def run(ctx: click.Context, bidirectional: bool):
    """Check for new videos and post them"""
    config = load_config(ctx.obj.get("config_path"))
    setup_logging(
        log_level=config.monitoring.log_level,
        log_file=config.monitoring.log_file,
    )

    if bidirectional:
        console.print("[bold blue]XPST - Bidirectional cross-posting check...[/bold blue]")
        engine = CrossPostEngine(config)
        results = asyncio.run(engine.check_and_post_bidirectional())
    else:
        console.print("[bold blue]XPST - Checking for new videos...[/bold blue]")
        engine = CrossPostEngine(config)
        results = asyncio.run(engine.check_and_post())

    if not results:
        console.print("[green]No new videos to post[/green]")
        return

    # Display results
    for result in results:
        _display_result(result)


@main.command()
@click.option("--interval", "-i", default=None, type=int, help="Check interval in seconds")
@click.option("--bidirectional", "-b", is_flag=True, help="Check ALL sources for bidirectional cross-posting")
@click.pass_context
def watch(ctx: click.Context, interval: int | None, bidirectional: bool):
    """Watch for new videos (runs continuously). Delegates to Scheduler."""
    from xpst.scheduler import Scheduler

    config = load_config(ctx.obj.get("config_path"))
    setup_logging(
        log_level=config.monitoring.log_level,
        log_file=config.monitoring.log_file,
    )

    check_interval = interval or config.schedule.check_interval
    mode_label = "Bidirectional" if bidirectional else "Standard"
    console.print(f"[bold blue]XPST - {mode_label} watching every {check_interval}s (Ctrl+C to stop)[/bold blue]")

    engine = CrossPostEngine(config)

    # Crash recovery check on startup
    _check_crash_recovery(engine)

    scheduler = Scheduler(engine, config)

    # Display results after each scheduler cycle via a hook
    import time as _time
    while True:
        try:
            if bidirectional:
                # Use jittered interval for human-like timing
                jittered = engine.anti_bot.get_jittered_interval(check_interval)
                results = asyncio.run(engine.check_and_post_bidirectional())
            else:
                if scheduler._needs_catch_up():
                    console.print("[yellow]Mac was asleep. Running catch-up...[/yellow]")
                    scheduler._run_check(catch_up=True)
                else:
                    scheduler._run_check(catch_up=False)
                results = scheduler.last_results
                jittered = check_interval

            for result in results:
                _display_result(result)

            engine.state.update_last_wake_check()
            engine.state.save()

            console.print(f"[dim]Next check in {jittered:.0f}s...[/dim]")
            _time.sleep(jittered)

        except KeyboardInterrupt:
            console.print("\n[yellow]Stopped by user[/yellow]")
            break
        except Exception as e:
            logger.error(f"Error in watch loop: {e}")
            console.print(f"[red]Error:[/red] {e}")
            _time.sleep(60)


@main.command()
@click.option("--video", "-v", required=True, multiple=True, type=click.Path(exists=True), help="Video/image file path (use multiple times for carousel)")
@click.option("--caption", "-c", required=True, help="Video caption")
@click.option("--platforms", "-p", default=None, help="Comma-separated platforms (default: all)")
@click.pass_context
def post(ctx: click.Context, video: tuple[str, ...], caption: str, platforms: str | None):
    """Manually post a video or carousel (multiple --video flags)"""
    config = load_config(ctx.obj.get("config_path"))
    setup_logging(
        log_level=config.monitoring.log_level,
        log_file=config.monitoring.log_file,
    )

    media_paths = [Path(v) for v in video]
    platform_list = platforms.split(",") if platforms else None

    if len(media_paths) > 1:
        console.print(f"[bold blue]Posting carousel ({len(media_paths)} items) to: {', '.join(platform_list or ['all platforms'])}[/bold blue]")
    else:
        console.print(f"[bold blue]Posting to: {', '.join(platform_list or ['all platforms'])}[/bold blue]")

    engine = CrossPostEngine(config)

    if len(media_paths) > 1:
        result = asyncio.run(engine.post_manual_carousel(media_paths, caption, platform_list))
    else:
        result = asyncio.run(engine.post_manual(media_paths[0], caption, platform_list))

    _display_result(result)


@main.command()
@click.option("--platforms", "-p", default=None, help="Comma-separated platforms")
@click.option("--limit", "-l", default=10, help="Maximum videos to backfill")
@click.pass_context
def backfill(ctx: click.Context, platforms: str | None, limit: int):
    """Retry failed or incomplete posts"""
    config = load_config(ctx.obj.get("config_path"))
    setup_logging(
        log_level=config.monitoring.log_level,
        log_file=config.monitoring.log_file,
    )

    platform_list = platforms.split(",") if platforms else None

    console.print(f"[bold blue]Backfilling (limit: {limit})...[/bold blue]")

    engine = CrossPostEngine(config)
    results = asyncio.run(engine.backfill(platform_list, limit))

    for result in results:
        _display_result(result)


@main.command()
@click.pass_context
def status(ctx: click.Context):
    """Show health status"""
    config = load_config(ctx.obj.get("config_path"))

    console.print("[bold blue]XPST Health Status[/bold blue]\n")

    # Load state
    state = StateManager(config.config_dir)
    stats = state.get_statistics()

    # Create status table
    table = Table(title="Statistics")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Total Videos Tracked", str(stats["total_videos_tracked"]))
    table.add_row("Total Processed", str(stats["total_processed"]))
    table.add_row("Last Check", stats.get("last_check", "Never"))

    for platform, count in stats["by_platform"].items():
        table.add_row(f"  {platform.title()}", str(count))

    console.print(table)

    # Platform health
    console.print("\n[bold]Platform Health:[/bold]")
    for platform, health in stats["platform_health"].items():
        status_icon = "✅" if health["status"] == "ok" else "❌"
        console.print(f"  {status_icon} {platform.title()}: {health['status']}")
        if health.get("failures", 0) > 0:
            console.print(f"     Failures: {health['failures']}")
        if health.get("last_success"):
            console.print(f"     Last success: {health['last_success']}")

    # Dead letter queue
    dlq = state.get_dead_letter_queue()
    if dlq:
        console.print(f"\n[bold red]Dead Letter Queue ({len(dlq)} items):[/bold red]")
        for item in dlq[:5]:
            console.print(f"  - {item['video_id']} → {item['platform']}: {item.get('errors', 'Unknown')}")

    # Quota status
    quota_mgr = QuotaManager(config.config_dir)
    quota_status = quota_mgr.get_status()
    if quota_status:
        console.print("\n[bold]API Quotas:[/bold]")
        quota_table = Table(show_header=True, header_style="bold")
        quota_table.add_column("Platform")
        quota_table.add_column("Used Today")
        quota_table.add_column("Daily Limit")
        quota_table.add_column("Remaining")

        for platform_name, info in quota_status.items():
            quota_table.add_row(
                platform_name.title(),
                str(info["used_today"]),
                str(info["daily_limit"]),
                str(info["remaining"]),
            )

        console.print(quota_table)


@main.command()
@click.pass_context
def health(ctx: click.Context):
    """Test connectivity to all platforms (no uploads)"""
    config = load_config(ctx.obj.get("config_path"))
    setup_logging(
        log_level=config.monitoring.log_level,
        log_file=config.monitoring.log_file,
    )

    console.print("[bold blue]XPST - Platform Health Check[/bold blue]\n")
    console.print("[dim]Testing connectivity to all platforms (no uploads)...[/dim]\n")

    engine = CrossPostEngine(config)
    health_data = asyncio.run(engine.check_health())

    # ── TikTok Source ──
    console.print("[bold]TikTok Source:[/bold]")
    for source_name, source_health in health_data.get("sources", {}).items():
        status_ok = source_health.get("status") == "ok"
        icon = "✅" if status_ok else "❌"
        console.print(f"  {icon} {source_name.title()}")

        if source_health.get("yt_dlp_version"):
            console.print(f"     yt-dlp version: {source_health['yt_dlp_version']}")
        if source_health.get("username"):
            console.print(f"     Username: @{source_health['username']}")
        if source_health.get("cookies_available"):
            console.print("     Cookies: available")
        if not status_ok and source_health.get("error"):
            console.print(f"     [red]Error: {source_health['error']}[/red]")

    # ── Platforms ──
    console.print("\n[bold]Platforms:[/bold]")

    all_ok = True
    for platform_name, platform_health in health_data.get("platforms", {}).items():
        authenticated = platform_health.get("authenticated", False)
        session_valid = platform_health.get("session_valid", False)

        if authenticated and session_valid:
            icon = "✅"
            status_text = "[green]Connected[/green]"
        elif authenticated:
            icon = "⚠️"
            status_text = "[yellow]Session may be expired[/yellow]"
            all_ok = False
        else:
            icon = "❌"
            status_text = "[red]Not authenticated[/red]"
            all_ok = False

        console.print(f"  {icon} {platform_name.title()}: {status_text}")

        details = platform_health.get("details", {})
        if details.get("channel_name"):
            console.print(f"     Channel: {details['channel_name']}")
        if details.get("username"):
            console.print(f"     Username: @{details['username']}")
        if details.get("full_name"):
            console.print(f"     Name: {details['full_name']}")

        error = platform_health.get("error")
        if error:
            console.print(f"     [red]Error: {error}[/red]")

    # ── Circuit Breakers ──
    cb_status = health_data.get("circuit_breakers", {})
    if cb_status:
        console.print("\n[bold]Circuit Breakers:[/bold]")
        for cb_name, cb_data in cb_status.items():
            state = cb_data.get("state", "unknown")
            failures = cb_data.get("failure_count", 0)
            if state == "open":
                console.print(f"  🔴 {cb_name.title()}: OPEN ({failures} failures)")
            elif state == "half_open":
                console.print(f"  🟡 {cb_name.title()}: HALF-OPEN (testing)")
            else:
                console.print(f"  🟢 {cb_name.title()}: CLOSED (healthy)")

    # ── Quotas ──
    quota_status = health_data.get("quotas", {})
    if quota_status:
        console.print("\n[bold]API Quotas:[/bold]")
        for q_name, q_info in quota_status.items():
            remaining = q_info.get("remaining", "?")
            daily = q_info.get("daily_limit", "?")
            console.print(f"  {q_name.title()}: {remaining}/{daily} uploads remaining today")

    # ── Notifications ──
    if config.notifications.enabled:
        console.print("\n[bold]Notifications:[/bold]")
        targets = []
        if config.notifications.discord_webhook_url:
            targets.append("Discord")
        if config.notifications.telegram_bot_token:
            targets.append("Telegram")
        console.print(f"  ✅ Enabled ({', '.join(targets) if targets else 'no targets'})")
    else:
        console.print("\n[dim]Notifications: disabled[/dim]")

    # ── Summary ──
    console.print()
    if all_ok and health_data.get("platforms"):
        console.print("[bold green]✅ All platforms healthy![/bold green]")
    elif not health_data.get("platforms"):
        console.print("[yellow]⚠️ No platforms configured[/yellow]")
    else:
        console.print("[yellow]⚠️ Some platforms need attention[/yellow]")


# ──────────────────────────────────────────────
# Connect Command (streamlined wizard)
# ──────────────────────────────────────────────

@main.command()
@click.argument("platform", required=False, type=click.Choice(["tiktok", "youtube", "x", "instagram"]))
@click.option("--test", "test_only", is_flag=True, help="Test existing connections only")
@click.pass_context
def connect(ctx: click.Context, platform: str | None, test_only: bool):
    """Connect social media accounts (streamlined wizard)"""
    from xpst.connect import run_connect

    platforms = [platform] if platform else None
    success = run_connect(platforms=platforms, test_only=test_only)
    if not success:
        sys.exit(1)


# ──────────────────────────────────────────────
# Auth Commands
# ──────────────────────────────────────────────

@main.group(invoke_without_command=True)
@click.argument("platform", required=False, type=click.Choice(["tiktok", "youtube", "x", "instagram"]))
@click.pass_context
def auth(ctx: click.Context, platform: str | None):
    """Authenticate with a platform or check auth status"""
    if ctx.invoked_subcommand is not None:
        return
    if platform is None:
        # Show help if no platform and no subcommand
        click.echo(ctx.get_help())
        return

    config = load_config(ctx.obj.get("config_path"))

    console.print(f"[bold blue]Authenticating with {platform.title()}...[/bold blue]")

    if platform == "youtube":
        _auth_youtube(config)
    elif platform == "x":
        _auth_x(config)
    elif platform == "instagram":
        _auth_instagram(config)
    elif platform == "tiktok":
        _auth_tiktok(config)


@auth.command("status")
@click.pass_context
def auth_status(ctx: click.Context):
    """Show authentication and quota status for all platforms"""
    config = load_config(ctx.obj.get("config_path"))

    console.print("[bold blue]XPST Authentication Status[/bold blue]\n")

    # Credential store status
    cred_store = CredentialStore(config.config_dir)
    SessionManager(config.config_dir)
    quota_mgr = QuotaManager(config.config_dir)

    # Credential store info
    stored_keys = cred_store.list_keys()
    storage_type = "OS Keychain" if cred_store._use_keyring else "File Storage (fallback)"
    console.print(f"[bold]Credential Storage:[/bold] {storage_type}")
    console.print(f"[bold]Stored Credentials:[/bold] {len(stored_keys)}")
    for key in stored_keys:
        console.print(f"  🔑 {key}")

    # Platform auth status
    console.print("\n[bold]Platform Status:[/bold]")

    table = Table(show_header=True, header_style="bold")
    table.add_column("Platform")
    table.add_column("Auth")
    table.add_column("Quota (Daily)")
    table.add_column("Remaining")
    table.add_column("Details")

    # YouTube
    yt_creds = cred_store.retrieve("youtube_token")
    yt_file = Path(config.youtube.client_secrets).expanduser()
    yt_auth = "✅" if yt_creds or yt_file.exists() else "❌"
    yt_quota = quota_mgr.get_remaining("youtube")
    table.add_row(
        "YouTube",
        yt_auth,
        str(quota_mgr.quotas.get("youtube", {}).daily_limit if hasattr(quota_mgr.quotas.get("youtube", {}), "daily_limit") else "N/A"),
        str(yt_quota.get("daily", "N/A")),
        "Keyring" if yt_creds else ("File" if yt_file.exists() else "Not configured"),
    )

    # X/Twitter
    x_creds = cred_store.retrieve_json("x_cookies")
    x_file = Path(config.x.cookies_file).expanduser()
    x_auth = "✅" if x_creds or x_file.exists() else "❌"
    x_quota = quota_mgr.get_remaining("x")
    table.add_row(
        "X/Twitter",
        x_auth,
        str(quota_mgr.quotas.get("x", {}).daily_limit if hasattr(quota_mgr.quotas.get("x", {}), "daily_limit") else "N/A"),
        str(x_quota.get("daily", "N/A")),
        "Keyring" if x_creds else ("File" if x_file.exists() else "Not configured"),
    )

    # Instagram
    ig_creds = cred_store.retrieve_json("instagram_session")
    ig_file = Path(config.instagram.session_file).expanduser()
    ig_auth = "✅" if ig_creds or ig_file.exists() else "❌"
    ig_quota = quota_mgr.get_remaining("instagram")
    table.add_row(
        "Instagram",
        ig_auth,
        str(quota_mgr.quotas.get("instagram", {}).daily_limit if hasattr(quota_mgr.quotas.get("instagram", {}), "daily_limit") else "N/A"),
        str(ig_quota.get("daily", "N/A")),
        "Keyring" if ig_creds else ("File" if ig_file.exists() else "Not configured"),
    )

    console.print(table)


# ──────────────────────────────────────────────
# Other Commands
# ──────────────────────────────────────────────

@main.command()
@click.option("--platforms", "-p", default=None, help="Comma-separated platforms (default: all)")
@click.option("--refresh", "-r", is_flag=True, help="Force refresh (ignore cache)")
@click.pass_context
def analytics(ctx: click.Context, platforms: str | None, refresh: bool):
    """Show cross-platform analytics summary"""
    import asyncio

    config = load_config(ctx.obj.get("config_path"))

    platform_list = platforms.split(",") if platforms else None

    console.print("[bold blue]XPST - Cross-Platform Analytics[/bold blue]\n")

    # Load state to get post IDs
    from xpst.analytics import AnalyticsCollector
    from xpst.state import StateManager

    StateManager(config.config_dir)
    collector = AnalyticsCollector(config.config_dir)

    if refresh:
        collector._cache_ttl = 0  # Force cache miss

    # Discover post IDs from state
    post_ids = collector._discover_post_ids()

    if platform_list:
        post_ids = {k: v for k, v in post_ids.items() if k in platform_list}

    total_ids = sum(len(v) for v in post_ids.values())
    if total_ids == 0:
        console.print("[yellow]No posts found in state. Run `xpst run` first.[/yellow]")
        return

    console.print(f"[dim]Fetching analytics for {total_ids} posts across {len(post_ids)} platforms...[/dim]\n")

    # Collect analytics
    data = asyncio.run(collector.collect_all(post_ids))

    # Display summary table
    table = Table(title="Platform Analytics")
    table.add_column("Platform", style="cyan")
    table.add_column("Posts", style="white")
    table.add_column("Views", style="green")
    table.add_column("Likes", style="red")
    table.add_column("Comments", style="magenta")
    table.add_column("Shares", style="blue")

    totals = {"posts": 0, "views": 0, "likes": 0, "comments": 0, "shares": 0}
    for platform, posts_data in data.items():
        posts = len(posts_data)
        views = sum(m.get("views", 0) for m in posts_data.values())
        likes = sum(m.get("likes", 0) for m in posts_data.values())
        comments = sum(m.get("comments", 0) for m in posts_data.values())
        shares = sum(m.get("shares", 0) for m in posts_data.values())

        totals["posts"] += posts
        totals["views"] += views
        totals["likes"] += likes
        totals["comments"] += comments
        totals["shares"] += shares

        table.add_row(
            platform.title(),
            str(posts),
            f"{views:,}",
            f"{likes:,}",
            f"{comments:,}",
            f"{shares:,}",
        )

    table.add_section()
    table.add_row(
        "[bold]TOTAL[/bold]",
        f"[bold]{totals['posts']}[/bold]",
        f"[bold]{totals['views']:,}[/bold]",
        f"[bold]{totals['likes']:,}[/bold]",
        f"[bold]{totals['comments']:,}[/bold]",
        f"[bold]{totals['shares']:,}[/bold]",
    )

    console.print(table)

    # Top posts detail
    all_posts = []
    for platform, posts_data in data.items():
        for _post_id, metrics in posts_data.items():
            metrics["platform"] = platform
            all_posts.append(metrics)

    if all_posts:
        all_posts.sort(key=lambda p: p.get("views", 0), reverse=True)
        console.print("\n[bold]Top Posts by Views:[/bold]")
        detail_table = Table(show_header=True, header_style="bold")
        detail_table.add_column("#", style="dim")
        detail_table.add_column("Platform", style="cyan")
        detail_table.add_column("Post ID")
        detail_table.add_column("Views", style="green")
        detail_table.add_column("Likes", style="red")
        detail_table.add_column("Comments", style="magenta")

        for i, p in enumerate(all_posts[:10], 1):
            detail_table.add_row(
                str(i),
                p["platform"].title(),
                str(p.get("post_id", ""))[:20],
                f"{p.get('views', 0):,}",
                f"{p.get('likes', 0):,}",
                f"{p.get('comments', 0):,}",
            )

        console.print(detail_table)


@main.command()
@click.pass_context
def logs(ctx: click.Context):
    """View recent logs"""
    config = load_config(ctx.obj.get("config_path"))
    log_file = Path(config.monitoring.log_file).expanduser()

    if not log_file.exists():
        console.print("[yellow]No log file found[/yellow]")
        return

    # Show last 50 lines
    with open(log_file) as f:
        lines = f.readlines()
        for line in lines[-50:]:
            console.print(line.rstrip())


@main.command()
@click.argument('video_id')
@click.option('--platform', '-p', help='Platform to delete from (default: all)')
@click.option('--yes', '-y', is_flag=True, help='Skip confirmation')
@click.pass_context
def delete(ctx: click.Context, video_id: str, platform: str, yes: bool):
    """Delete a posted video from platforms"""
    import asyncio

    if not yes:
        click.confirm(f'Delete {video_id} from {platform or "all platforms"}?', abort=True)

    config = load_config(ctx.obj.get("config_path"))
    engine = CrossPostEngine(config)

    platforms = [platform] if platform else list(engine._platforms.keys())

    async def do_delete() -> None:
        """Execute deletion across all target platforms."""
        for p in platforms:
            result = await engine.delete_post(video_id, p)
            if result:
                click.echo(f'  ✓ Deleted from {p}')
            else:
                click.echo(f'  ✗ Failed to delete from {p}')

    asyncio.run(do_delete())


@main.command()
@click.option("--port", "-p", default=8080, type=int, help="Dashboard HTTP port")
@click.pass_context
def dashboard(ctx: click.Context, port: int):
    """Launch the web analytics dashboard"""
    config_path = ctx.obj.get("config_path")
    config_dir = "~/.xpst"
    if config_path:
        config_dir = str(Path(config_path).parent)
    else:
        try:
            cfg = load_config(config_path)
            config_dir = cfg.config_dir
        except Exception:
            pass

    console.print(f"[bold blue]Starting XPST Dashboard on http://localhost:{port}[/bold blue]")
    console.print("[dim]Press Ctrl+C to stop[/dim]\n")

    from xpst.dashboard.server import start_dashboard

    start_dashboard(port=port, config_dir=config_dir)


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _display_result(result: CrossPostResult) -> None:
    """Display a cross-posting result as a rich table.

    Shows per-platform status (OK/FAIL) with URLs or error messages.

    Args:
        result: CrossPostResult to display.
    """

    status = "[green]✅ Success[/green]" if result.all_success else "[yellow]⚠️  Partial[/yellow]" if result.partial_success else "[red]❌ Failed[/red]"

    console.print(f"\n[bold]{result.video_id}[/bold] - {status}")
    console.print(f"Caption: {result.caption[:50]}...")

    table = Table(show_header=True, header_style="bold")
    table.add_column("Platform")
    table.add_column("Status")
    table.add_column("URL/Error")

    for platform, upload_result in result.results.items():
        if upload_result.success:
            table.add_row(
                platform.title(),
                "[green]OK[/green]",
                upload_result.post_url or "-",
            )
        else:
            table.add_row(
                platform.title(),
                "[red]FAIL[/red]",
                upload_result.error or "Unknown error",
            )

    console.print(table)


def _check_crash_recovery(engine: CrossPostEngine) -> None:
    """Check for crash recovery on startup and queue retries.

    Examines state for partially-completed uploads (some platforms done,
    others not) and prompts the user to retry missing platforms.

    Args:
        engine: Initialized CrossPostEngine instance.
    """

    from xpst.crash_recovery import CrashRecoveryManager

    recovery = CrashRecoveryManager(engine.config.config_dir)
    retry_items = recovery.check_and_recover(engine.state)

    if retry_items:
        for item in retry_items:
            video_id = item["video_id"]
            missing_platforms = item["missing_platforms"]
            console.print(f"[dim]Queuing retry: {video_id} → {', '.join(missing_platforms)}[/dim]")


def _auth_youtube(config: XPSTConfig) -> None:
    """Guide YouTube OAuth authentication and store credentials.

    Args:
        config: Loaded XPST configuration.
    """

    creds_dir = Path(config.config_dir) / "credentials"
    cred_store = CredentialStore(config.config_dir)

    # Check if credentials file exists
    Path(config.youtube.client_secrets).expanduser()
    token_file = Path(config.youtube.token_file).expanduser()

    if token_file.exists():
        # Store token in keyring
        try:
            token_data = token_file.read_text()
            cred_store.store("youtube_token", token_data)
            console.print("[green]✅ YouTube token stored in secure keychain[/green]")
        except Exception as e:
            console.print(f"[yellow]⚠️ Could not store in keychain: {e}[/yellow]")

    console.print(f"""
[bold]YouTube Authentication[/bold]

1. Go to Google Cloud Console: https://console.cloud.google.com
2. Create or select a project
3. Enable YouTube Data API v3
4. Create OAuth 2.0 credentials (Desktop application)
5. Download client_secrets.json
6. Save to: {creds_dir}/youtube_client_secrets.json

Then run this command again to complete authentication.
""")


def _auth_x(config: XPSTConfig) -> None:
    """Guide X/Twitter cookie-based authentication and store credentials.

    Args:
        config: Loaded XPST configuration.
    """

    creds_dir = Path(config.config_dir) / "credentials"
    cred_store = CredentialStore(config.config_dir)

    # Check if cookies file exists and store in keyring
    cookies_file = Path(config.x.cookies_file).expanduser()
    if cookies_file.exists():
        try:
            import json
            cookies_data = json.loads(cookies_file.read_text())
            cred_store.store_json("x_cookies", cookies_data)
            console.print("[green]✅ X cookies stored in secure keychain[/green]")
        except Exception as e:
            console.print(f"[yellow]⚠️ Could not store in keychain: {e}[/yellow]")

    console.print(f"""
[bold]X/Twitter Authentication[/bold]

Option 1: Browser cookie export
1. Log into x.com in your browser
2. Export cookies using a cookie editor extension
3. Save to: {creds_dir}/x_cookies.json

Option 2: twikit login
1. Run: python3 -c "import twikit, asyncio; asyncio.run(twikit.Client('en-US').login('USER', 'PASS').save_cookies('cookies.json'))"
2. Move cookies.json to: {creds_dir}/x_cookies.json
""")


def _auth_instagram(config: XPSTConfig) -> None:
    """Guide Instagram session-based authentication and store credentials.

    Args:
        config: Loaded XPST configuration.
    """

    creds_dir = Path(config.config_dir) / "credentials"
    cred_store = CredentialStore(config.config_dir)

    # Check if session file exists and store in keyring
    session_file = Path(config.instagram.session_file).expanduser()
    if session_file.exists():
        try:
            import json
            session_data = json.loads(session_file.read_text())
            cred_store.store_json("instagram_session", session_data)
            console.print("[green]✅ Instagram session stored in secure keychain[/green]")
        except Exception as e:
            console.print(f"[yellow]⚠️ Could not store in keychain: {e}[/yellow]")

    console.print(f"""
[bold]Instagram Authentication[/bold]

1. Log into instagram.com in your browser
2. Open DevTools → Application → Cookies
3. Find the cookie named 'sessionid'
4. Copy its value
5. Create a JSON file at: {creds_dir}/instagram_session.json

Format:
{{{{
    "authorization_data": {{{{
        "sessionid": "YOUR_SESSION_ID"
    }}}}
}}}}
""")


def _auth_tiktok(config: XPSTConfig) -> None:
    """Guide TikTok browser cookie configuration.

    Args:
        config: Loaded XPST configuration.
    """

    console.print("""
[bold]TikTok Authentication[/bold]

TikTok doesn't require authentication for basic downloads.
However, for HD quality without watermarks:

Option 1: Browser cookies (recommended)
1. Log into tiktok.com in your browser
2. The crossposter will automatically use your browser cookies
3. Set cookies_from_browser: true in config.yaml

Option 2: Export cookies manually
1. Use a cookie editor extension
2. Save cookies to a file
3. Set cookies_file in config.yaml
""")


def confirm(message: str) -> bool:
    """Prompt the user for a yes/no confirmation.

    Args:
        message: Question to display.

    Returns:
        True if user confirmed, False otherwise.
    """

    response = console.input(f"[cyan]{message} (y/n): [/cyan]")
    return response.lower() in ("y", "yes")


if __name__ == "__main__":
    main()
