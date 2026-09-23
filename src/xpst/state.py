"""State persistence for xPST — legacy import path (re-export only).

This module used to define a second ``StateManager`` class that wrapped the
real one in :mod:`xpst.state_manager`. That wrapper was a third owner of the
same state and duplicated the whole legacy method surface, so readers could
not tell which class was authoritative.

There is now exactly one state class and exactly one persistence owner:

* :class:`xpst.state_manager.StateManager` — business logic + the legacy API.
* :class:`xpst.state_store.StateStore` — the only module that reads/writes
  ``state.json`` (locking, atomic writes, backup rotation, migration).

This module stays as the stable public import path
(``from xpst.state import StateManager``) used by the engine, CLI, monitor,
dashboard and MCP surfaces. It defines no classes and owns no state.
"""

import json  # noqa: F401 - kept in this namespace for legacy monkeypatch callers

from xpst.state_manager import StateManager
from xpst.state_manager import StateManager as NewStateManager
from xpst.state_store import StateStore

__all__ = ["StateManager", "StateStore", "NewStateManager"]
