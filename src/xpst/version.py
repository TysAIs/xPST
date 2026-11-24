"""The version string the CLI and the dashboard report.

``importlib.metadata.version`` raises ``PackageNotFoundError`` when the
distribution is not installed. That is a normal way to run this project:

* a source checkout driven through ``PYTHONPATH=src`` (what the parity
  harness and every contributor does), and
* an install whose user site moved with ``HOME`` — ``pip install --user``
  puts the metadata under ``~/.local``, so a process that re-points ``HOME``
  at a scratch profile can no longer see it.

Both surfaces build their version at *import* time (``click.version_option``
and ``FastAPI(version=...)``), so an unguarded call turns "not installed"
into "cannot start at all" — the whole CLI dies with a traceback before it
parses a single argument. Fall back to the version the package declares.
"""

from __future__ import annotations

import importlib.metadata

from xpst import __version__ as _source_version


def package_version() -> str:
    """The installed distribution's version, else the source tree's own."""
    try:
        return importlib.metadata.version("xpst")
    except importlib.metadata.PackageNotFoundError:
        return _source_version
