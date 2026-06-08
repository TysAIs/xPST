"""
Auto-update system for xPST dependencies.

Provides commands to update yt-dlp, instagrapi, twikit, and the
xPST package itself.
"""

import importlib
import subprocess
import sys
from dataclasses import dataclass

from rich.console import Console
from rich.table import Table

from xpst.utils.logger import get_logger

console = Console()
logger = get_logger(__name__)

# Packages to track/update
TRACKED_PACKAGES = {
    "yt-dlp": "yt-dlp",
    "instagrapi": "instagrapi",
    "twikit": "twikit",
}

# Package import names (for version checking)
PACKAGE_IMPORTS = {
    "yt-dlp": "yt_dlp",
    "instagrapi": "instagrapi",
    "twikit": "twikit",
}


@dataclass
class PackageInfo:
    """Information about an installed package."""
    name: str
    current_version: str | None = None
    latest_version: str | None = None
    installed: bool = False
    updatable: bool = False
    error: str | None = None


def get_installed_version(package_name: str) -> str | None:
    """Get the installed version of a Python package.

    Tries ``importlib.metadata.version()`` first, then falls back to
    checking ``__version__`` / ``VERSION`` attributes on the module.

    Args:
        package_name: PyPI package name (e.g., ``yt-dlp``).

    Returns:
        Version string or None if not installed.
    """

    # Try importlib.metadata first (most reliable)
    try:
        from importlib.metadata import version as get_version
        ver = get_version(package_name)
        if ver:
            return ver
    except Exception as e:
        logger.debug("Could not get version for %s: %s", package_name, e)

    # Fallback: try module attributes
    import_name = PACKAGE_IMPORTS.get(package_name, package_name)
    try:
        mod = importlib.import_module(import_name)
        for attr in ("__version__", "VERSION"):
            ver = getattr(mod, attr, None)
            if ver and isinstance(ver, str):
                return ver
        # If 'version' exists and is a string (not a module)
        ver = getattr(mod, "version", None)
        if ver and isinstance(ver, str):
            return ver
        # If 'version' is a submodule, check its __version__
        if ver and hasattr(ver, "__version__"):
            return str(ver.__version__)
    except ImportError:
        pass
    return None


def get_latest_version(package_name: str) -> str | None:
    """Check PyPI for the latest version of a package.

    Tries ``pip index versions`` first, then falls back to the PyPI JSON API.

    Args:
        package_name: PyPI package name.

    Returns:
        Latest version string or None if unable to determine.
    """

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "index", "versions", package_name],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            # Parse: "package_name (X.Y.Z)"
            output = result.stdout.strip()
            if "(" in output:
                return output.split("(")[1].split(")")[0]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Fallback: use pip install --dry-run
    try:
        import json
        import urllib.request
        url = f"https://pypi.org/pypi/{package_name}/json"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get("info", {}).get("version")
    except Exception as e:
        logger.debug("Failed to check PyPI for %s: %s", package_name, e)

    return None


def get_xpst_version() -> str:
    """Get the current xPST package version.

    Returns:
        Version string from ``xpst.__version__``.
    """

    from xpst import __version__
    return __version__


def check_updates() -> list[PackageInfo]:
    """Check for available updates without installing."""
    packages = []

    # xPST itself
    current = get_xpst_version()
    info = PackageInfo(name="xpst", current_version=current, installed=True)
    latest = get_latest_version("xpst")
    if latest:
        info.latest_version = latest
        info.updatable = _version_is_newer(current, latest)
    packages.append(info)

    # Tracked dependencies
    for name in TRACKED_PACKAGES:
        info = PackageInfo(name=name)
        info.current_version = get_installed_version(name)
        info.installed = info.current_version is not None
        info.latest_version = get_latest_version(name)

        if info.installed and info.latest_version:
            info.updatable = _version_is_newer(info.current_version, info.latest_version)

        packages.append(info)

    return packages


