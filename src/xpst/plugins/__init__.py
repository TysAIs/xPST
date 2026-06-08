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

import importlib.util
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default plugin directory
DEFAULT_PLUGIN_DIR = Path("~/.xpst/plugins").expanduser()


class PluginManager:
    """Discovers and loads xPST platform plugins from disk."""

    def __init__(self, plugin_dir: str | Path | None = None) -> None:
        self.plugin_dir = Path(plugin_dir).expanduser() if plugin_dir else DEFAULT_PLUGIN_DIR
        self._plugins: dict[str, dict[str, Any]] = {}
        self._loaded: bool = False
        self._watch_thread: threading.Thread | None = None
        self._watch_stop: threading.Event = threading.Event()
        self._watch_interval: float = 2.0
        self._file_mtimes: dict[str, float] = {}

    @property
    def plugins(self) -> dict[str, dict[str, Any]]:
        """Return dict of loaded plugins keyed by platform name."""
        if not self._loaded:
            self.discover()
        return self._plugins

    def discover(self) -> list[str]:
        """Scan plugin_dir for .py files and load valid plugins.

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

        if loaded:
            logger.info("Loaded %d plugins: %s", len(loaded), ", ".join(loaded))
        return loaded

    def _load_plugin_file(self, path: Path) -> dict[str, Any] | None:
        """Load a single plugin file and call its register() function."""
        try:
            spec = importlib.util.spec_from_file_location(f"xpst_plugin_{path.stem}", path)
            if spec is None or spec.loader is None:
                logger.warning("Cannot create module spec for %s", path)
                return None

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

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
                changed = False
                for fpath, mtime in current_mtimes.items():
                    if fpath not in self._file_mtimes or self._file_mtimes[fpath] != mtime:
                        changed = True
                        logger.info("Detected change in plugin file: %s", fpath)
                # Detect removed files
                for fpath in self._file_mtimes:
                    if fpath not in current_mtimes:
                        changed = True
                        logger.info("Detected removed plugin file: %s", fpath)
                if changed:
                    self.reload()
                    logger.info("Plugins reloaded after file change")
                self._file_mtimes = current_mtimes
            except Exception as exc:
                logger.warning("Plugin watcher error: %s", exc)
        logger.info("Plugin file watcher stopped")

    def start_watching(self, interval: float = 2.0) -> None:
        """Start watching the plugin directory for file changes.

        Args:
            interval: Polling interval in seconds (default: 2.0).
        """
        if self._watch_thread is not None and self._watch_thread.is_alive():
            logger.debug("Plugin watcher already running")
            return
        self._watch_stop.clear()
        self._watch_interval = interval
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
