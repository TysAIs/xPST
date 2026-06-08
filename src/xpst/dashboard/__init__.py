"""
xPST Dashboard

Web-based analytics dashboard built with NiceGUI.
Provides overview, posts, analytics, platform health, and settings views.
"""

from xpst.dashboard.app import create_dashboard
from xpst.dashboard.server import start_dashboard

__all__ = ["create_dashboard", "start_dashboard"]
