"""
CLI interface for xPST

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
import json as _json
import shutil
import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from xpst.config import XPSTConfig
from xpst.engine import CrossPostEngine, CrossPostResult
from xpst.state import StateManager
from xpst.utils.credentials import CredentialStore
from xpst.utils.logger import get_logger, setup_logging
from xpst.utils.platform import get_config_dir
from xpst.utils.quota import QuotaManager

console = Console()
logger = get_logger(__name__)


# ── Meaningful exit codes ───────────────────────
EXIT_SUCCESS = 0
EXIT_GENERAL = 1
EXIT_AUTH_FAILURE = 2
EXIT_RATE_LIMIT = 3
EXIT_CONFIG_ERROR = 4
EXIT_PLATFORM_UNAVAILABLE = 10


def json_output(data: object, as_json: bool) -> None:
    """Print *data* as JSON when ``--json`` is passed, otherwise Rich console.

    Args:
        data: Any JSON-serialisable object.
        as_json: If ``True``, print compact JSON; otherwise print nothing
            (caller is responsible for Rich output in the ``else`` branch).
    """
    if as_json:
        click.echo(_json.dumps(data, default=str, ensure_ascii=False))


# Shared Click decorator that adds ``--json`` to every command
json_option = click.option(
    "--json", "as_json", is_flag=True, help="Machine-readable JSON output"
)


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
        sys.exit(EXIT_CONFIG_ERROR)
    except Exception as e:
        console.print(f"[red]Failed to load config:[/red] {e}")
        sys.exit(EXIT_CONFIG_ERROR)


@click.group()
@click.option("--config", "-c", help="Path to config file")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
@click.option("--quiet", "-q", is_flag=True, help="Suppress decorative output")
@click.option("--json", "json_output", is_flag=True, help="Output in JSON format")
@click.version_option(version="0.1.0", prog_name="xPST")
@click.pass_context
def main(ctx: click.Context, config: str | None, verbose: bool, quiet: bool, json_output: bool):
    """
    xPST - Enterprise-grade cross-posting for short-form video

    Automatically distribute TikTok videos to YouTube Shorts, X/Twitter,
    and Instagram Reels.
    """
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config
    ctx.obj["verbose"] = verbose
    ctx.obj["quiet"] = quiet

    # Auto-enable JSON mode when stdout is not a TTY (piped)
    ctx.obj["json"] = json_output or not sys.stdout.isatty()

    # Setup logging
    log_level = "DEBUG" if verbose else "WARNING" if quiet else "INFO"
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
@click.option("--components", is_flag=True, help="Show app, helper, and provider metadata update status")
@json_option
def update(check_only: bool, components: bool, as_json: bool):
    """Update xPST dependencies to latest versions"""
    from xpst.updater import check_update_components, check_updates, display_update_status, update_all

    if components:
        status = check_update_components(include_network=check_only)
        if as_json:
            json_output(status, True)
        else:
            for section, items in status.items():
                table = Table(title=section.replace("_", " ").title())
                table.add_column("Name", style="cyan")
                table.add_column("Type")
                table.add_column("Installed")
                table.add_column("Current")
                table.add_column("Update Mode")
                table.add_column("Status")
                table.add_column("Action")
                for item in items:
                    table.add_row(
                        str(item["name"]),
                        str(item["component_type"]),
                        "yes" if item["installed"] else "no",
                        str(item["current_version"] or "unknown"),
                        str(item["update_mode"]),
                        str(item.get("status") or "unknown"),
                        str(item.get("action") or ""),
                    )
                console.print(table)
        return

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
@json_option
def version(as_json: bool):
    """Show xPST version and all dependency versions"""
    if as_json:
        from xpst.updater import PACKAGE_IMPORTS, get_installed_version, get_xpst_version
        info = {
            "xpst": get_xpst_version(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        }
        for name in PACKAGE_IMPORTS:
            info[name] = get_installed_version(name)
        json_output(info, True)
    else:
        from xpst.updater import display_version_info
        display_version_info()


# ──────────────────────────────────────────────
# Core Commands
# ──────────────────────────────────────────────

@main.command()
@click.option("--bidirectional", "-b", is_flag=True, help="Check ALL sources (not just TikTok) for bidirectional cross-posting")
@click.option("--dry-run", "dry_run", is_flag=True, help="Show what would happen without uploading")
@json_option
@click.pass_context
def run(ctx: click.Context, bidirectional: bool, dry_run: bool, as_json: bool):
    """Check for new videos and post them"""
    config = load_config(ctx.obj.get("config_path"))
    quiet = ctx.obj.get("quiet", False)
    setup_logging(
        log_level=config.monitoring.log_level,
        log_file=config.monitoring.log_file,
    )

    engine = CrossPostEngine(config)

    if dry_run:
        if bidirectional:
            monitor = engine._get_monitor()
            new_posts = asyncio.run(monitor.check_all_sources(5))
            if as_json:
                json_output({"dry_run": True, "posts": [{"source": p.source_platform, "video_id": p.video_id, "caption": p.caption[:50], "targets": list(p.target_platforms)} for p in new_posts]}, True)
            elif new_posts:
                if not quiet:
                    console.print("[bold blue]Dry run — would cross-post:[/bold blue]")
                for p in new_posts:
                    console.print(f"  {p.source_platform}:{p.video_id} → {', '.join(p.target_platforms)}")
            else:
                if not quiet:
                    console.print("[green]No new videos to post[/green]")
        else:
            videos = asyncio.run(engine.source_service.fetch_new_videos("tiktok", 5))
            new_videos = engine.source_service.filter_new(videos, engine.state, engine._platforms) if videos else []
            if as_json:
                json_output({"dry_run": True, "videos": [{"video_id": v.video_id, "caption": v.caption[:50], "targets": list(engine._platforms.keys())} for v in new_videos]}, True)
            elif new_videos:
                if not quiet:
                    console.print("[bold blue]Dry run — would post:[/bold blue]")
                for v in new_videos:
                    console.print(f"  {v.video_id}: {v.caption[:50]} → {', '.join(engine._platforms.keys())}")
            else:
                if not quiet:
                    console.print("[green]No new videos to post[/green]")
        return

    if bidirectional:
        if not as_json and not quiet:
            console.print("[bold blue]xPST - Bidirectional cross-posting check...[/bold blue]")
        results = asyncio.run(engine.check_and_post_bidirectional())
    else:
        if not as_json and not quiet:
            console.print("[bold blue]xPST - Checking for new videos...[/bold blue]")
        results = asyncio.run(engine.check_and_post())

    if not results:
        if as_json:
            json_output({"status": "no_new_videos", "results": []}, True)
        elif not quiet:
            console.print("[green]No new videos to post[/green]")
        return

    if as_json:
        out = [_result_to_dict(r) for r in results]
        json_output({"status": "ok", "results": out}, True)
    else:
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
    console.print(f"[bold blue]xPST - {mode_label} watching every {check_interval}s (Ctrl+C to stop)[/bold blue]")

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
@click.option("--dry-run", "dry_run", is_flag=True, help="Show what would happen without uploading")
@json_option
@click.pass_context
def post(ctx: click.Context, video: tuple[str, ...], caption: str, platforms: str | None, dry_run: bool, as_json: bool):
    """Manually post a video or carousel (multiple --video flags)"""
    config = load_config(ctx.obj.get("config_path"))
    quiet = ctx.obj.get("quiet", False)
    setup_logging(
        log_level=config.monitoring.log_level,
        log_file=config.monitoring.log_file,
    )

    media_paths = [Path(v) for v in video]
    platform_list = platforms.split(",") if platforms else None

    if dry_run:
        engine = CrossPostEngine(config)
        targets = platform_list or list(engine._platforms.keys())
        info = {
            "dry_run": True,
            "video": str(media_paths[0]),
            "caption": caption[:80],
            "carousel": len(media_paths) > 1,
            "items": len(media_paths),
            "targets": targets,
        }
        if as_json:
            json_output(info, True)
        else:
            if not quiet:
                console.print("[bold blue]Dry run — would post:[/bold blue]")
            console.print(f"  File: {media_paths[0]}")
            if len(media_paths) > 1:
                console.print(f"  Carousel: {len(media_paths)} items")
            console.print(f"  Caption: {caption[:80]}")
            console.print(f"  Targets: {', '.join(targets)}")
        return

    if not as_json and not quiet:
        if len(media_paths) > 1:
            console.print(f"[bold blue]Posting carousel ({len(media_paths)} items) to: {', '.join(platform_list or ['all platforms'])}[/bold blue]")
        else:
            console.print(f"[bold blue]Posting to: {', '.join(platform_list or ['all platforms'])}[/bold blue]")

    engine = CrossPostEngine(config)

    if len(media_paths) > 1:
        result = asyncio.run(engine.post_manual_carousel(media_paths, caption, platform_list))
    else:
        result = asyncio.run(engine.post_manual(media_paths[0], caption, platform_list))

    if as_json:
        json_output(_result_to_dict(result), True)
    else:
        _display_result(result)


@main.command()
@click.option("--platforms", "-p", default=None, help="Comma-separated platforms")
@click.option("--limit", "-l", default=10, help="Maximum videos to backfill")
@click.option("--dry-run", "dry_run", is_flag=True, help="Show what would be backfilled without uploading")
@json_option
@click.pass_context
def backfill(ctx: click.Context, platforms: str | None, limit: int, dry_run: bool, as_json: bool):
    """Retry failed or incomplete posts"""
    config = load_config(ctx.obj.get("config_path"))
    quiet = ctx.obj.get("quiet", False)
    setup_logging(
        log_level=config.monitoring.log_level,
        log_file=config.monitoring.log_file,
    )

    platform_list = platforms.split(",") if platforms else None

    engine = CrossPostEngine(config)

    if dry_run:
        target_platforms = platform_list or list(engine._platforms.keys())
        # Find videos that need backfilling
        candidates = []
        for video_id, video_data in list(engine.state.state["posted_videos"].items())[:limit]:
            missing = [p for p in target_platforms if p not in video_data.get("posted_to", {})]
            if missing:
                candidates.append({"video_id": video_id, "missing_platforms": missing})
        if as_json:
            json_output({"dry_run": True, "candidates": candidates}, True)
        else:
            if not quiet:
                console.print(f"[bold blue]Dry run — {len(candidates)} videos need backfilling:[/bold blue]")
            for c in candidates:
                console.print(f"  {c['video_id']} → {', '.join(c['missing_platforms'])}")
        return

    if not as_json and not quiet:
        console.print(f"[bold blue]Backfilling (limit: {limit})...[/bold blue]")

    results = asyncio.run(engine.backfill(platform_list, limit))

    if as_json:
        out = [_result_to_dict(r) for r in results]
        json_output({"status": "ok", "results": out}, True)
    else:
        for result in results:
            _display_result(result)


@main.command()
@json_option
@click.pass_context
def status(ctx: click.Context, as_json: bool):
    """Show health status"""
    config = load_config(ctx.obj.get("config_path"))

    # Load state
    state = StateManager(config.config_dir)
    stats = state.get_statistics()

    if as_json:
        json_output(stats, True)
        return

    console.print("[bold blue]xPST Health Status[/bold blue]\n")

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
@click.option("--fix", "fix_local", is_flag=True, help="Create missing local folders and save safe defaults")
@json_option
@click.pass_context
def readiness(ctx: click.Context, fix_local: bool, as_json: bool):
    """Show first-run readiness and next actions"""
    from xpst.readiness import build_readiness_report, repair_local_setup

    config = load_config(ctx.obj.get("config_path"))
    if fix_local:
        result = repair_local_setup(config, ctx.obj.get("config_path"))
        if as_json:
            json_output(result, True)
        else:
            console.print("[green]Local setup repaired[/green]")
            for action in result["actions"]:
                console.print(f"  {action}")
            console.print(result["readiness"]["summary"])
        return

    report = build_readiness_report(config)

    if as_json:
        json_output(report.to_dict(), True)
        return

    console.print("[bold blue]xPST Readiness[/bold blue]\n")
    console.print(report.summary)
    table = Table(title="Setup Checks")
    table.add_column("Status")
    table.add_column("Check", style="cyan")
    table.add_column("Message")
    table.add_column("Next Action")
    for check in report.checks:
        icon = "OK" if check.ok else "FIX" if check.severity == "error" else "WARN"
        table.add_row(icon, check.label, check.message, check.action or "-")
    console.print(table)


@main.command()
@json_option
@click.pass_context
def providers(ctx: click.Context, as_json: bool):
    """Show supported source and destination providers"""
    from xpst.platforms.base import PlatformRegistry
    from xpst.sources.base import SourceRegistry

    config = load_config(ctx.obj.get("config_path"))
    SourceRegistry.auto_discover()
    PlatformRegistry.auto_discover()
    sources = SourceRegistry.list_manifests(config)
    destinations = PlatformRegistry.list_manifests(config)
    data = {
        "sources": [manifest.to_dict() for manifest in sorted(sources, key=lambda item: item.name)],
        "destinations": [
            manifest.to_dict()
            for manifest in sorted(destinations, key=lambda item: item.name)
        ],
    }

    if as_json:
        json_output(data, True)
        return

    for title, manifests in (("Sources", data["sources"]), ("Destinations", data["destinations"])):
        table = Table(title=title)
        table.add_column("Provider", style="cyan")
        table.add_column("Auth")
        table.add_column("Official API")
        table.add_column("Capabilities")
        for manifest in manifests:
            table.add_row(
                str(manifest["display_name"]),
                str(manifest["auth_mode"]),
                "yes" if manifest["is_official_api"] else "no",
                ", ".join(str(capability) for capability in manifest["capabilities"]),
            )
        console.print(table)


@main.command()
@json_option
@click.pass_context
def health(ctx: click.Context, as_json: bool):
    """Test connectivity to all platforms (no uploads)"""
    config = load_config(ctx.obj.get("config_path"))
    setup_logging(
        log_level=config.monitoring.log_level,
        log_file=config.monitoring.log_file,
    )

    if not as_json:
        console.print("[bold blue]xPST - Platform Health Check[/bold blue]\n")
        console.print("[dim]Testing connectivity to all platforms (no uploads)...[/dim]\n")

    engine = CrossPostEngine(config)
    health_data = asyncio.run(engine.check_health())

    if as_json:
        json_output(health_data, True)
        return

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
        sys.exit(EXIT_AUTH_FAILURE)


# ──────────────────────────────────────────────
# Auth Commands
# ──────────────────────────────────────────────

@main.group(invoke_without_command=True)
@click.argument("platform", required=False)
@click.pass_context
def auth(ctx: click.Context, platform: str | None):
    """Authenticate with a platform or check auth status"""
    if ctx.invoked_subcommand is not None:
        return
    if platform is None:
        # Show help if no platform and no subcommand
        click.echo(ctx.get_help())
        return

    valid_platforms = {"tiktok", "youtube", "x", "instagram"}
    if platform not in valid_platforms:
        click.echo(f"Unknown platform: {platform}")
        click.echo(f"Valid platforms: {', '.join(sorted(valid_platforms))}")
        ctx.exit(EXIT_CONFIG_ERROR)
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
@json_option
@click.pass_context
def auth_status(ctx: click.Context, as_json: bool):
    """Show authentication and quota status for all platforms"""
    config = load_config(ctx.obj.get("config_path"))

    # Credential store status
    cred_store = CredentialStore(config.config_dir)
    quota_mgr = QuotaManager(config.config_dir)

    stored_keys = cred_store.list_keys()
    storage_type = "OS Keychain" if cred_store._use_keyring else "File Storage (fallback)"

    if as_json:
        data: dict = {
            "credential_storage": storage_type,
            "stored_credentials": stored_keys,
            "platforms": {},
        }
        yt_creds = cred_store.retrieve("youtube_token")
        x_creds = cred_store.retrieve_json("x_cookies")
        ig_creds = cred_store.retrieve_json("instagram_session")
        for plat, creds in [("youtube", yt_creds), ("x", x_creds), ("instagram", ig_creds)]:
            remaining = quota_mgr.get_remaining(plat)
            data["platforms"][plat] = {
                "authenticated": bool(creds),
                "quota_remaining": remaining.get("daily", "N/A"),
            }
        json_output(data, True)
        return

    console.print("[bold blue]xPST Authentication Status[/bold blue]\n")

    # Credential store info
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

@main.group(invoke_without_command=True)
@click.option("--platforms", "-p", default=None, help="Comma-separated platforms (default: all)")
@click.option("--refresh", "-r", is_flag=True, help="Force refresh (ignore cache)")
@json_option
@click.pass_context
def analytics(ctx: click.Context, platforms: str | None, refresh: bool, as_json: bool = False):
    """Show cross-platform analytics summary"""
    if ctx.invoked_subcommand is not None:
        return
    import asyncio

    config = load_config(ctx.obj.get("config_path"))

    platform_list = platforms.split(",") if platforms else None

    console.print("[bold blue]xPST - Cross-Platform Analytics[/bold blue]\n")

    # Load state to get post IDs
    from xpst.analytics import AnalyticsCollector

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
@json_option
@click.pass_context
def logs(ctx: click.Context, as_json: bool):
    """View recent logs"""
    config = load_config(ctx.obj.get("config_path"))
    log_file = Path(config.monitoring.log_file).expanduser()

    if not log_file.exists():
        if as_json:
            json_output({"logs": [], "error": "No log file found"}, True)
        else:
            console.print("[yellow]No log file found[/yellow]")
        return

    # Show last 50 lines
    with open(log_file) as f:
        lines = f.readlines()

    if as_json:
        json_output({"logs": [log_line.rstrip() for log_line in lines[-50:]]}, True)
    else:
        for line in lines[-50:]:
            console.print(line.rstrip())


@main.command()
@click.option("--output", "-o", default=None, type=click.Path(), help="Output zip path")
@click.option("--log-lines", default=200, type=int, show_default=True, help="Recent log lines to include")
@json_option
@click.pass_context
def diagnostics(ctx: click.Context, output: str | None, log_lines: int, as_json: bool):
    """Export a redacted local diagnostics bundle."""
    from xpst.diagnostics import build_diagnostics_bundle

    config = load_config(ctx.obj.get("config_path"))
    try:
        path = build_diagnostics_bundle(config, output=output, log_lines=log_lines)
    except OSError as e:
        if as_json:
            json_output({"ok": False, "error": str(e)}, True)
        else:
            console.print(f"[red]Failed to create diagnostics bundle:[/red] {e}")
        sys.exit(EXIT_GENERAL)

    if as_json:
        json_output({"ok": True, "output": str(path), "redacted": True}, True)
    else:
        console.print(f"[green]Diagnostics bundle written:[/green] [bold]{path}[/bold]")
        console.print("[dim]Review diagnostics.json before sharing if logs may contain private details.[/dim]")


@main.command()
@click.argument('video_id')
@click.option('--platform', '-p', help='Platform to delete from (default: all)')
@click.option('--yes', '-y', is_flag=True, help='Skip confirmation')
@json_option
@click.pass_context
def delete(ctx: click.Context, video_id: str, platform: str, yes: bool, as_json: bool):
    """Delete a posted video from platforms"""
    import asyncio

    if not yes and not as_json:
        click.confirm(f'Delete {video_id} from {platform or "all platforms"}?', abort=True)

    config = load_config(ctx.obj.get("config_path"))
    engine = CrossPostEngine(config)

    platforms = [platform] if platform else list(engine._platforms.keys())

    async def do_delete() -> list[dict]:
        """Execute deletion across all target platforms."""
        results = []
        for p in platforms:
            result = await engine.delete_post(video_id, p)
            results.append({"platform": p, "deleted": result})
            if not as_json:
                if result:
                    click.echo(f'  ✓ Deleted from {p}')
                else:
                    click.echo(f'  ✗ Failed to delete from {p}')
        return results

    results = asyncio.run(do_delete())
    if as_json:
        json_output({"video_id": video_id, "results": results}, True)


@main.command()
@click.option("--port", "-p", default=8080, type=int, help="Dashboard HTTP port")
@click.option("--host", default="127.0.0.1",
              help="Bind address (default: 127.0.0.1, loopback only)")
@click.option("--api-only", is_flag=True, default=True, hidden=True,
              help="API-only mode (default, no NiceGUI required)")
@click.pass_context
def dashboard(ctx: click.Context, port: int, host: str, api_only: bool):
    """Launch the web API dashboard"""
    config_path = ctx.obj.get("config_path")
    config_dir = str(get_config_dir())
    if config_path:
        config_dir = str(Path(config_path).parent)
    else:
        try:
            cfg = load_config(config_path)
            config_dir = cfg.config_dir
        except Exception as e:
            logger.debug("Could not load config for dashboard: %s", e)

    console.print(f"[bold blue]Starting xPST Dashboard on http://{host}:{port}[/bold blue]")
    console.print("[dim]Press Ctrl+C to stop[/dim]\n")

    from xpst.dashboard.server import start_dashboard

    start_dashboard(port=port, host=host, config_dir=config_dir)


# ──────────────────────────────────────────────
# Desktop App Command
# ──────────────────────────────────────────────

@main.command()
@click.option("--port", "-p", default=None, type=int, help="Dashboard HTTP port (default: auto-select free port)")
@click.option("--no-splash", is_flag=True, help="Skip the splash screen on startup")
@click.pass_context
def app(ctx: click.Context, port: int | None, no_splash: bool):
    """Launch xPST as a native desktop app (PySide6)"""
    config_path = ctx.obj.get("config_path")
    config_dir = str(get_config_dir())
    if config_path:
        config_dir = str(Path(config_path).parent)
    else:
        try:
            cfg = load_config(config_path)
            config_dir = cfg.config_dir
        except Exception as e:
            logger.debug("Could not load config for desktop app: %s", e)

    # Try PySide6 native desktop app first, fall back to pywebview, then browser
    try:
        from xpst.desktop_app.main import main as pyside_main
        console.print("[bold blue]Launching xPST desktop app…[/bold blue]")
        sys.exit(pyside_main(no_splash=no_splash))
    except ImportError:
        console.print("[yellow]PySide6 not installed — trying pywebview fallback.[/yellow]")
        console.print("[dim]Install with: pip install PySide6[/dim]\n")
        try:
            from xpst.desktop import launch_desktop_app
            launch_desktop_app(config_dir=config_dir, port=port)
        except ImportError:
            console.print("[yellow]pywebview not installed — falling back to browser.[/yellow]")
            from xpst.desktop import launch_browser_fallback
            launch_browser_fallback(config_dir=config_dir, port=port or 8080)
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")
            sys.exit(EXIT_PLATFORM_UNAVAILABLE)


# ──────────────────────────────────────────────
# MCP Server Command
# ──────────────────────────────────────────────

@main.command()
def mcp():
    """Start MCP (Model Context Protocol) server over stdio"""
    from xpst.mcp import cli_main
    cli_main()


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _result_to_dict(result: CrossPostResult) -> dict:
    """Convert a CrossPostResult to a JSON-serialisable dict."""
    return {
        "video_id": result.video_id,
        "caption": result.caption,
        "all_success": result.all_success,
        "partial_success": result.partial_success,
        "results": {
            platform: {
                "success": ur.success,
                "post_url": ur.post_url,
                "post_id": ur.post_id,
                "error": ur.error,
                "platform": ur.platform,
            }
            for platform, ur in result.results.items()
        },
    }


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


def _result_to_dict(result: CrossPostResult) -> dict:
    """Convert a CrossPostResult to a plain dict for JSON output."""
    platforms = {}
    for platform, ur in result.results.items():
        platforms[platform] = {
            "success": ur.success,
            "post_url": ur.post_url,
            "post_id": ur.post_id,
            "error": ur.error,
            "platform": ur.platform,
        }
    return {
        "video_id": result.video_id,
        "caption": result.caption[:80],
        "all_success": result.all_success,
        "partial_success": result.partial_success,
        "platforms": platforms,
    }


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
        config: Loaded xPST configuration.
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
        config: Loaded xPST configuration.
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
        config: Loaded xPST configuration.
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
        config: Loaded xPST configuration.
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


# ──────────────────────────────────────────────
# Config Commands
# ──────────────────────────────────────────────

def _mask_sensitive_values(data: dict | list | Any, _path: str = "") -> Any:
    """Recursively mask sensitive values (passwords, tokens, secrets, keys).

    Args:
        data: The data structure to mask.
        _path: Current key path (for determining sensitivity).

    Returns:
        Data with sensitive values replaced by '***'.
    """
    sensitive_keywords = {
        "password", "token", "secret", "key", "webhook_url",
        "cookies", "session", "credentials", "auth",
    }
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            child_path = f"{_path}.{k}" if _path else k
            is_sensitive = any(kw in k.lower() for kw in sensitive_keywords)
            if is_sensitive and isinstance(v, str) and v:
                result[k] = "***"
            else:
                result[k] = _mask_sensitive_values(v, child_path)
        return result
    elif isinstance(data, list):
        return [_mask_sensitive_values(item, _path) for item in data]
    return data


@main.group()
@click.pass_context
def config(ctx: click.Context):
    """View and manage xPST configuration"""
    pass


@config.command("show")
@click.option("--raw", is_flag=True, help="Show raw values (no masking)")
@click.option("--file", "config_file", default=None, help="Config file path")
@json_option
@click.pass_context
def config_show(ctx: click.Context, raw: bool, config_file: str | None, as_json: bool):
    """Display current configuration as YAML"""
    import os

    import yaml
    from rich.syntax import Syntax

    config_path = config_file or os.path.expanduser("~/.xpst/config.yaml")
    if not Path(config_path).exists():
        console.print(f"[red]Config file not found:[/red] {config_path}")
        sys.exit(EXIT_CONFIG_ERROR)

    with open(config_path) as f:
        raw_config = yaml.safe_load(f) or {}

    if not raw:
        raw_config = _mask_sensitive_values(raw_config)

    if as_json:
        json_output(raw_config, True)
    else:
        yaml_str = yaml.dump(raw_config, default_flow_style=False, sort_keys=False)
        syntax = Syntax(yaml_str, "yaml", theme="monokai", line_numbers=False)
        console.print(syntax)


@config.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--file", "config_file", default=None, help="Config file path")
@json_option
@click.pass_context
def config_set(ctx: click.Context, key: str, value: str, config_file: str | None, as_json: bool):
    """Set a configuration value using dotted keys.

    Examples:
        xpst config set accounts.youtube.enabled true
        xpst config set rate_limits.youtube 10
        xpst config set monitoring.log_level DEBUG
    """
    import os

    import yaml

    config_path = config_file or os.path.expanduser("~/.xpst/config.yaml")
    config_path = Path(config_path)

    # Load existing config
    if config_path.exists():
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = {}

    # Parse value: try int, then float, then bool, else string
    def _parse_value(v: str):
        # Bool
        if v.lower() in ("true", "yes", "1"):
            return True
        if v.lower() in ("false", "no", "0"):
            return False
        # Int
        try:
            return int(v)
        except ValueError:
            pass
        # Float
        try:
            return float(v)
        except ValueError:
            pass
        # String
        return v

    parsed_value = _parse_value(value)

    # Navigate dotted key and set value
    parts = key.split(".")
    current = cfg
    for part in parts[:-1]:
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]
    current[parts[-1]] = parsed_value

    # Save
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)

    if as_json:
        json_output({"key": key, "value": parsed_value, "saved": True}, True)
    else:
        console.print(f"[green]✓[/green] Set [bold]{key}[/bold] = [cyan]{parsed_value}[/cyan]")


@config.command("validate")
@click.option("--file", "config_file", default=None, help="Config file path")
@json_option
@click.pass_context
def config_validate(ctx: click.Context, config_file: str | None, as_json: bool):
    """Validate configuration for errors.

    Checks required fields, path existence, and platform config validity.
    Exit code 0 if valid, 4 if invalid.
    """
    import os

    checks: list[tuple[str, bool, str]] = []

    # Load config
    try:
        cfg = load_config(config_file)
        checks.append(("Config file loaded", True, "OK"))
    except SystemExit:
        checks.append(("Config file loaded", False, "Failed to load config"))
        if as_json:
            json_output({"valid": False, "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks]}, True)
        else:
            _print_validation_results(checks)
        sys.exit(EXIT_CONFIG_ERROR)

    # Check config file exists
    config_path = config_file or os.path.expanduser("~/.xpst/config.yaml")
    exists = Path(config_path).exists()
    checks.append(("Config file exists", exists, config_path))

    # Check download directory
    dl_dir = Path(cfg.video.download_dir).expanduser()
    dl_ok = dl_dir.exists() or dl_dir.parent.exists()
    checks.append(
        ("Download directory accessible", dl_ok, str(dl_dir))
    )

    # Check log directory
    log_dir = Path(cfg.monitoring.log_file).expanduser().parent
    log_ok = log_dir.exists() or log_dir.parent.exists()
    checks.append(
        ("Log directory accessible", log_ok, str(log_dir))
    )

    # Check platform configs
    platforms_to_check = [
        ("YouTube", cfg.youtube.enabled, cfg.youtube.client_secrets, "client_secrets"),
        ("X/Twitter", cfg.x.enabled, cfg.x.cookies_file, "cookies_file"),
        ("Instagram", cfg.instagram.enabled, cfg.instagram.session_file, "session_file"),
    ]
    for name, enabled, cred_path, field_name in platforms_to_check:
        if enabled:
            if cred_path:
                expanded = Path(cred_path).expanduser()
                cred_exists = expanded.exists()
                checks.append(
                    (f"{name} credentials", cred_exists, str(expanded))
                )
            else:
                checks.append(
                    (f"{name} credentials", False, f"{field_name} not configured")
                )
        else:
            checks.append((f"{name} enabled", False, "Platform disabled (OK)"))

    # Check rate limits are positive
    for platform, limit in [
        ("YouTube", cfg.rate_limits.youtube),
        ("Instagram", cfg.rate_limits.instagram),
        ("X", cfg.rate_limits.x),
        ("TikTok", cfg.rate_limits.tiktok),
    ]:
        ok = isinstance(limit, (int, float)) and limit > 0
        checks.append(
            (f"{platform} rate limit", ok, str(limit))
        )

    # Check schedule config
    checks.append(
        ("Check interval >= 60s", cfg.schedule.check_interval >= 60, str(cfg.schedule.check_interval))
    )

    if as_json:
        all_pass = all(ok for _, ok, _ in checks)
        json_output({
            "valid": all_pass,
            "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks],
        }, True)
    else:
        _print_validation_results(checks)

    all_pass = all(ok for _, ok, _ in checks)
    if not as_json:
        sys.exit(EXIT_SUCCESS if all_pass else EXIT_CONFIG_ERROR)


def _print_validation_results(checks: list[tuple[str, bool, str]]) -> None:
    """Print validation check results as a table.

    Args:
        checks: List of (name, passed, detail) tuples.
    """
    table = Table(title="Configuration Validation", show_lines=True)
    table.add_column("Check", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Details")

    for name, passed, detail in checks:
        status = "[green]PASS[/green]" if passed else "[red]FAIL[/red]"
        table.add_row(name, status, detail)

    console.print(table)

    passed = sum(1 for _, ok, _ in checks if ok)
    total = len(checks)
    if passed == total:
        console.print(f"\n[green]All {total} checks passed ✓[/green]")
    else:
        console.print(f"\n[red]{total - passed} of {total} checks failed ✗[/red]")


@config.command("fix")
@click.option("--file", "config_file", default=None, help="Config file path")
@click.option("--yes", "-y", is_flag=True, help="Apply fixes without confirmation")
@json_option
@click.pass_context
def config_fix(ctx: click.Context, config_file: str | None, yes: bool, as_json: bool):
    """Detect and auto-fix common configuration issues.

    Fixes: missing credentials directory, stale .crosspstr paths,
    invalid port numbers, and missing required fields.
    """
    import os

    config_path = config_file or os.path.expanduser("~/.xpst/config.yaml")
    fixes: list[str] = []

    # Load current config
    try:
        cfg = load_config(config_file)
    except Exception as e:
        if as_json:
            json_output({"ok": False, "error": str(e)}, True)
        else:
            console.print(f"[red]Cannot load config:[/red] {e}")
        sys.exit(EXIT_CONFIG_ERROR)

    # Fix 1: Ensure credentials directory exists
    cred_dir = Path("~/.xpst/credentials").expanduser()
    if not cred_dir.exists():
        fixes.append(f"Create missing credentials directory: {cred_dir}")
        if yes or as_json:
            cred_dir.mkdir(parents=True, exist_ok=True)
        elif not yes:
            console.print(f"[yellow]Missing credentials directory:[/yellow] {cred_dir}")
            if not confirm("Create it?"):
                fixes[-1] = fixes[-1] + " (skipped)"
            else:
                cred_dir.mkdir(parents=True, exist_ok=True)

    # Fix 2: Stale .crosspstr paths (already handled by _fix_legacy_paths, but check raw YAML)
    if Path(config_path).exists():
        with open(config_path) as f:
            raw_content = f.read()
        if ".crosspstr" in raw_content:
            fixes.append("Replace stale .crosspstr paths with .xpst")
            if yes or as_json:
                fixed_content = raw_content.replace(".crosspstr", ".xpst")
                with open(config_path, "w") as f:
                    f.write(fixed_content)

    # Fix 3: Invalid port numbers (must be 1-65535)
    port = cfg.monitoring.healthcheck_port
    if not (1 <= port <= 65535):
        fixes.append(f"Fix invalid healthcheck_port ({port} → 8080)")
        if yes or as_json:
            cfg.monitoring.healthcheck_port = 8080

    # Fix 4: Missing required fields with defaults
    if not cfg.video.download_dir:
        fixes.append("Set default download directory (~/.xpst/downloads)")
        if yes or as_json:
            cfg.video.download_dir = "~/.xpst/downloads"

    if not cfg.monitoring.log_file:
        fixes.append("Set default log file path (~/.xpst/logs/xpst.log)")
        if yes or as_json:
            cfg.monitoring.log_file = "~/.xpst/logs/xpst.log"

    # Save fixed config
    if fixes and (yes or as_json):
        cfg.save(config_path)

    if as_json:
        json_output({"ok": True, "fixes_applied": len(fixes), "details": fixes}, True)
    else:
        if fixes:
            console.print(f"\n[green]Applied {len(fixes)} fix(es):[/green]")
            for fix in fixes:
                console.print(f"  [green]✓[/green] {fix}")
        else:
            console.print("[green]No issues detected — configuration looks good ✓[/green]")


@config.command("export")
@click.argument("output_file", type=click.Path())
@click.option("--raw", is_flag=True, help="Export raw values (no masking)")
@click.option("--file", "config_file", default=None, help="Source config file path")
@json_option
@click.pass_context
def config_export(ctx: click.Context, output_file: str, raw: bool, config_file: str | None, as_json: bool):
    """Export current configuration to a file.

    Writes the config YAML to OUTPUT_FILE. By default masks sensitive values.
    """
    import os

    import yaml

    config_path = config_file or os.path.expanduser("~/.xpst/config.yaml")
    if not Path(config_path).exists():
        if as_json:
            json_output({"ok": False, "error": f"Config file not found: {config_path}"}, True)
        else:
            console.print(f"[red]Config file not found:[/red] {config_path}")
        sys.exit(EXIT_CONFIG_ERROR)

    with open(config_path) as f:
        raw_config = yaml.safe_load(f) or {}

    if not raw:
        raw_config = _mask_sensitive_values(raw_config)

    out_path = Path(output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        yaml.dump(raw_config, f, default_flow_style=False, sort_keys=False)

    if as_json:
        json_output({"ok": True, "exported": str(out_path), "masked": not raw}, True)
    else:
        console.print(f"[green]✓[/green] Config exported to [bold]{out_path}[/bold]")


@config.command("import")
@click.argument("input_file", type=click.Path(exists=True))
@click.option("--merge/--replace", default=True, help="Merge with existing config or replace entirely")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--strict", is_flag=True, help="Fail on validation warnings")
@json_option
@click.pass_context
def config_import(ctx: click.Context, input_file: str, merge: bool, yes: bool, strict: bool, as_json: bool):
    """Import configuration from a file.

    By default merges with existing config. Use --replace to overwrite entirely.
    Shows a diff of changes before applying. Use --yes to skip confirmation.
    Validates the imported config structure. Use --strict to fail on warnings.
    """
    import os

    import yaml

    config_path = os.path.expanduser("~/.xpst/config.yaml")

    with open(input_file) as f:
        imported = yaml.safe_load(f) or {}

    if not isinstance(imported, dict):
        if as_json:
            json_output({"ok": False, "error": "Import file must contain a YAML mapping"}, True)
        else:
            console.print("[red]Import file must contain a YAML mapping[/red]")
        sys.exit(EXIT_CONFIG_ERROR)

    # ── Validate imported config structure ──
    validation_errors, validation_warnings = _validate_config_dict(imported)

    if validation_errors:
        if as_json:
            json_output({"ok": False, "errors": validation_errors, "warnings": validation_warnings}, True)
        else:
            console.print("[red]Validation errors:[/red]")
            for err in validation_errors:
                console.print(f"  [red]✗[/red] {err}")
        sys.exit(EXIT_CONFIG_ERROR)

    if strict and validation_warnings:
        if as_json:
            json_output({"ok": False, "errors": [], "warnings": validation_warnings}, True)
        else:
            console.print("[red]Strict mode — warnings treated as errors:[/red]")
            for warn in validation_warnings:
                console.print(f"  [yellow]⚠[/yellow] {warn}")
        sys.exit(EXIT_CONFIG_ERROR)

    existing: dict = {}
    if merge and Path(config_path).exists():
        with open(config_path) as f:
            existing = yaml.safe_load(f) or {}

    # ── Compute and display diff ──
    merged = _deep_merge_import(existing, imported) if merge else imported

    changes = _compute_config_diff(existing, merged)

    if not changes["added"] and not changes["removed"] and not changes["changed"]:
        if as_json:
            json_output({"ok": True, "message": "No changes detected", "imported": str(input_file)}, True)
        else:
            console.print("[dim]No changes detected — config is identical.[/dim]")
        return

    if not as_json:
        _display_config_diff(changes, validation_warnings)

    # Confirm before applying
    if not yes and not as_json:
        if not confirm("Apply these changes?"):
            console.print("[dim]Import cancelled.[/dim]")
            return

    Path(config_path).parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        yaml.dump(merged, f, default_flow_style=False, sort_keys=False)

    if as_json:
        json_output({
            "ok": True,
            "imported": str(input_file),
            "mode": "merge" if merge else "replace",
            "added": changes["added"],
            "removed": changes["removed"],
            "changed": changes["changed"],
            "warnings": validation_warnings,
        }, True)
    else:
        mode_label = "merged" if merge else "replaced"
        console.print(f"[green]✓[/green] Config {mode_label} from [bold]{input_file}[/bold]")
        if validation_warnings:
            console.print(f"[yellow]{len(validation_warnings)} warning(s) — run `xpst config validate` for details[/yellow]")


# ──────────────────────────────────────────────
# Schedule Commands
# ──────────────────────────────────────────────

@main.group()
@click.pass_context
def schedule(ctx: click.Context):
    """Manage scheduled posts"""
    pass


@schedule.command("add")
@click.argument("file", type=click.Path())
@click.option("--caption", "-c", required=True, help="Post caption text")
@click.option("--at", "scheduled_time", required=True, help="Scheduled time (ISO or 'YYYY-MM-DD HH:MM')")
@click.option("--platforms", "-p", default=None, help="Comma-separated target platforms")
@click.option("--repeat", "repeat_rule", default=None, type=click.Choice(["none", "daily", "weekly", "monthly"]), help="Repeat schedule")
@json_option
@click.pass_context
def schedule_add(ctx: click.Context, file: str, caption: str, scheduled_time: str, platforms: str | None, repeat_rule: str | None, as_json: bool):
    """Schedule a post for later publishing.

    Examples:
        xpst schedule add video.mp4 --caption 'My video' --at '2026-06-08 10:00'
        xpst schedule add video.mp4 --caption 'My video' --at '2026-06-08T10:00:00' -p youtube,instagram
        xpst schedule add video.mp4 --caption 'My video' --at '2026-06-08 10:00' --repeat daily
    """
    from datetime import datetime

    from xpst.schedule_manager import ScheduleManager

    video_path = Path(file)
    if not video_path.exists():
        console.print(f"[red]File not found:[/red] {file}")
        sys.exit(EXIT_GENERAL)

    # Parse scheduled time
    dt = None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(scheduled_time, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        console.print(f"[red]Invalid date format:[/red] {scheduled_time}")
        console.print("[dim]Use: 'YYYY-MM-DD HH:MM' or ISO format[/dim]")
        sys.exit(EXIT_CONFIG_ERROR)

    platform_list = [p.strip() for p in platforms.split(",")] if platforms else None

    manager = ScheduleManager()
    effective_repeat = repeat_rule if repeat_rule and repeat_rule != "none" else None
    entry = manager.add(
        video_path=str(video_path.resolve()),
        caption=caption,
        scheduled_time=dt,
        platforms=platform_list,
        repeat_rule=effective_repeat,
    )

    if as_json:
        json_output(entry, True)
    else:
        console.print("[green]✓ Scheduled post[/green]")
        console.print(f"  ID:       [bold]{entry['id']}[/bold]")
        console.print(f"  File:     {video_path}")
        console.print(f"  Caption:  {caption[:60]}{'...' if len(caption) > 60 else ''}")
        console.print(f"  Time:     {dt.strftime('%Y-%m-%d %H:%M')}")
        console.print(f"  Platforms: {', '.join(platform_list) if platform_list else 'all enabled'}")
        if effective_repeat:
            console.print(f"  Repeat:   {effective_repeat}")


@schedule.command("list")
@json_option
@click.pass_context
def schedule_list(ctx: click.Context, as_json: bool):
    """List all scheduled posts"""
    from xpst.schedule_manager import ScheduleManager

    manager = ScheduleManager()
    entries = manager.list()

    if as_json:
        json_output(entries, True)
        return

    if not entries:
        console.print("[dim]No scheduled posts.[/dim]")
        return

    table = Table(title="Scheduled Posts", show_lines=True)
    table.add_column("ID", style="bold")
    table.add_column("File")
    table.add_column("Caption", max_width=40)
    table.add_column("Scheduled")
    table.add_column("Platforms")
    table.add_column("Status")

    status_styles = {
        "pending": "[yellow]pending[/yellow]",
        "completed": "[green]completed[/green]",
        "failed": "[red]failed[/red]",
    }

    for entry in entries:
        file_name = Path(entry.get("video_path", "")).name
        caption = entry.get("caption", "")[:37]
        if len(entry.get("caption", "")) > 37:
            caption += "..."
        scheduled = entry.get("scheduled_time", "")
        if "T" in scheduled:
            scheduled = scheduled.replace("T", " ").split(".")[0]
        platforms = ", ".join(entry.get("platforms", [])) or "all"
        status = status_styles.get(entry.get("status", "pending"), entry.get("status", ""))
        table.add_row(entry.get("id", "?"), file_name, caption, scheduled, platforms, status)

    console.print(table)


@schedule.command("remove")
@click.argument("entry_id")
@json_option
@click.pass_context
def schedule_remove(ctx: click.Context, entry_id: str, as_json: bool):
    """Remove a scheduled post by ID"""
    from xpst.schedule_manager import ScheduleManager

    manager = ScheduleManager()
    if manager.remove(entry_id):
        if as_json:
            json_output({"ok": True, "removed": entry_id}, True)
        else:
            console.print(f"[green]✓ Removed scheduled post [bold]{entry_id}[/bold][/green]")
    else:
        if as_json:
            json_output({"ok": False, "error": f"Post not found: {entry_id}"}, True)
        else:
            console.print(f"[red]Post not found:[/red] {entry_id}")
        sys.exit(EXIT_GENERAL)


@schedule.command("run")
@click.option("--dry-run", "dry_run", is_flag=True, help="Show what would be posted without uploading")
@json_option
@click.pass_context
def schedule_run(ctx: click.Context, dry_run: bool, as_json: bool):
    """Process all due scheduled posts.

    Fetches posts where scheduled_time <= now and status is pending,
    then posts each one. Typically called by cron or manually.
    """
    from xpst.schedule_manager import ScheduleManager

    config_obj = load_config(ctx.obj.get("config_path"))
    setup_logging(
        log_level=config_obj.monitoring.log_level,
        log_file=config_obj.monitoring.log_file,
    )

    manager = ScheduleManager()
    due = manager.get_due()

    if not due:
        if as_json:
            json_output({"status": "nothing_due", "processed": 0}, True)
        else:
            console.print("[dim]No scheduled posts are due.[/dim]")
        return

    if as_json:
        results = []
        for entry in due:
            results.append({"id": entry["id"], "status": "would_post" if dry_run else "pending"})
        json_output({"status": "dry_run" if dry_run else "processing", "count": len(due), "posts": results}, True)
        if dry_run:
            return
    else:
        console.print(f"[bold blue]Found {len(due)} due post(s)[/bold blue]")
        if dry_run:
            for entry in due:
                console.print(f"  Would post: {entry['id']} — {Path(entry['video_path']).name} → {', '.join(entry.get('platforms', ['all']))}")
            return

    engine = CrossPostEngine(config_obj)

    for entry in due:
        entry_id = entry["id"]
        video_path = Path(entry["video_path"])
        caption = entry["caption"]
        platforms = entry.get("platforms") or None

        if not video_path.exists():
            if not as_json:
                console.print(f"  [red]✗[/red] {entry_id}: file not found — {video_path}")
            manager.mark_complete(entry_id, success=False, error=f"File not found: {video_path}")
            continue

        try:
            result = asyncio.run(engine.post_manual(video_path, caption, platforms))
            success = result.all_success
            error_msg = None
            if not success:
                failed = [f"{p}: {ur.error}" for p, ur in result.results.items() if not ur.success]
                error_msg = "; ".join(failed)

            manager.mark_complete(entry_id, success=success, error=error_msg)

            if not as_json:
                if success:
                    console.print(f"  [green]✓[/green] {entry_id}: posted successfully")
                else:
                    console.print(f"  [yellow]⚠[/yellow] {entry_id}: partial — {error_msg}")
        except Exception as e:
            manager.mark_complete(entry_id, success=False, error=str(e))
            if not as_json:
                console.print(f"  [red]✗[/red] {entry_id}: {e}")

    if not as_json:
        console.print(f"\n[green]Processed {len(due)} scheduled post(s)[/green]")


@schedule.command("install")
@click.option("--interval", "-i", default=15, type=int, help="Run interval in minutes (default: 15)")
@click.option("--remove", "uninstall", is_flag=True, help="Remove the OS scheduler instead of installing")
@json_option
@click.pass_context
def schedule_install(ctx: click.Context, interval: int, uninstall: bool, as_json: bool):
    """Install an OS-level scheduler to run 'xpst schedule run' periodically.

    Creates a LaunchAgent on macOS, a scheduled task on Windows, or a
    crontab entry on Linux. Use --remove to uninstall.
    """
    import os
    import platform as _platform

    system = _platform.system()
    exe_name = "xpst.exe" if _platform.system() == "Windows" else "xpst"
    xpst_bin = os.path.realpath(os.path.join(os.path.dirname(sys.executable), exe_name))

    if uninstall:
        result = _uninstall_os_scheduler(system, xpst_bin, as_json)
    else:
        result = _install_os_scheduler(system, xpst_bin, interval, as_json)

    if not result:
        sys.exit(EXIT_GENERAL)


def _install_os_scheduler(system: str, xpst_bin: str, interval: int, as_json: bool) -> bool:
    """Install OS-specific scheduler entry. Returns True on success."""
    import os

    if system == "Darwin":
        plist_dir = Path(os.path.expanduser("~/Library/LaunchAgents"))
        plist_dir.mkdir(parents=True, exist_ok=True)
        plist_path = plist_dir / "com.xpst.schedule.plist"
        interval_sec = interval * 60
        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.xpst.schedule</string>
    <key>ProgramArguments</key>
    <array>
        <string>{xpst_bin}</string>
        <string>schedule</string>
        <string>run</string>
        <string>--quiet</string>
    </array>
    <key>StartInterval</key>
    <integer>{interval_sec}</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{get_config_dir() / "logs" / "launchagent.log"}</string>
    <key>StandardErrorPath</key>
    <string>{get_config_dir() / "logs" / "launchagent.err"}</string>
</dict>
</plist>"""
        with open(plist_path, "w") as f:
            f.write(plist_content)

        # Load the agent
        import subprocess
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        result = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True, text=True)

        if result.returncode == 0:
            if as_json:
                json_output({"ok": True, "os": "macos", "path": str(plist_path), "interval_min": interval}, True)
            else:
                console.print(f"[green]✓[/green] LaunchAgent installed: [bold]{plist_path}[/bold]")
                console.print(f"  Runs every {interval} minutes")
                console.print("  Logs: ~/.xpst/logs/launchagent.log")
                console.print("  Uninstall: [dim]xpst schedule install --remove[/dim]")
            return True
        else:
            err = result.stderr.strip()
            if as_json:
                json_output({"ok": False, "error": f"launchctl load failed: {err}"}, True)
            else:
                console.print(f"[red]Failed to load LaunchAgent:[/red] {err}")
            return False

    elif system == "Linux":
        import subprocess

        if shutil.which("crontab") is None:
            raise RuntimeError(
                "crontab not found on PATH. Install cron to use scheduled posting on "
                "Linux (e.g. 'sudo apt install cron' or 'sudo dnf install cronie')."
            )

        # cron does NOT expand '~', so the log path must be fully resolved.
        cron_log = get_config_dir() / "logs" / "cron.log"
        cron_line = f"*/{interval} * * * * {xpst_bin} schedule run --quiet >> {cron_log} 2>&1"

        # Read existing crontab
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        lines = existing.stdout.strip().split("\n") if existing.returncode == 0 and existing.stdout.strip() else []
        marker = "# xPST schedule runner"

        # Remove old xpst entry if present
        filtered = []
        skip_next = False
        for line in lines:
            if marker in line:
                skip_next = True
                continue
            if skip_next and "xpst schedule run" in line:
                skip_next = False
                continue
            skip_next = False
            filtered.append(line)

        filtered.append(marker)
        filtered.append(cron_line)
        new_crontab = "\n".join(filtered) + "\n"

        proc = subprocess.run(["crontab", "-"], input=new_crontab, capture_output=True, text=True)
        if proc.returncode == 0:
            if as_json:
                json_output({"ok": True, "os": "linux", "interval_min": interval}, True)
            else:
                console.print(f"[green]✓[/green] Crontab entry installed (every {interval} minutes)")
                console.print("  Logs: ~/.xpst/logs/cron.log")
                console.print("  Uninstall: [dim]xpst schedule install --remove[/dim]")
            return True
        else:
            if as_json:
                json_output({"ok": False, "error": proc.stderr.strip()}, True)
            else:
                console.print(f"[red]Failed to set crontab:[/red] {proc.stderr.strip()}")
            return False

    elif system == "Windows":
        import subprocess
        task_name = "XpstScheduleRun"
        cmd = f'"{xpst_bin}" schedule run --quiet'

        result = subprocess.run(
            ["schtasks", "/Create", "/TN", task_name, "/TR", cmd,
             "/SC", "MINUTE", "/MO", str(interval), "/F"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            if as_json:
                json_output({"ok": True, "os": "windows", "task": task_name, "interval_min": interval}, True)
            else:
                console.print(f"[green]✓[/green] Scheduled task '{task_name}' created (every {interval} minutes)")
                console.print("  Uninstall: [dim]xpst schedule install --remove[/dim]")
            return True
        else:
            if as_json:
                json_output({"ok": False, "error": result.stderr.strip()}, True)
            else:
                console.print(f"[red]Failed to create task:[/red] {result.stderr.strip()}")
            return False

    else:
        if as_json:
            json_output({"ok": False, "error": f"Unsupported OS: {system}"}, True)
        else:
            console.print(f"[red]Unsupported OS:[/red] {system}")
        return False


def _uninstall_os_scheduler(system: str, xpst_bin: str, as_json: bool) -> bool:
    """Remove OS-specific scheduler entry. Returns True on success."""
    import os
    import subprocess

    if system == "Darwin":
        plist_path = Path(os.path.expanduser("~/Library/LaunchAgents/com.xpst.schedule.plist"))
        if not plist_path.exists():
            if as_json:
                json_output({"ok": False, "error": "LaunchAgent not found"}, True)
            else:
                console.print("[yellow]LaunchAgent not found — nothing to remove[/yellow]")
            return True

        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        plist_path.unlink()
        if as_json:
            json_output({"ok": True, "removed": str(plist_path)}, True)
        else:
            console.print(f"[green]✓[/green] LaunchAgent removed: {plist_path}")
        return True

    elif system == "Linux":
        if shutil.which("crontab") is None:
            raise RuntimeError(
                "crontab not found on PATH. Install cron to manage scheduled posting on Linux."
            )

        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if existing.returncode != 0 or not existing.stdout.strip():
            if as_json:
                json_output({"ok": True, "message": "No crontab entries found"}, True)
            else:
                console.print("[yellow]No crontab entries found[/yellow]")
            return True

        lines = existing.stdout.strip().split("\n")
        filtered = []
        skip_next = False
        for line in lines:
            if "# xPST schedule runner" in line:
                skip_next = True
                continue
            if skip_next and "xpst schedule run" in line:
                skip_next = False
                continue
            skip_next = False
            filtered.append(line)

        new_crontab = "\n".join(filtered) + "\n" if filtered else ""
        if new_crontab.strip():
            subprocess.run(["crontab", "-"], input=new_crontab, capture_output=True, text=True)
        else:
            subprocess.run(["crontab", "-r"], capture_output=True, text=True)

        if as_json:
            json_output({"ok": True, "removed": "crontab entry"}, True)
        else:
            console.print("[green]✓[/green] xPST crontab entry removed")
        return True

    elif system == "Windows":
        import subprocess
        task_name = "XpstScheduleRun"
        result = subprocess.run(
            ["schtasks", "/Delete", "/TN", task_name, "/F"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            if as_json:
                json_output({"ok": True, "removed": task_name}, True)
            else:
                console.print(f"[green]✓[/green] Scheduled task '{task_name}' removed")
            return True
        else:
            if as_json:
                json_output({"ok": False, "error": result.stderr.strip()}, True)
            else:
                console.print(f"[red]Failed to delete task:[/red] {result.stderr.strip()}")
            return False

    else:
        if as_json:
            json_output({"ok": False, "error": f"Unsupported OS: {system}"}, True)
        else:
            console.print(f"[red]Unsupported OS:[/red] {system}")
        return False


def _deep_merge_import(base: dict, override: dict) -> dict:
    """Recursively merge override into base for config import."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge_import(result[k], v)
        else:
            result[k] = v
    return result


def _flatten_dict(d: dict, prefix: str = "") -> dict[str, Any]:
    """Flatten a nested dict to dotted-key paths."""
    items: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            items.update(_flatten_dict(v, key))
        else:
            items[key] = v
    return items


_KNOWN_CONFIG_KEYS = {
    "accounts", "video", "reliability", "monitoring", "notifications",
    "rate_limits", "schedule",
}

_KNOWN_ACCOUNT_PLATFORMS = {"tiktok", "youtube", "x", "instagram", "local"}

_KNOWN_ENCODING_KEYS = {"resolution", "crf", "bitrate", "maxrate", "bufsize", "profile", "level", "gop", "fps", "color", "pix_fmt", "passthrough"}


def _validate_config_dict(data: dict) -> tuple[list[str], list[str]]:
    """Validate an imported config dict against known XPSTConfig structure.

    Returns:
        (errors, warnings) — errors block import, warnings are informational.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(data, dict):
        errors.append("Config must be a YAML mapping (dict)")
        return errors, warnings

    # Check for unknown top-level keys
    for key in data:
        if key not in _KNOWN_CONFIG_KEYS:
            warnings.append(f"Unknown top-level key: '{key}'")

    # Validate accounts
    accounts = data.get("accounts", {})
    if accounts and isinstance(accounts, dict):
        for plat in accounts:
            if plat not in _KNOWN_ACCOUNT_PLATFORMS:
                warnings.append(f"Unknown platform in accounts: '{plat}'")

    # Validate encoding sections
    video = data.get("video", {})
    if video and isinstance(video, dict):
        encoding = video.get("encoding", {})
        if encoding and isinstance(encoding, dict):
            for plat, enc in encoding.items():
                if isinstance(enc, dict):
                    for key in enc:
                        if key not in _KNOWN_ENCODING_KEYS:
                            warnings.append(f"Unknown encoding key for {plat}: '{key}'")

    # Validate required top-level types
    for key in ("rate_limits", "reliability", "monitoring", "schedule"):
        if key in data and not isinstance(data[key], dict):
            errors.append(f"'{key}' must be a mapping, got {type(data[key]).__name__}")

    return errors, warnings


def _compute_config_diff(old: dict, new: dict) -> dict[str, list[str]]:
    """Compute added, removed, and changed keys between two config dicts."""
    old_flat = _flatten_dict(old)
    new_flat = _flatten_dict(new)

    old_keys = set(old_flat.keys())
    new_keys = set(new_flat.keys())

    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    changed = sorted(k for k in old_keys & new_keys if old_flat[k] != new_flat[k])

    # Store old/new values for changed keys so display can show them
    old_values = {k: old_flat[k] for k in changed}
    new_values = {k: new_flat[k] for k in changed}

    return {"added": added, "removed": removed, "changed": changed, "_old_values": old_values, "_new_values": new_values}


def _display_config_diff(changes: dict[str, list[str]], warnings: list[str]) -> None:
    """Display config import diff to console with color-coded changes."""
    if changes["added"]:
        console.print("\n[green]Added keys:[/green]")
        for key in changes["added"]:
            console.print(f"  [green]+[/green] {key}")
    if changes["removed"]:
        console.print("\n[red]Removed keys:[/red]")
        for key in changes["removed"]:
            console.print(f"  [red]-[/red] {key}")
    if changes["changed"]:
        console.print("\n[yellow]Changed values:[/yellow]")
        for key in changes["changed"]:
            old_val = changes.get("_old_values", {}).get(key, "?")
            new_val = changes.get("_new_values", {}).get(key, "?")
            console.print(f"  [yellow]~[/yellow] {key}: [red]{old_val}[/red] → [green]{new_val}[/green]")
    if warnings:
        console.print("\n[yellow]Warnings:[/yellow]")
        for w in warnings:
            console.print(f"  [yellow]⚠[/yellow] {w}")
    console.print()


# ──────────────────────────────────────────────
# Analytics Export Command
# ──────────────────────────────────────────────

@analytics.command("export")
@click.option("--format", "fmt", default="json", type=click.Choice(["json", "csv"]), help="Export format")
@click.option("--output", "-o", required=True, type=click.Path(), help="Output file path")
@click.option("--platforms", "-p", default=None, help="Comma-separated platforms (default: all)")
@click.option("--refresh", "-r", is_flag=True, help="Force refresh (ignore cache)")
@json_option
@click.pass_context
def analytics_export(ctx: click.Context, fmt: str, output: str, platforms: str | None, refresh: bool, as_json: bool):
    """Export analytics data to a file.

    Collects engagement metrics from all platforms and writes to JSON or CSV.
    """
    import asyncio
    import csv as csv_mod
    from datetime import datetime as _dt

    config = load_config(ctx.obj.get("config_path"))

    from xpst.analytics import AnalyticsCollector

    collector = AnalyticsCollector(config.config_dir)
    if refresh:
        collector._cache_ttl = 0

    post_ids = collector._discover_post_ids()
    if platforms:
        platform_list = [p.strip() for p in platforms.split(",")]
        post_ids = {k: v for k, v in post_ids.items() if k in platform_list}

    total_ids = sum(len(v) for v in post_ids.values())
    if total_ids == 0:
        if as_json:
            json_output({"ok": False, "error": "No posts found in state"}, True)
        else:
            console.print("[yellow]No posts found in state. Run `xpst run` first.[/yellow]")
        return

    data = asyncio.run(collector.collect_all(post_ids))
    totals = collector.get_total_metrics(data)
    platform_totals = collector.get_platform_totals(data)

    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "json":
        export_data = {
            "platforms": {p: {pid: m for pid, m in posts.items()} for p, posts in data.items()},
            "totals": totals,
            "platform_totals": platform_totals,
            "exported_at": _dt.now().isoformat(),
        }
        with open(out_path, "w") as f:
            _json.dump(export_data, f, indent=2, default=str, ensure_ascii=False)
    elif fmt == "csv":
        import io
        with open(out_path, "wb") as raw_f:
            # Write UTF-8 BOM for Excel compatibility
            raw_f.write(b"\xef\xbb\xbf")
            f = io.TextIOWrapper(raw_f, encoding="utf-8", newline="")
            writer = csv_mod.writer(f)
            writer.writerow(["platform", "post_id", "caption", "views", "likes", "comments", "shares", "saves", "timestamp"])
            for platform, posts in data.items():
                for post_id, metrics in posts.items():
                    writer.writerow([
                        platform,
                        post_id,
                        metrics.get("caption", ""),
                        metrics.get("views", 0),
                        metrics.get("likes", 0),
                        metrics.get("comments", 0),
                        metrics.get("shares", 0),
                        metrics.get("saves", 0),
                        metrics.get("timestamp", ""),
                    ])
            f.detach()  # prevent TextIOWrapper from closing raw_f twice

    if as_json:
        json_output({"ok": True, "format": fmt, "output": str(out_path), "totals": totals}, True)
    else:
        console.print(f"[green]✓[/green] Analytics exported to [bold]{out_path}[/bold] ({fmt})")
        console.print(f"  Views: {totals['views']:,}  Likes: {totals['likes']:,}  Comments: {totals['comments']:,}  Shares: {totals['shares']:,}")


# ──────────────────────────────────────────────
# Plugins Group
# ──────────────────────────────────────────────

@main.group()
@click.pass_context
def plugins(ctx: click.Context):
    """Manage xPST plugins"""
    pass


@plugins.command("docs")
@click.option("--output", "-o", default=None, type=click.Path(), help="Output file (default: stdout)")
@json_option
@click.pass_context
def plugins_docs(ctx: click.Context, output: str | None, as_json: bool):
    """Generate markdown documentation for installed plugins.

    Introspects all installed plugins and produces a markdown file
    documenting each plugin's name, description, capabilities, and
    configuration options.
    """
    from xpst.plugins import PluginManager

    pm = PluginManager()
    pm.discover()
    plugin_list = pm.list_plugins()

    if not plugin_list:
        if as_json:
            json_output({"ok": True, "plugins": 0, "message": "No plugins installed"}, True)
        else:
            console.print("[dim]No plugins installed. Place .py files in ~/.xpst/plugins/[/dim]")
        return

    # Build markdown
    from datetime import datetime as _dt

    lines: list[str] = []
    lines.append("# xPST Plugin Documentation\n")
    lines.append(f"Auto-generated on {_dt.now().strftime('%Y-%m-%d %H:%M')}\n")
    lines.append(f"**{len(plugin_list)} plugin(s) installed**\n")
    lines.append("---\n")

    for pinfo in plugin_list:
        name = pinfo.get("name", "unknown")
        lines.append(f"## {name}\n")
        lines.append(f"- **Version:** {pinfo.get('version', 'unknown')}")
        lines.append(f"- **Description:** {pinfo.get('description', 'No description')}")
        caps = []
        if pinfo.get("has_uploader"):
            caps.append("Uploader")
        if pinfo.get("has_source"):
            caps.append("Source")
        lines.append(f"- **Capabilities:** {', '.join(caps) if caps else 'None'}")

        # Try to get more details from the plugin
        plugin_data = pm.get(name)
        if plugin_data:
            config_opts = plugin_data.get("config_options", [])
            if config_opts:
                lines.append("- **Config Options:**")
                for opt in config_opts:
                    if isinstance(opt, dict):
                        opt_name = opt.get("name", "?")
                        opt_desc = opt.get("description", "")
                        opt_default = opt.get("default", "")
                        lines.append(f"  - `{opt_name}`: {opt_desc} (default: `{opt_default}`)")
                    else:
                        lines.append(f"  - `{opt}`")
        lines.append("")

    md_content = "\n".join(lines)

    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            f.write(md_content)
        if as_json:
            json_output({"ok": True, "output": str(out_path), "plugins": len(plugin_list)}, True)
        else:
            console.print(f"[green]✓[/green] Plugin docs written to [bold]{out_path}[/bold]")
    else:
        if as_json:
            json_output({"ok": True, "content": md_content, "plugins": len(plugin_list)}, True)
        else:
            console.print(md_content)


@plugins.command("list")
@json_option
@click.pass_context
def plugins_list(ctx: click.Context, as_json: bool):
    """List installed plugins."""
    from xpst.plugins import PluginManager

    pm = PluginManager()
    pm.discover()
    plugin_list = pm.list_plugins()

    if as_json:
        json_output({"plugins": plugin_list, "count": len(plugin_list)}, True)
        return

    if not plugin_list:
        console.print("[dim]No plugins installed.[/dim]")
        return

    table = Table(title="Installed Plugins", show_lines=True)
    table.add_column("Name", style="bold")
    table.add_column("Version")
    table.add_column("Description", max_width=50)
    table.add_column("Uploader")
    table.add_column("Source")

    for p in plugin_list:
        table.add_row(
            p["name"],
            p.get("version", "?"),
            p.get("description", ""),
            "✓" if p.get("has_uploader") else "✗",
            "✓" if p.get("has_source") else "✗",
        )

    console.print(table)


# ──────────────────────────────────────────────
# Build Command
# ──────────────────────────────────────────────

@main.command()
@click.option("--target", default=None, type=click.Choice(["macos", "windows", "linux"]), help="Target OS (default: current OS)")
@click.option("--spec-file", default=None, type=click.Path(), help="PyInstaller .spec file path")
@json_option
@click.pass_context
def build(ctx: click.Context, target: str | None, spec_file: str | None, as_json: bool):
    """Build a standalone executable using PyInstaller.

    Auto-detects the appropriate .spec file for the current OS (or --target).
    Checks for PyInstaller and offers to install if missing.
    Supports cross-compilation via Docker when --target differs from current OS.
    Streams PyInstaller output in real-time.
    """
    import platform as _platform
    import shutil
    import subprocess

    # Determine current OS
    system = _platform.system()
    if system == "Darwin":
        current_os = "macos"
    elif system == "Windows":
        current_os = "windows"
    else:
        current_os = "linux"

    # Determine target OS
    target_os = target if target else current_os

    # Cross-compilation: if target differs from current OS, use Docker
    use_docker = target is not None and target_os != current_os

    # Find spec file
    if spec_file:
        spec_path = Path(spec_file)
        if not spec_path.exists():
            if as_json:
                json_output({"ok": False, "error": f"Spec file not found: {spec_file}"}, True)
            else:
                console.print(f"[red]Spec file not found:[/red] {spec_file}")
            sys.exit(EXIT_GENERAL)
    else:
        # Auto-detect spec file
        spec_map = {
            "macos": "build_macos.spec",
            "windows": "build_windows.spec",
            "linux": "build_linux.spec",
        }
        spec_name = spec_map.get(target_os)
        spec_path = Path.cwd() / spec_name
        if not spec_path.exists():
            if as_json:
                json_output({"ok": False, "error": f"Spec file not found: {spec_path}"}, True)
            else:
                console.print(f"[red]Spec file not found:[/red] {spec_path}")
            sys.exit(EXIT_GENERAL)

    # Docker-based cross-compilation
    if use_docker:
        docker_bin = shutil.which("docker")
        if not docker_bin:
            if as_json:
                json_output({"ok": False, "error": "Docker is required for cross-compilation. Install Docker and try again."}, True)
            else:
                console.print("[red]Docker is required for cross-compilation.[/red]")
                console.print("[dim]Install Docker: https://docs.docker.com/get-docker/[/dim]")
            sys.exit(EXIT_GENERAL)

        # Map target OS to Docker image
        docker_images = {
            "macos": "ghcr.io/cdrx/pyinstaller-windows:latest",  # macOS not natively possible in Docker
            "windows": "ghcr.io/cdrx/pyinstaller-windows:latest",
            "linux": "ghcr.io/cdrx/pyinstaller-linux:latest",
        }
        docker_image = docker_images.get(target_os, "python:3.11-slim")

        if not as_json:
            console.print(f"[bold blue]Cross-compiling for {target_os} via Docker...[/bold blue]")
            console.print(f"  Docker image: {docker_image}")
            console.print(f"  Spec file: {spec_path}\n")

        docker_cmd = [
            docker_bin, "run", "--rm",
            "-v", f"{Path.cwd()}:/src",
            "-w", "/src",
            docker_image,
            "pyinstaller", "--clean", "--noconfirm", str(spec_path),
        ]

        # Stream output in real-time
        try:
            proc = subprocess.Popen(
                docker_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            output_lines: list[str] = []
            for line in proc.stdout:  # type: ignore[union-attr]
                output_lines.append(line)
                if not as_json:
                    console.print(f"  [dim]{line.rstrip()}[/dim]")
            proc.wait()

            if proc.returncode == 0:
                dist_dir = Path.cwd() / "dist"
                if as_json:
                    json_output({"ok": True, "target": target_os, "spec_file": str(spec_path), "dist_dir": str(dist_dir), "docker": True}, True)
                else:
                    console.print(f"\n[green]✓[/green] Cross-compilation complete for [bold]{target_os}[/bold]")
                    console.print(f"  Output: {dist_dir}")
            else:
                stderr_text = "".join(output_lines)[-500:] if output_lines else "Build failed"
                if as_json:
                    json_output({"ok": False, "error": stderr_text}, True)
                else:
                    console.print("\n[red]Cross-compilation failed:[/red]")
                    console.print(stderr_text)
                sys.exit(EXIT_GENERAL)
        except FileNotFoundError:
            console.print("[red]Docker not found or not running.[/red]")
            sys.exit(EXIT_GENERAL)
        return

    # Local build (same OS)
    # Check for PyInstaller
    pyinstaller_bin = shutil.which("pyinstaller")
    if not pyinstaller_bin:
        exe_name = "pyinstaller.exe" if current_os == "windows" else "pyinstaller"
        candidate = Path(sys.executable).resolve().parent / exe_name
        if candidate.exists():
            pyinstaller_bin = str(candidate)
    if not pyinstaller_bin:
        # Check if it's in the current venv
        if as_json:
            json_output({"ok": False, "error": "PyInstaller not found. Install with: pip install pyinstaller"}, True)
            sys.exit(EXIT_GENERAL)
        else:
            console.print("[yellow]PyInstaller not found.[/yellow]")
            if confirm("Install PyInstaller now?"):
                console.print("[dim]Installing PyInstaller...[/dim]")
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "pyinstaller"],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    console.print(f"[red]Installation failed:[/red] {result.stderr}")
                    sys.exit(EXIT_GENERAL)
                console.print("[green]✓[/green] PyInstaller installed")
                pyinstaller_bin = shutil.which("pyinstaller")
                if not pyinstaller_bin:
                    exe_name = "pyinstaller.exe" if current_os == "windows" else "pyinstaller"
                    candidate = Path(sys.executable).resolve().parent / exe_name
                    if candidate.exists():
                        pyinstaller_bin = str(candidate)
            else:
                sys.exit(EXIT_GENERAL)

    if not pyinstaller_bin:
        if as_json:
            json_output({"ok": False, "error": "PyInstaller installed but executable was not found"}, True)
        else:
            console.print("[red]PyInstaller installed but executable was not found.[/red]")
        sys.exit(EXIT_GENERAL)

    if not as_json:
        console.print(f"[bold blue]Building xPST for {target_os}...[/bold blue]")
        console.print(f"  Spec file: {spec_path}")
        console.print(f"  PyInstaller: {pyinstaller_bin}\n")

    # Run PyInstaller with real-time streaming output
    cmd = [pyinstaller_bin, "--clean", "--noconfirm", str(spec_path)]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    output_lines = []
    for line in proc.stdout:  # type: ignore[union-attr]
        output_lines.append(line)
        if not as_json:
            console.print(f"  [dim]{line.rstrip()}[/dim]")
    proc.wait()

    if proc.returncode == 0:
        dist_dir = Path.cwd() / "dist"
        if as_json:
            json_output({
                "ok": True,
                "target": target_os,
                "spec_file": str(spec_path),
                "dist_dir": str(dist_dir),
            }, True)
        else:
            console.print(f"[green]✓[/green] Build complete for [bold]{target_os}[/bold]")
            console.print(f"  Output: {dist_dir}")
    else:
        stderr_text = "".join(output_lines)[-500:] if output_lines else "Build failed"
        if as_json:
            json_output({"ok": False, "error": stderr_text}, True)
        else:
            console.print("[red]Build failed:[/red]")
            console.print(stderr_text)
        sys.exit(EXIT_GENERAL)


def confirm(message: str) -> bool:
    """Prompt the user for a yes/no confirmation.

    Args:
        message: Question to display.

    Returns:
        True if user confirmed, False otherwise.
    """

    response = console.input(f"[cyan]{message} (y/n): [/cyan]")
    return response.lower() in ("y", "yes")


# Knowledge base commands (optional; safe to import — heavy deps load lazily).
from xpst.knowledge.cli_kb import kb as _kb_group  # noqa: E402

main.add_command(_kb_group)


if __name__ == "__main__":
    main()
