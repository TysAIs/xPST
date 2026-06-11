"""
Auto-update system for xPST dependencies.

Provides commands to update yt-dlp, instagrapi, twikit, and the
xPST package itself.
"""

import importlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal

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


@dataclass
class UpdateComponent:
    """Update status for an app, package, helper, or provider metadata source."""

    name: str
    component_type: Literal["app", "package", "helper", "provider_metadata"]
    current_version: str | None = None
    latest_version: str | None = None
    installed: bool = False
    update_mode: Literal["pip", "external", "bundled", "manual"] = "manual"
    required: bool = False
    updatable: bool = False
    status: Literal["current", "update_available", "missing", "manual", "unknown"] = "unknown"
    action: str = "No action available."
    update_command: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable status object."""
        return {
            "name": self.name,
            "component_type": self.component_type,
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "installed": self.installed,
            "update_mode": self.update_mode,
            "required": self.required,
            "updatable": self.updatable,
            "status": self.status,
            "action": self.action,
            "update_command": self.update_command,
            "error": self.error,
        }


def _annotate_component(component: UpdateComponent) -> UpdateComponent:
    """Attach a user-facing status and next action to an update component."""
    if not component.installed:
        component.status = "missing"
        if component.update_mode == "external":
            component.action = f"Install {component.name} with your operating system package manager."
        elif component.update_mode == "pip":
            component.action = f"Install {component.name} with xPST's updater."
            component.update_command = "xpst update"
        else:
            component.action = f"Install or configure {component.name}."
        return component

    if component.updatable:
        component.status = "update_available"
        if component.update_mode == "pip":
            component.action = f"Update {component.name} with xPST's updater."
            component.update_command = "xpst update"
        elif component.update_mode == "external":
            component.action = f"Update {component.name} with your operating system package manager."
        else:
            component.action = f"Update {component.name} manually."
        return component

    if component.update_mode == "external" and component.latest_version is None:
        component.status = "manual"
        component.action = f"{component.name} is installed; update it with your operating system package manager when needed."
        return component

    if component.update_mode == "bundled":
        component.status = "current"
        component.action = "Provider metadata is bundled; update xPST to refresh it."
        component.update_command = "xpst update --check"
        return component

    if component.latest_version is None and component.update_mode == "pip":
        component.status = "unknown"
        component.action = "Run xpst update --components --check to check latest versions."
        component.update_command = "xpst update --components --check"
        return component

    component.status = "current"
    component.action = "No action needed."
    return component


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


def check_helper_tools() -> list[UpdateComponent]:
    """Check local helper tools without installing or reaching the network."""
    from xpst.utils.platform import get_ffmpeg_name

    helpers: list[UpdateComponent] = []

    ytdlp_version = get_installed_version("yt-dlp")
    helpers.append(
        _annotate_component(
            UpdateComponent(
                name="yt-dlp",
                component_type="helper",
                current_version=ytdlp_version,
                installed=ytdlp_version is not None,
                update_mode="pip",
                required=True,
            )
        )
    )

    ffmpeg_name = get_ffmpeg_name()
    ffmpeg_path = shutil.which(ffmpeg_name)
    ffmpeg = UpdateComponent(
        name="FFmpeg",
        component_type="helper",
        installed=ffmpeg_path is not None,
        update_mode="external",
        required=True,
    )
    if ffmpeg_path:
        try:
            result = subprocess.run(
                [ffmpeg_name, "-version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            first_line = result.stdout.splitlines()[0] if result.stdout else ""
            if "version" in first_line.lower():
                ffmpeg.current_version = first_line.split("version", 1)[1].strip().split(" ")[0]
            else:
                ffmpeg.current_version = "installed"
        except Exception as e:
            ffmpeg.current_version = "installed"
            ffmpeg.error = str(e)[:200]
    helpers.append(_annotate_component(ffmpeg))

    return helpers


def check_provider_metadata() -> list[UpdateComponent]:
    """Report provider metadata status.

    Provider metadata is currently bundled with the app. Keeping it explicit
    lets the updater and UI grow toward remote, signed provider-rule updates
    without changing the status contract later.
    """
    return [
        _annotate_component(
            UpdateComponent(
                name="provider-manifests",
                component_type="provider_metadata",
                current_version=get_xpst_version(),
                installed=True,
                update_mode="bundled",
                required=True,
            )
        )
    ]


def check_update_components(include_network: bool = False) -> dict[str, list[dict[str, object]]]:
    """Return a structured update view for app, packages, helpers, and providers.

    Args:
        include_network: When True, include PyPI latest-version checks. When
            False, only local/offline-safe checks are performed.
    """
    current_app_version = get_xpst_version()
    latest_app_version = get_latest_version("xpst") if include_network else None
    app = _annotate_component(
        UpdateComponent(
            name="xpst",
            component_type="app",
            current_version=current_app_version,
            latest_version=latest_app_version,
            installed=True,
            update_mode="pip",
            required=True,
            updatable=_version_is_newer(current_app_version, latest_app_version),
        )
    )

    packages: list[UpdateComponent] = []
    for name in TRACKED_PACKAGES:
        current = get_installed_version(name)
        latest = get_latest_version(name) if include_network else None
        packages.append(
            _annotate_component(
                UpdateComponent(
                    name=name,
                    component_type="package",
                    current_version=current,
                    latest_version=latest,
                    installed=current is not None,
                    update_mode="pip",
                    required=True,
                    updatable=_version_is_newer(current, latest),
                )
            )
        )

    return {
        "app": [app.to_dict()],
        "packages": [pkg.to_dict() for pkg in packages],
        "helpers": [helper.to_dict() for helper in check_helper_tools()],
        "provider_metadata": [metadata.to_dict() for metadata in check_provider_metadata()],
    }


# Post-update smoke probes: the module each tracked dependency must still
# import after an upgrade. A failed probe triggers an automatic rollback to
# the previously installed version (G45).
_SMOKE_MODULES = {
    "yt-dlp": "yt_dlp",
    "instagrapi": "instagrapi",
    "twikit": "twikit",
}


def _smoke_check(pip_name: str) -> bool:
    """Import-probe a freshly upgraded package in a clean interpreter."""
    module = _SMOKE_MODULES.get(pip_name)
    if module is None:
        return True
    try:
        probe = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True, text=True, timeout=60,
        )
        return probe.returncode == 0
    except Exception:
        return False


def update_all(check_only: bool = False) -> list[PackageInfo]:
    """
    Check and optionally install updates for all tracked packages.

    Installs are guarded (G44/G45): frozen builds never attempt pip (inside
    a PyInstaller bundle ``sys.executable -m pip`` re-invokes the bundled
    app, not pip); each upgrade is smoke-checked and rolled back to the
    previous version on failure.

    Args:
        check_only: If True, only check without installing

    Returns:
        List of PackageInfo with update status
    """
    packages = check_updates()

    if check_only:
        return packages

    # G44: in a frozen (PyInstaller) build there is no pip and
    # sys.executable IS the bundled app — a pip invocation would recurse
    # into xPST itself. Self-update for packaged builds means downloading
    # a new release.
    # sys.frozen is set by PyInstaller in packaged builds.
    if getattr(sys, "frozen", False):
        console.print(
            "[yellow]Packaged build: dependency self-update is unavailable. "
            "Download the latest release to update.[/yellow]"
        )
        for pkg in packages:
            if pkg.updatable:
                pkg.error = "self-update unavailable in packaged build"
        return packages

    # Install updates
    to_update = [p for p in packages if p.updatable and p.name != "xpst"]

    if not to_update:
        return packages

    for pkg in to_update:
        pip_name = TRACKED_PACKAGES.get(pkg.name, pkg.name)
        previous_version = pkg.current_version
        console.print(f"  Updating {pip_name}...")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", pip_name],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                pkg.error = result.stderr.strip()[:200]
                console.print(f"    ❌ Update failed: {pkg.error}")
                continue

            # G45: smoke-check, roll back on failure — an update must never
            # leave the app broken.
            if not _smoke_check(pip_name):
                console.print(
                    f"    ⚠️ {pip_name} failed its post-update smoke check"
                )
                if previous_version:
                    rollback = subprocess.run(
                        [sys.executable, "-m", "pip", "install",
                         f"{pip_name}=={previous_version}"],
                        capture_output=True, text=True, timeout=120,
                    )
                    if rollback.returncode == 0:
                        pkg.error = (
                            f"update broke import; rolled back to {previous_version}"
                        )
                        console.print(f"    ↩️ Rolled back to {previous_version}")
                    else:
                        pkg.error = "update broke import; ROLLBACK FAILED"
                        console.print("    ❌ Rollback failed — reinstall manually")
                else:
                    pkg.error = "update broke import; no previous version known"
                continue

            pkg.current_version = get_installed_version(pip_name) or pkg.latest_version
            pkg.updatable = False
            console.print(f"    ✅ Updated to {pkg.current_version}")
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
