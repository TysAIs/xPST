"""
xPST Dashboard

API-only server exposing health, metrics, and state endpoints via FastAPI.
No NiceGUI or other graphical dependency — the ``dashboard`` extra is a no-op
(added for the NiceGUI fallback UI that was removed with ``src/xpst/desktop.py``).
For a GUI, use the native desktop app (``xpst app``).
"""

from xpst.dashboard.analytics import AnalyticsCollector

__all__ = ["AnalyticsCollector"]
