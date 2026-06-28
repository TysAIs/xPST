"""Security tests for the xPST plugin loader.

A dropped plugin file can declare a ``requires`` list. Auto-installing those via
``pip install`` is a remote-code-execution vector, so it must be OFF by default
and only happen with explicit opt-in.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from xpst.plugins import PluginManager

PLUGIN_WITH_REQUIRES = '''
def register():
    return {
        "name": "evil",
        "version": "1.0.0",
        "description": "declares a dependency",
        "requires": ["definitely-not-a-real-package==9.9.9"],
    }
'''


def _write_plugin(plugin_dir, body: str) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "evil.py").write_text(body, encoding="utf-8")


def test_auto_install_is_off_by_default(tmp_path):
    """Default PluginManager must never run pip install for a plugin's requires."""
    plugin_dir = tmp_path / "plugins"
    _write_plugin(plugin_dir, PLUGIN_WITH_REQUIRES)

    pm = PluginManager(plugin_dir=plugin_dir)
    with patch("xpst.plugins.subprocess.run") as run:
        loaded = pm.discover()

    assert "evil" in loaded
    run.assert_not_called()


def test_legacy_no_deps_false_does_not_enable_install(tmp_path):
    """The deprecated no_deps flag can no longer turn auto-install back on."""
    plugin_dir = tmp_path / "plugins"
    _write_plugin(plugin_dir, PLUGIN_WITH_REQUIRES)

    pm = PluginManager(plugin_dir=plugin_dir, no_deps=False)
    with patch("xpst.plugins.subprocess.run") as run:
        pm.discover()

    run.assert_not_called()


def test_install_deps_opt_in_runs_pip(tmp_path):
    """Explicit install_deps=True opts into dependency installation."""
    plugin_dir = tmp_path / "plugins"
    _write_plugin(plugin_dir, PLUGIN_WITH_REQUIRES)

    pm = PluginManager(plugin_dir=plugin_dir, install_deps=True)
    completed = MagicMock(returncode=0, stderr="")
    # The declared requirement is genuinely not importable, so the install
    # branch is reached naturally without patching importlib.
    with patch("xpst.plugins.subprocess.run", return_value=completed) as run:
        pm.discover()

    assert run.called
    args = run.call_args[0][0]
    assert "pip" in args and "install" in args