def update_all(check_only: bool = False) -> list[PackageInfo]:
    """
    Check and optionally install updates for all tracked packages.

    Args:
        check_only: If True, only check without installing

    Returns:
        List of PackageInfo with update status
    """
    packages = check_updates()

    if check_only:
        return packages

    # Install updates
    to_update = [p for p in packages if p.updatable and p.name != "xpst"]

    if not to_update:
        return packages

    for pkg in to_update:
        pip_name = TRACKED_PACKAGES.get(pkg.name, pkg.name)
        console.print(f"  Updating {pip_name}...")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", pip_name],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                pkg.current_version = get_installed_version(pip_name) or pkg.latest_version
                pkg.updatable = False
                console.print(f"    ✅ Updated to {pkg.current_version}")
            else:
                pkg.error = result.stderr.strip()[:200]
                console.print(f"    ❌ Update failed: {pkg.error}")
        except subprocess.TimeoutExpired:
            pkg.error = "Update timed out"
            console.print("    ❌ Update timed out")
        except Exception as e:
            pkg.error = str(e)[:200]
            console.print(f"    ❌ Error: {pkg.error}")

    return packages


def display_update_status(packages: list[PackageInfo]) -> None:
    """Display update status as a rich table."""
    table = Table(title="Package Status")
    table.add_column("Package", style="cyan")
    table.add_column("Installed", style="green")
    table.add_column("Latest", style="yellow")
    table.add_column("Status")

    for pkg in packages:
        installed = pkg.current_version or "[dim]not installed[/dim]"
        latest = pkg.latest_version or "[dim]unknown[/dim]"

        if pkg.updatable:
            status = "[yellow]⬆ Update available[/yellow]"
        elif pkg.installed:
            status = "[green]✓ Up to date[/green]"
        elif pkg.error:
            status = f"[red]✗ {pkg.error}[/red]"
        else:
            status = "[dim]— Not installed[/dim]"

        table.add_row(pkg.name, installed, latest, status)

    console.print(table)


def display_version_info() -> None:
    """Display comprehensive version information."""
    from xpst import __version__

    console.print(f"\n[bold blue]xPST v{__version__}[/bold blue]\n")

    table = Table(title="Dependency Versions")
    table.add_column("Package", style="cyan")
    table.add_column("Version", style="green")
    table.add_column("Status")

    for name, _import_name in PACKAGE_IMPORTS.items():
        ver = get_installed_version(name)
        if ver:
            table.add_row(name, ver, "[green]✓[/green]")
        else:
            table.add_row(name, "[dim]not installed[/dim]", "[red]✗[/red]")

    # Python
    table.add_row("Python", f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}", "[green]✓[/green]")

    # FFmpeg
    import shutil

    from xpst.utils.platform import get_ffmpeg_name
    if shutil.which(get_ffmpeg_name()):
        try:
            result = subprocess.run(
                [get_ffmpeg_name(), "-version"],
                capture_output=True, text=True, timeout=5,
            )
            first_line = result.stdout.split("\n")[0] if result.stdout else "installed"
            # Extract version number
            ver_str = "installed"
            if "version" in first_line.lower():
                parts = first_line.split("version")
                if len(parts) > 1:
                    ver_str = parts[1].strip().split(" ")[0]
            table.add_row("FFmpeg", ver_str, "[green]✓[/green]")
        except Exception as e:
            logger.debug("FFmpeg version display failed: %s", e)
            table.add_row("FFmpeg", "installed", "[green]✓[/green]")
    else:
        table.add_row("FFmpeg", "[dim]not found[/dim]", "[red]✗[/red]")

    console.print(table)


def _version_is_newer(current: str | None, latest: str | None) -> bool:
    """Check if latest version is newer than current.

    Uses ``packaging.version.Version`` for proper semver comparison.
    Falls back to simple string inequality if packaging is unavailable.

    Args:
        current: Currently installed version string.
        latest: Latest available version string.

    Returns:
        True if latest is strictly newer than current.
    """

    if not current or not latest:
        return False

    try:
        from packaging.version import Version
        return Version(latest) > Version(current)
    except (ImportError, Exception):
        # Fallback: simple string comparison
        return current != latest
