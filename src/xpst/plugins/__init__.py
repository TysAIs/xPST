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


# Module-level convenience instance
_manager: PluginManager | None = None


def get_plugin_manager() -> PluginManager:
    """Return the global PluginManager singleton."""
    global _manager
    if _manager is None:
        _manager = PluginManager()
    return _manager
