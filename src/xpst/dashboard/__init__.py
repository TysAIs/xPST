"""
xPST Dashboard

API-only mode: exposes analytics and server modules for programmatic access.
NiceGUI UI code has been removed; use the desktop app for the GUI.
"""

from xpst.dashboard.analytics import AnalyticsCollector

__all__ = ["AnalyticsCollector"]
