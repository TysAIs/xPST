"""xPST Plugin System — dynamic platform plugin discovery and loading.

Discovers platform plugins from the xPST plugin directory. Each plugin must be a
Python file that defines a ``register()`` function returning a dict::

    {
        "name": "my_platform",          # platform identifier
        "uploader": MyUploaderClass,    # subclass of BaseUploader
        "source": MySourceClass,        # subclass of BaseSource (optional)
        "version": "1.0.0",             # plugin version
        "description": "...",           # human-readable description
    }

SECURITY
--------
Loading a plugin **executes arbitrary Python code** from the plugin file. xPST
does NOT sandbox plugins: the ``RestrictedPlugin`` helper only tweaks a module's
``__builtins__`` *after* the module body has already run, so it cannot prevent a
malicious plugin from doing anything at import time. Treat plugin files in
the xPST plugin directory as fully trusted code that you have reviewed yourself.

To reduce the blast radius:

* Automatic dependency installation is **disabled by default**. xPST will never
  run ``pip install`` based on a plugin's ``requires`` list unless you explicitly
  opt in with ``PluginManager(install_deps=True)``.
* Only place plugins you trust in the plugin directory.
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

from xpst.utils.platform import get_config_dir

logger = logging.getLogger(__name__)

# Builtins that are restricted in sandboxed plugins
_RESTRICTED_MODULES = frozenset({"os", "sys", "subprocess", "shutil", "signal", "ctypes"})

# Default plugin directory
DEFAULT_PLUGIN_DIR = get_config_dir() / "plugins"


class RestrictedPlugin:
    """Best-effort wrapper around an already-loaded plugin module.

    .. warning::
        This is **not** a security sandbox. By the time a module is wrapped it
        has already been fully executed by ``exec_module`` (including any code at
        module top level), so patching ``__builtins__`` here cannot stop a
        malicious plugin from importing ``os``/``subprocess`` or touching the
        filesystem during import. It only discourages *post-load* dynamic imports
        of a small set of modules. Do not rely on it to run untrusted code.
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
        """Discourage *post-load* dynamic imports of a few modules.

        Not a security boundary — see the class docstring. The module body has
        already executed before this runs.
        """
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

    def __init__(
        self,
        plugin_dir: str | Path | None = None,
        sandbox: bool = False,
        install_deps: bool = False,
        no_deps: bool | None = None,
    ) -> None:
        """Create a plugin manager.

        Args:
            plugin_dir: Directory to scan for plugin ``.py`` files.
            sandbox: Apply the best-effort ``RestrictedPlugin`` post-load tweak.
                This is NOT a security boundary (see ``RestrictedPlugin``).
            install_deps: Explicit opt-in to automatically ``pip install`` a
                plugin's declared ``requires`` dependencies. **Off by default** —
                xPST never installs packages on a plugin's behalf unless asked.
                Installing dependencies a plugin requested can pull arbitrary
                code from PyPI, so only enable this for plugins you trust.
            no_deps: Deprecated. Retained for backward compatibility. When given,
                ``no_deps=False`` does NOT enable auto-install (the default is
                still off); only ``install_deps=True`` enables it.
        """
        self.plugin_dir = Path(plugin_dir).expanduser() if plugin_dir else DEFAULT_PLUGIN_DIR
        self._plugins: dict[str, dict[str, Any]] = {}
        self._loaded: bool = False
        self._watch_thread: threading.Thread | None = None
        self._watch_stop: threading.Event = threading.Event()
        self._watch_interval: float = 2.0
        self._file_mtimes: dict[str, float] = {}
        self._on_reload_callback: Any = None
        self._sandbox: bool = sandbox
        # Auto-install is opt-in only. The legacy ``no_deps`` flag can no longer
        # turn it on; it is accepted solely so old call sites do not break.
        self._install_deps: bool = bool(install_deps)

    @property
    def plugins(self) -> dict[str, dict[str, Any]]:
        """Return dict of loaded plugins keyed by platform name."""
        if not self._loaded:
            self.discover()
        return self._plugins

    def discover(self) -> list[str]:
        """Scan plugin_dir for .py files and load valid plugins.

        Loading a plugin executes its module body (arbitrary code); this is not
        sandboxed. If ``sandbox=True``, a best-effort post-load tweak is applied
        (not a security boundary). Automatic dependency installation only runs
        when the manager was created with ``install_deps=True``.

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
                # Auto-install plugin dependencies ONLY when explicitly opted in.
                if self._install_deps:
                    self._install_plugin_deps(plugin)
                elif plugin.get("requires"):
                    logger.info(
                        "Plugin '%s' declares dependencies %s; auto-install is "
                        "disabled. Install them manually or re-run with "
                        "install_deps=True if you trust this plugin.",
                        name, plugin.get("requires"),
                    )

        if loaded:
            logger.info("Loaded %d plugins: %s", len(loaded), ", ".join(loaded))
        return loaded

    def _install_plugin_deps(self, plugin: dict[str, Any]) -> None:
        """Install dependencies declared in a plugin's 'requires' key.

        Only invoked when the manager was created with ``install_deps=True``.
        Running ``pip install`` on values supplied by a dropped plugin file is a
        remote-code-execution vector, so this is gated behind explicit opt-in and
        guarded here as defence in depth.
        """
        if not self._install_deps:
            return
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
