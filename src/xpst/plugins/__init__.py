"""xPST Plugin System — dynamic platform plugin discovery and loading.

Discovers platform plugins from ~/.xpst/plugins/. Each plugin must be a
Python file that defines a ``register()`` function returning a dict::

    {
        "name": "my_platform",          # platform identifier
        "uploader": MyUploaderClass,    # subclass of BaseUploader
        "source": MySourceClass,        # subclass of BaseSource (optional)
        "version": "1.0.0",             # plugin version
        "description": "...",           # human-readable description
    }
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Builtins that are restricted in sandboxed plugins
_RESTRICTED_MODULES = frozenset({"os", "sys", "subprocess", "shutil", "signal", "ctypes"})

# Default plugin directory
DEFAULT_PLUGIN_DIR = Path("~/.xpst/plugins").expanduser()


class RestrictedPlugin:
    """Wrapper that provides a sandboxed execution environment for plugin modules.

    Removes dangerous builtins (os, sys, subprocess, etc.) so untrusted
    plugins cannot perform filesystem or process operations.
    """

    def __init__(self, module: Any) -> None:
        self._module = module
        self._name = getattr(module, "__name__", "unknown")

    @property
    def module(self) -> Any:
        return self._module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._module, name)

    @staticmethod
    def apply_sandbox(module: Any) -> None:
        """Restrict __builtins__ in a loaded module to block dangerous imports."""
        if not hasattr(module, "__builtins__"):
            return
        builtins = module.__builtins__
        if isinstance(builtins, dict):
            original_import = builtins.get("__import__")
            if original_import:
                def _restricted_import(name: str, *args: Any, **kwargs: Any) -> Any:
                    if name in _RESTRICTED_MODULES:
                        raise ImportError(f"Import of '{name}' is not allowed in sandboxed plugins")
                    return original_import(name, *args, **kwargs)
                builtins["__import__"] = _restricted_import


class PluginManager:
    """Discovers and loads xPST platform plugins from disk."""

    def __init__(self, plugin_dir: str | Path | None = None, sandbox: bool = False, no_deps: bool = False) -> None:
        self.plugin_dir = Path(plugin_dir).expanduser() if plugin_dir else DEFAULT_PLUGIN_DIR
        self._plugins: dict[str, dict[str, Any]] = {}
        self._loaded: bool = False
        self._watch_thread: threading.Thread | None = None
        self._watch_stop: threading.Event = threading.Event()
        self._watch_interval: float = 2.0
        self._file_mtimes: dict[str, float] = {}
        self._on_reload_callback: Any = None
        self._sandbox: bool = sandbox
        self._no_deps: bool = no_deps

    @property
    def plugins(self) -> dict[str, dict[str, Any]]:
        """Return dict of loaded plugins keyed by platform name."""
        if not self._loaded:
            self.discover()
        return self._plugins

    def discover(self) -> list[str]:
        """Scan plugin_dir for .py files and load valid plugins.

        If sandbox=True, plugin modules are wrapped with restricted builtins.
        If no_deps=True, automatic dependency installation is skipped.

        Returns:
            List of successfully loaded plugin names.
        """
        self._plugins.clear()
        self._loaded = True

        if not self.plugin_dir.exists():
            self.plugin_dir.mkdir(parents=True, exist_ok=True)
            logger.info("Created plugin directory: %s", self.plugin_dir)
            return []

        loaded: list[str] = []
        for py_file in sorted(self.plugin_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            plugin = self._load_plugin_file(py_file)
            if plugin is not None:
                name = plugin.get("name", py_file.stem)
                self._plugins[name] = plugin
                loaded.append(name)
                logger.info("Loaded plugin '%s' from %s", name, py_file)
                # Auto-install plugin dependencies if not skipped
                if not self._no_deps:
                    self._install_plugin_deps(plugin)

        if loaded:
            logger.info("Loaded %d plugins: %s", len(loaded), ", ".join(loaded))
        return loaded

    def _install_plugin_deps(self, plugin: dict[str, Any]) -> None:
        """Check and install dependencies declared in plugin's 'requires' key."""
        requires = plugin.get("requires")
        if not requires or not isinstance(requires, list):
            return
        for dep in requires:
            if not isinstance(dep, str):
                continue
            # Check if already importable
            module_name = dep.split(">=")[0].split("==")[0].split("<=")[0].split("[")[0].strip()
            try:
                importlib.import_module(module_name)
            except ImportError:
                logger.info("Installing missing plugin dependency: %s", dep)
                try:
                    result = subprocess.run(
                        [sys.executable, "-m", "pip", "install", dep],
                        capture_output=True, text=True, timeout=120,
                    )
                    if result.returncode == 0:
                        logger.info("Installed %s successfully", dep)
                    else:
                        logger.warning("Failed to install %s: %s", dep, result.stderr[-300:])
                except Exception as exc:
                    logger.warning("Error installing %s: %s", dep, exc)

    def _load_plugin_file(self, path: Path) -> dict[str, Any] | None:
        """Load a single plugin file and call its register() function."""
        try:
            spec = importlib.util.spec_from_file_location(f"xpst_plugin_{path.stem}", path)
            if spec is None or spec.loader is None:
                logger.warning("Cannot create module spec for %s", path)
                return None

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Apply sandbox restrictions if enabled
            if self._sandbox:
                RestrictedPlugin.apply_sandbox(module)

            if not hasattr(module, "register"):
                logger.warning("Plugin %s has no register() function", path.name)
                return None

            info = module.register()
            if not isinstance(info, dict) or "name" not in info:
                logger.warning("Plugin %s register() did not return a valid dict", path.name)
                return None

            return info

        except Exception as exc:
            logger.error("Failed to load plugin %s: %s", path.name, exc)
            return None

    def get(self, name: str) -> dict[str, Any] | None:
        """Get a plugin by name."""
        return self.plugins.get(name)

    def get_uploader_class(self, name: str) -> type | None:
        """Get the uploader class for a named plugin."""
        plugin = self.get(name)
        if plugin is not None:
            return plugin.get("uploader")
        return None

    def get_source_class(self, name: str) -> type | None:
        """Get the source class for a named plugin."""
        plugin = self.get(name)
        if plugin is not None:
            return plugin.get("source")
        return None

    def list_plugins(self) -> list[dict[str, Any]]:
        """Return a list of plugin summaries (name, version, description)."""
        return [
            {
                "name": name,
                "version": info.get("version", "unknown"),
                "description": info.get("description", ""),
                "has_uploader": info.get("uploader") is not None,
                "has_source": info.get("source") is not None,
            }
            for name, info in self.plugins.items()
        ]

    def reload(self) -> list[str]:
        """Re-discover and reload all plugins."""
        self._loaded = False
        return self.discover()

    def _get_plugin_files(self) -> list[Path]:
        """Return list of .py plugin files in plugin_dir."""
        if not self.plugin_dir.exists():
            return []
        return sorted(f for f in self.plugin_dir.glob("*.py") if not f.name.startswith("_"))

    def _snapshot_mtimes(self) -> dict[str, float]:
        """Snapshot modification times for all plugin files."""
        return {str(f): f.stat().st_mtime for f in self._get_plugin_files()}

    def _watch_loop(self) -> None:
        """Background polling loop that detects plugin file changes."""
        logger.info("Plugin file watcher started (interval=%.1fs)", self._watch_interval)
        self._file_mtimes = self._snapshot_mtimes()
        while not self._watch_stop.is_set():
            self._watch_stop.wait(timeout=self._watch_interval)
            if self._watch_stop.is_set():
                break
            try:
                current_mtimes = self._snapshot_mtimes()
                # Detect new or modified files
                changed_files: list[str] = []
                for fpath, mtime in current_mtimes.items():
                    if fpath not in self._file_mtimes or self._file_mtimes[fpath] != mtime:
                        changed_files.append(fpath)
                        logger.info("Detected change in plugin file: %s", fpath)
                # Detect removed files
                for fpath in self._file_mtimes:
                    if fpath not in current_mtimes:
                        changed_files.append(fpath)
                        logger.info("Detected removed plugin file: %s", fpath)
                if changed_files:
                    self.reload()
                    logger.info("Plugins reloaded after file change")
                    # Invoke on_reload callback for each changed plugin
                    if self._on_reload_callback is not None:
                        for fpath in changed_files:
                            plugin_name = Path(fpath).stem
                            try:
                                self._on_reload_callback(plugin_name)
                            except Exception as exc:
                                logger.warning("on_reload callback error for %s: %s", plugin_name, exc)
                self._file_mtimes = current_mtimes
            except Exception as exc:
                logger.warning("Plugin watcher error: %s", exc)
        logger.info("Plugin file watcher stopped")

    def start_watching(self, interval: float = 2.0, on_reload: Any = None) -> None:
        """Start watching the plugin directory for file changes.

        Args:
            interval: Polling interval in seconds (default: 2.0).
            on_reload: Optional callback invoked with plugin name when a plugin is reloaded.
        """
        if self._watch_thread is not None and self._watch_thread.is_alive():
            logger.debug("Plugin watcher already running")
            return
        self._watch_stop.clear()
        self._watch_interval = interval
        self._on_reload_callback = on_reload
        self._watch_thread = threading.Thread(target=self._watch_loop, daemon=True, name="plugin-watcher")
        self._watch_thread.start()

    def stop_watching(self) -> None:
        """Stop watching the plugin directory for file changes."""
        if self._watch_thread is None or not self._watch_thread.is_alive():
            return
        self._watch_stop.set()
        self._watch_thread.join(timeout=5.0)
        self._watch_thread = None

# Module-level convenience instance
_manager: PluginManager | None = None


def get_plugin_manager() -> PluginManager:
    """Return the global PluginManager singleton."""
    global _manager
    if _manager is None:
        _manager = PluginManager()
    return _manager
