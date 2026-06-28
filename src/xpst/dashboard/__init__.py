"""
xPST Dashboard

API-only server exposing health, metrics, and state endpoints via FastAPI.
No NiceGUI dependency required — install ``xpst[dashboard]`` for the
full NiceGUI web UI, or use the native desktop app (``xpst app``).
"""

from xpst.dashboard.analytics import AnalyticsCollector

__all__ = ["AnalyticsCollector"]
