"""Hatch build hook: embed the built web UI in the xPST wheel.

``ui/dist`` is a Vite build and is gitignored, so a wheel built from a plain
checkout used to ship NO user interface at all: the installed engine served
``_DASHBOARD_INDEX_HTML`` (the string placeholder) at ``/`` and only the JSON
API worked. ``#/phone`` is a route *in that bundle*, so a pip user could pair a
phone and still never reach the phone screen.

This hook maps ``ui/dist`` (or ``XPST_UI_DIST`` when set) into the wheel as
``xpst/dashboard/webui`` without touching the source tree, so
``xpst.dashboard.server._ui_dist_dir()`` finds it after a plain
``pip install``. Build the UI first::

    cd ui && npm install && npm run build

Set ``XPST_REQUIRE_UI=1`` to turn a missing build into a build failure; the
release and PyPI lanes do that, while a local ``uv build`` in a bare checkout
still succeeds (engine-only wheel, which stays honest: the served placeholder
page says the bundle is missing).
"""

from __future__ import annotations

import os
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

#: Destination of the Vite build inside the wheel, next to ``server.py``.
WHEEL_UI_TARGET = "xpst/dashboard/webui"

#: ``1`` makes a missing ``ui/dist`` fail the wheel build instead of warning.
REQUIRE_UI_ENV = "XPST_REQUIRE_UI"

#: Same override the server honours, so a custom build directory can be used.
UI_DIST_ENV = "XPST_UI_DIST"

MISSING_UI_MESSAGE = (
    "the xPST wheel is being built without the web UI: neither ui/dist nor a "
    f"{UI_DIST_ENV} build directory was found. Run `cd ui && npm install && npm run build` "
    f"first (or point {UI_DIST_ENV} at a build). A wheel without the bundle serves the "
    "placeholder page at / and only the JSON API works, so #/phone is unreachable."
)


def locate_ui_dist(root: Path, environ: dict[str, str] | None = None) -> Path | None:
    """Return the directory holding the built UI (``index.html`` present), else ``None``."""
    env = os.environ if environ is None else environ
    override = env.get(UI_DIST_ENV)
    if override:
        candidate = Path(override).expanduser()
        return candidate if (candidate / "index.html").is_file() else None
    candidate = Path(root) / "ui" / "dist"
    return candidate if (candidate / "index.html").is_file() else None


class CustomBuildHook(BuildHookInterface):
    """Copy a built web UI into the wheel; warn (or fail) when it is missing."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        if self.target_name != "wheel":
            # The sdist stays source-only: it carries no build product, and a
            # pip install from the sdist rebuilds the wheel (still without a
            # bundle unless ui/dist is present). PyPI wheels are py3-none-any,
            # so the wheel is what a pip user actually installs.
            return

        ui_dist = locate_ui_dist(Path(self.root))
        if ui_dist is None:
            if os.environ.get(REQUIRE_UI_ENV) == "1":
                raise RuntimeError(MISSING_UI_MESSAGE)
            self.app.display_warning(f"{MISSING_UI_MESSAGE} Set {REQUIRE_UI_ENV}=1 to fail the build instead.")
            return

        force_include = build_data.setdefault("force_include", {})
        force_include[str(ui_dist)] = WHEEL_UI_TARGET
