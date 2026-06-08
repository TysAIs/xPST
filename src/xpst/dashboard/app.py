"""
XPST Dashboard App — Apple macOS Settings Aesthetic

Dark/Light theme with clean typography, proper spacing, and smooth animations.
Pages: Overview · Content Library · Analytics · Connect · Settings
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta
from pathlib import Path

from nicegui import ui

from xpst.config import XPSTConfig
from xpst.dashboard.analytics import (
    PLATFORM_BADGE_LABELS,
    PLATFORM_COLORS,
    PLATFORM_ICONS,
    PLATFORM_LABELS,
    AnalyticsCollector,
)

# ── Design Tokens (Dark Mode - Default) ───────────────────────────────
DARK_BG = "#1c1c1e"
DARK_SURFACE = "#2c2c2e"
DARK_SIDEBAR = "#3a3a3c"
DARK_TEXT = "#ffffff"
DARK_TEXT_SEC = "#ebebf5"
DARK_TEXT_MUTED = "#8e8e93"
DARK_BORDER = "rgba(255,255,255,0.08)"

LIGHT_BG = "#ffffff"
LIGHT_SURFACE = "#f2f2f7"
LIGHT_SIDEBAR = "#e5e5ea"
LIGHT_TEXT = "#1c1c1e"
LIGHT_TEXT_SEC = "#3a3a3c"
LIGHT_TEXT_MUTED = "#8e8e93"
LIGHT_BORDER = "rgba(0,0,0,0.08)"

ACCENT = "#0a84ff"
GREEN = "#30d158"
RED = "#ff453a"
ORANGE = "#ff9f0a"

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"

# ── CSS ────────────────────────────────────────────────────────────────
CUSTOM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
@import url('https://fonts.googleapis.com/icon?family=Material+Icons');

* {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif !important;
    box-sizing: border-box;
}

html {
    font-size: 16px;
}

:root {
    --sidebar-width: 240px;
    --sidebar-collapsed: 0px;
}

:root {
    --bg: #1c1c1e;
    --surface: #2c2c2e;
    --sidebar-bg: #3a3a3c;
    --text: #ffffff;
    --text-sec: #ebebf5;
    --text-muted: #8e8e93;
    --border: rgba(255,255,255,0.08);
    --card-shadow: 0 1px 3px rgba(0,0,0,0.3);
    --card-hover-shadow: 0 4px 12px rgba(0,0,0,0.4);
}

body.light-mode {
    --bg: #ffffff;
    --surface: #f2f2f7;
    --sidebar-bg: #e5e5ea;
    --text: #1c1c1e;
    --text-sec: #3a3a3c;
    --text-muted: #8e8e93;
    --border: rgba(0,0,0,0.08);
    --card-shadow: 0 1px 3px rgba(0,0,0,0.08);
    --card-hover-shadow: 0 4px 12px rgba(0,0,0,0.12);
}

body, .q-page, .q-layout {
    background: var(--bg) !important;
    color: var(--text) !important;
    transition: background 0.3s ease, color 0.3s ease;
    overflow-x: hidden;
}

.q-drawer {
    background: var(--sidebar-bg) !important;
    border-right: 1px solid var(--border) !important;
    transition: background 0.3s ease;
}

/* ── Hamburger Toggle ─────────────────────────────────── */
.xpst-hamburger {
    display: none;
    position: fixed;
    top: 12px;
    left: 12px;
    z-index: 1000;
    width: 40px;
    height: 40px;
    border-radius: 10px;
    background: var(--surface);
    border: 1px solid var(--border);
    align-items: center;
    justify-content: center;
    cursor: pointer;
    transition: all 0.2s ease;
    box-shadow: 0 2px 8px rgba(0,0,0,0.2);
}

.xpst-hamburger:hover {
    background: var(--sidebar-bg);
}

.xpst-hamburger .material-icons {
    font-size: 22px;
    color: var(--text);
}

/* ── Sidebar ─────────────────────────────────────────── */
.xpst-sidebar {
    width: var(--sidebar-width);
    min-width: var(--sidebar-width);
    min-height: 100vh;
    border-right: 1px solid var(--border);
    transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1), opacity 0.3s ease;
}

.xpst-main-content {
    flex: 1;
    min-width: 0;
    max-width: 100%;
}

.xpst-content-inner {
    width: 100%;
    max-width: 1400px;
    margin: 0 auto;
}

.sidebar-container {
    padding: 20px 16px;
    height: 100%;
    display: flex;
    flex-direction: column;
}

.sidebar-logo {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 32px;
    padding: 0 8px;
}

.sidebar-logo-text {
    font-size: 20px;
    font-weight: 700;
    color: var(--text);
    letter-spacing: -0.02em;
}

.nav-item {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 14px;
    border-radius: 8px;
    color: var(--text-muted);
    text-decoration: none;
    font-size: 14px;
    font-weight: 500;
    transition: all 0.15s ease;
    cursor: pointer;
    margin-bottom: 2px;
}

.nav-item:hover {
    background: rgba(255,255,255,0.06);
    color: var(--text);
}

body.light-mode .nav-item:hover {
    background: rgba(0,0,0,0.05);
}

.nav-item.active {
    background: rgba(10,132,255,0.15);
    color: #0a84ff;
}

/* ── Cards Grid ──────────────────────────────────────── */
.metric-cards-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    width: 100%;
}

.content-cards-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: 16px;
    width: 100%;
}

.charts-row {
    display: flex;
    gap: 16px;
    width: 100%;
    flex-wrap: wrap;
}

.chart-main {
    flex: 1;
    min-width: 0;
}

.chart-side {
    width: 320px;
    min-width: 260px;
    flex-shrink: 1;
}

/* ── Cards ───────────────────────────────────────────── */
.metric-card {
    background: var(--surface);
    border-radius: 12px;
    padding: 20px;
    border: 1px solid var(--border);
    transition: all 0.2s ease;
    min-width: 0;
}

.metric-card:hover {
    transform: translateY(-2px);
    box-shadow: var(--card-hover-shadow);
}

.metric-card-icon {
    width: 36px;
    height: 36px;
    border-radius: 10px;
    display: flex;
    align-items: center;
    justify-content: center;
    margin-bottom: 12px;
}

.metric-value {
    font-size: clamp(1.4rem, 2.5vw, 1.75rem);
    font-weight: 700;
    color: var(--text);
    line-height: 1.1;
    letter-spacing: -0.02em;
    word-break: break-word;
}

.metric-label {
    font-size: clamp(0.7rem, 1vw, 0.75rem);
    color: var(--text-muted);
    margin-top: 4px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-weight: 500;
}

.glass-card {
    background: var(--surface);
    border-radius: 12px;
    border: 1px solid var(--border);
    padding: 20px;
    transition: all 0.2s ease;
}

.glass-card:hover {
    box-shadow: var(--card-hover-shadow);
}

.content-card {
    background: var(--surface);
    border-radius: 12px;
    border: 1px solid var(--border);
    overflow: hidden;
    transition: all 0.2s ease;
}

.content-card:hover {
    transform: translateY(-2px);
    box-shadow: var(--card-hover-shadow);
}

.connect-card {
    background: var(--surface);
    border-radius: 12px;
    border: 1px solid var(--border);
    padding: 28px;
    text-align: center;
    transition: all 0.2s ease;
}

.connect-card:hover {
    transform: translateY(-2px);
    box-shadow: var(--card-hover-shadow);
}

/* ── Badges ──────────────────────────────────────────── */
.badge {
    display: inline-flex;
    align-items: center;
    padding: 3px 8px;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 600;
    color: #ffffff;
    letter-spacing: 0.02em;
}

.badge-sm {
    padding: 2px 6px;
    font-size: 10px;
    border-radius: 4px;
}

/* ── Status ──────────────────────────────────────────── */
.status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    display: inline-block;
    margin-right: 6px;
}

.status-healthy { background: #30d158; box-shadow: 0 0 6px rgba(48,209,88,0.4); }
.status-degraded { background: #ff9f0a; box-shadow: 0 0 6px rgba(255,159,10,0.4); }
.status-error { background: #ff453a; box-shadow: 0 0 6px rgba(255,69,58,0.4); }
.status-unknown { background: #8e8e93; }

/* ── Typography ──────────────────────────────────────── */
.page-title {
    font-size: clamp(1.4rem, 2.5vw, 1.75rem);
    font-weight: 700;
    color: var(--text);
    margin-bottom: 6px;
    letter-spacing: -0.02em;
}

.page-subtitle {
    font-size: clamp(0.8rem, 1.2vw, 0.875rem);
    color: var(--text-muted);
    margin-bottom: 28px;
}

.section-header {
    font-size: clamp(1rem, 1.5vw, 1.125rem);
    font-weight: 600;
    color: var(--text);
    margin-bottom: 16px;
    letter-spacing: -0.01em;
}

.settings-section-title {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 16px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border);
}

/* ── Tabs & Pills ────────────────────────────────────── */
.platform-tab {
    padding: 8px 18px;
    border-radius: 8px;
    font-size: clamp(0.75rem, 1vw, 0.8125rem);
    font-weight: 500;
    color: var(--text-muted);
    cursor: pointer;
    transition: all 0.15s ease;
    border: 1px solid transparent;
    background: transparent;
}

.platform-tab:hover {
    color: var(--text);
    background: rgba(255,255,255,0.04);
}

body.light-mode .platform-tab:hover {
    background: rgba(0,0,0,0.04);
}

.platform-tab.active {
    color: #0a84ff;
    background: rgba(10,132,255,0.12);
    border-color: rgba(10,132,255,0.2);
}

.filter-pill {
    padding: 6px 14px;
    border-radius: 20px;
    font-size: clamp(0.75rem, 1vw, 0.8125rem);
    font-weight: 500;
    color: var(--text-muted);
    cursor: pointer;
    transition: all 0.15s ease;
    border: 1px solid var(--border);
    background: transparent;
}

.filter-pill:hover {
    color: var(--text);
    border-color: rgba(255,255,255,0.15);
}

body.light-mode .filter-pill:hover {
    border-color: rgba(0,0,0,0.15);
}

.filter-pill.active {
    color: #ffffff;
    background: #0a84ff;
    border-color: #0a84ff;
}

/* ── Thumbnails ──────────────────────────────────────── */
.thumb-placeholder {
    width: 100%;
    aspect-ratio: 16/9;
    border-radius: 8px 8px 0 0;
    display: flex;
    align-items: center;
    justify-content: center;
    position: relative;
    overflow: hidden;
}

/* ── Theme Toggle ────────────────────────────────────── */
.theme-toggle {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    border-radius: 8px;
    cursor: pointer;
    transition: all 0.15s ease;
    color: var(--text-muted);
    font-size: 13px;
}

.theme-toggle:hover {
    background: rgba(255,255,255,0.06);
    color: var(--text);
}

body.light-mode .theme-toggle:hover {
    background: rgba(0,0,0,0.05);
}

/* ── Animations ──────────────────────────────────────── */
.fade-in {
    animation: fadeIn 0.2s ease;
}

@keyframes fadeIn {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
}

/* ── Scrollbar ───────────────────────────────────────── */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(128,128,128,0.2); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: rgba(128,128,128,0.3); }

/* ── Plotly ──────────────────────────────────────────── */
.js-plotly-plot .plotly { background: transparent !important; }
.js-plotly-plot {
    width: 100% !important;
    max-width: 100%;
    overflow: hidden;
}

/* ── NiceGUI overrides ───────────────────────────────── */
.q-field {
    width: 100%;
}
.q-field__control {
    max-width: 100%;
}
.q-field__label {
    color: var(--text-sec) !important;
    font-size: clamp(0.75rem, 1vw, 0.8125rem) !important;
}

.q-field__control {
    background: var(--bg) !important;
    border-color: var(--border) !important;
}

body.light-mode .q-field__control {
    background: #f2f2f7 !important;
}

.q-toggle__label {
    color: var(--text-sec) !important;
    font-size: clamp(0.8rem, 1.1vw, 0.875rem) !important;
}

/* ── Settings Form Responsive ────────────────────────── */
.settings-form-row {
    display: flex;
    gap: 16px;
    width: 100%;
    flex-wrap: wrap;
}

.settings-form-row > .q-field,
.settings-form-row > .q-input,
.settings-form-row > .q-number {
    flex: 1;
    min-width: 140px;
}

/* ── Path rows responsive ────────────────────────────── */
.path-row {
    flex-wrap: wrap;
}
.path-row .path-value {
    word-break: break-all;
    min-width: 0;
}

/* ── Overlay for mobile sidebar ──────────────────────── */
.xpst-overlay {
    display: none;
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(0,0,0,0.5);
    z-index: 998;
    opacity: 0;
    transition: opacity 0.3s ease;
}
.xpst-overlay.visible {
    display: block;
    opacity: 1;
}

/* ══════════════════════════════════════════════════════ */
/* ── RESPONSIVE BREAKPOINTS ──────────────────────────── */
/* ══════════════════════════════════════════════════════ */

/* ── Tablet (≤ 1200px) ──────────────────────────────── */
@media (max-width: 1200px) {
    .xpst-sidebar {
        width: 220px;
        min-width: 220px;
    }

    .xpst-content-inner {
        padding: 20px 24px !important;
    }

    .metric-cards-grid {
        grid-template-columns: repeat(2, 1fr);
    }

    .content-cards-grid {
        grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    }

    .chart-side {
        width: 280px;
        min-width: 240px;
    }

    .connect-card {
        width: calc(50% - 12px) !important;
    }
}

/* ── Mobile (≤ 768px) ───────────────────────────────── */
@media (max-width: 768px) {
    .xpst-hamburger {
        display: flex;
    }

    .xpst-sidebar {
        position: fixed;
        top: 0;
        left: 0;
        height: 100vh;
        z-index: 999;
        transform: translateX(-100%);
        box-shadow: 4px 0 24px rgba(0,0,0,0.3);
    }

    .xpst-sidebar.open {
        transform: translateX(0);
    }

    .xpst-content-inner {
        padding: 56px 16px 20px 16px !important;
    }

    .metric-cards-grid {
        grid-template-columns: repeat(2, 1fr);
        gap: 10px;
    }

    .metric-card {
        padding: 14px;
    }

    .charts-row {
        flex-direction: column;
    }

    .chart-side {
        width: 100%;
        min-width: 0;
    }

    .content-cards-grid {
        grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
        gap: 10px;
    }

    .page-title {
        font-size: 1.3rem;
    }

    .page-subtitle {
        margin-bottom: 16px;
    }

    .platform-tab {
        padding: 6px 12px;
    }

    .connect-card {
        width: calc(50% - 8px) !important;
    }

    .settings-form-row {
        flex-direction: column;
    }

    .settings-form-row > .q-field,
    .settings-form-row > .q-input,
    .settings-form-row > .q-number {
        min-width: 100%;
    }

    /* Hide sidebar footer on mobile for cleaner look */
    .sidebar-container {
        padding: 16px 12px;
    }

    .sidebar-logo {
        margin-bottom: 24px;
    }

    .sidebar-logo-text {
        font-size: 18px;
    }
}

/* ── Small Mobile (≤ 480px) ─────────────────────────── */
@media (max-width: 480px) {
    .xpst-content-inner {
        padding: 52px 10px 16px 10px !important;
    }

    .metric-cards-grid {
        grid-template-columns: 1fr;
        gap: 8px;
    }

    .content-cards-grid {
        grid-template-columns: 1fr;
    }

    .connect-card {
        width: 100% !important;
    }

    .metric-card {
        padding: 12px;
    }

    .metric-value {
        font-size: 1.3rem;
    }

    .glass-card {
        padding: 14px;
    }

    .section-header {
        font-size: 0.95rem;
    }

    /* Tabs should scroll horizontally */
    .analytics-tabs,
    .filter-pills {
        overflow-x: auto;
        flex-wrap: nowrap;
        -webkit-overflow-scrolling: touch;
        scrollbar-width: none;
    }
    .analytics-tabs::-webkit-scrollbar,
    .filter-pills::-webkit-scrollbar {
        display: none;
    }
}
</style>
"""


# ── Helpers ─────────────────────────────────────────────────────────────

def _fmt_num(n: int | float | None) -> str:
    """Format number with K/M suffix."""
    if n is None:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))


def _relative_time(ts_str: str | None) -> str:
    """ISO timestamp → '2h ago' style."""
    if not ts_str:
        return "—"
    try:
        dt = datetime.fromisoformat(ts_str)
        delta = datetime.now() - dt
        secs = delta.total_seconds()
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{int(secs / 60)}m ago"
        if secs < 86400:
            return f"{int(secs / 3600)}h ago"
        return f"{int(secs / 86400)}d ago"
    except Exception:
        return ts_str[:10] if ts_str else "—"


def _status_color(status: str) -> str:
    return {"ok": GREEN, "degraded": ORANGE, "error": RED, "unknown": DARK_TEXT_MUTED}.get(status, DARK_TEXT_MUTED)


def _status_label(status: str) -> str:
    return {"ok": "Healthy", "degraded": "Degraded", "error": "Error", "unknown": "Unknown"}.get(status, "Unknown")


# ── Shared sidebar ──────────────────────────────────────────────────────

_NAV_ITEMS = [
    ("/", "dashboard", "Overview"),
    ("/content", "video_library", "Content"),
    ("/analytics", "insights", "Analytics"),
    ("/connect", "link", "Connect"),
    ("/settings", "settings", "Settings"),
]


def _sidebar(current: str = "/"):
    """Render the sidebar navigation."""
    with ui.column().classes("sidebar-container"):
        # Logo
        with ui.row().classes("sidebar-logo"):
            ui.icon("hub", size="24px", color=ACCENT)
            ui.label("XPST").classes("sidebar-logo-text")

        # Navigation
        for path, icon, label in _NAV_ITEMS:
            is_active = path == current
            cls = "nav-item active" if is_active else "nav-item"
            with ui.link(target=path).classes(cls):
                ui.icon(icon, size="20px")
                ui.label(label)

        # Spacer
        ui.element("div").style("flex:1;")

        # Theme toggle
        with ui.element("div").classes("theme-toggle").on("click", lambda: _toggle_theme()):
            ui.icon("dark_mode", size="18px").mark("theme-icon")
            ui.label("Dark Mode").mark("theme-label")

        # Footer
        with ui.column().style("padding:0 8px; margin-top:12px;"):
            ui.label("XPST v0.1.0").style("font-size:11px; color:var(--text-muted);")


def _toggle_theme():
    """Toggle between dark and light mode."""
    ui.run_javascript("""
        const body = document.body;
        const isLight = body.classList.toggle('light-mode');
        localStorage.setItem('xpst-theme', isLight ? 'light' : 'dark');
        const icon = document.querySelector('[data-xpst-mark="theme-icon"]');
        const label = document.querySelector('[data-xpst-mark="theme-label"]');
        if (icon) icon.innerText = isLight ? 'light_mode' : 'dark_mode';
        if (label) label.innerText = isLight ? 'Light Mode' : 'Dark Mode';
    """)


# ── Page Shell ──────────────────────────────────────────────────────────

def _page_shell(current: str = "/"):
    """Build sidebar + main content layout."""
    ui.add_head_html(CUSTOM_CSS)
    # Viewport meta tag for responsive design
    ui.add_head_html(
        '<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=5">'
    )
    # Restore theme from localStorage + sidebar toggle logic
    ui.add_head_html("""
    <script>
    (function() {
        const theme = localStorage.getItem('xpst-theme');
        if (theme === 'light') {
            document.body.classList.add('light-mode');
        }
    })();

    function xpstToggleSidebar() {
        const sidebar = document.querySelector('.xpst-sidebar');
        const overlay = document.querySelector('.xpst-overlay');
        if (sidebar) {
            sidebar.classList.toggle('open');
            if (overlay) {
                overlay.classList.toggle('visible');
            }
        }
    }

    function xpstCloseSidebar() {
        const sidebar = document.querySelector('.xpst-sidebar');
        const overlay = document.querySelector('.xpst-overlay');
        if (sidebar) {
            sidebar.classList.remove('open');
            if (overlay) {
                overlay.classList.remove('visible');
            }
        }
    }
    </script>
    """)

    # Hamburger button (visible on mobile via CSS)
    ui.html(
        '<div class="xpst-hamburger" onclick="xpstToggleSidebar()">'
        '<span class="material-icons">menu</span></div>'
    )

    # Overlay for closing sidebar on mobile
    ui.html('<div class="xpst-overlay" onclick="xpstCloseSidebar()"></div>')

    with ui.row().classes("w-full no-wrap").style("min-height:100vh;"):
        with ui.column().classes("xpst-sidebar"):
            _sidebar(current)
        with ui.column().classes("xpst-main-content fade-in").style("min-height:100vh;") as main:
            with ui.column().classes("xpst-content-inner").style("padding:28px 36px;"):
                pass  # caller appends inside this
    return main


# ── Metric Card Component ──────────────────────────────────────────────

def _metric_card(icon: str, value: str, label: str, accent_color: str = ACCENT) -> None:
    """Render a metric card with icon, value, and label."""
    with ui.element("div").classes("metric-card"):
        with ui.element("div").classes("metric-card-icon").style(f"background:{accent_color}18;"):
            ui.icon(icon, size="20px", color=accent_color)
        ui.label(value).classes("metric-value")
        ui.label(label).classes("metric-label")


def _section_header(title: str, subtitle: str = "") -> None:
    """Render a page section header."""
    ui.label(title).classes("page-title")
    if subtitle:
        ui.label(subtitle).classes("page-subtitle")


# ── Plotly Theme Helper ─────────────────────────────────────────────────

def _plotly_layout(fig, height: int = 280):
    """Apply consistent dark-themed layout to a Plotly figure."""
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#ebebf5", family="Inter", size=12),
        xaxis=dict(gridcolor="rgba(128,128,128,0.1)", zeroline=False, showgrid=False),
        yaxis=dict(gridcolor="rgba(128,128,128,0.1)", zeroline=False, showgrid=True),
        margin=dict(t=10, b=30, l=40, r=10),
        height=height,
        autosize=True,
        hovermode="x unified",
    )
    fig.update_xaxes(automargin=True)
    fig.update_yaxes(automargin=True)


# ── Page: Overview (/) ──────────────────────────────────────────────────

def _page_overview(collector: AnalyticsCollector) -> None:
    """Render the Overview dashboard page."""
    import plotly.graph_objects as go

    stats = collector.get_summary_stats()
    posts = collector.get_all_posts()
    health = collector.get_platform_health_all()

    with _page_shell("/"):
        _section_header("Dashboard", "Your cross-platform content at a glance")

        # ── Metric Cards ────────────────────────────────────────────────
        with ui.element("div").classes("metric-cards-grid").style("margin-bottom:24px;"):
            _metric_card("video_library", str(stats["total_posts"]), "Total Posts", ACCENT)
            _metric_card("visibility", _fmt_num(stats["total_platform_posts"]), "Total Reach", "#a855f7")
            best = stats.get("best_platform")
            best_label = PLATFORM_LABELS.get(best, "—") if best else "—"
            best_color = PLATFORM_COLORS.get(best, DARK_TEXT_MUTED) if best else DARK_TEXT_MUTED
            _metric_card("emoji_events", best_label, "Best Platform", best_color)
            _metric_card("schedule", str(stats.get("posts_this_week", 0)), "This Week", GREEN)

        # ── Charts Row ──────────────────────────────────────────────────
        with ui.element("div").classes("charts-row").style("margin-bottom:24px;"):
            # Line chart — Posts over time
            with ui.element("div").classes("glass-card chart-main"):
                ui.label("Posts Over Time").classes("section-header")

                posts_by_date = collector.get_posts_over_time(30)
                if posts_by_date:
                    dates = list(posts_by_date.keys())
                    counts = list(posts_by_date.values())
                else:
                    dates = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(29, -1, -1)]
                    counts = [0] * 30

                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=dates, y=counts,
                    mode="lines+markers",
                    line=dict(color=ACCENT, width=2, shape="spline"),
                    marker=dict(size=5, color=ACCENT, line=dict(width=0)),
                    fill="tozeroy",
                    fillcolor="rgba(10,132,255,0.08)",
                    hovertemplate="%{x}<br>%{y} posts<extra></extra>",
                ))
                _plotly_layout(fig, 260)
                ui.plotly(fig).classes("w-full").style("pointer-events:none; width:100%; min-width:0;")

            # Pie chart — Platform breakdown
            with ui.element("div").classes("glass-card chart-side"):
                ui.label("By Platform").classes("section-header")

                pc = stats["platform_counts"]
                labels = [PLATFORM_LABELS[k] for k, v in pc.items() if v > 0]
                values = [v for v in pc.values() if v > 0]
                colors = [PLATFORM_COLORS[k] for k, v in pc.items() if v > 0]

                if not values:
                    labels = ["No posts yet"]
                    values = [1]
                    colors = ["#8e8e93"]

                fig2 = go.Figure(data=[go.Pie(
                    labels=labels, values=values,
                    hole=0.6,
                    marker=dict(colors=colors, line=dict(color="#2c2c2e", width=2)),
                    textfont=dict(color="white", size=11, family="Inter"),
                    hovertemplate="%{label}: %{value}<extra></extra>",
                    sort=False,
                )])
                _plotly_layout(fig2, 260)
                fig2.update_layout(
                    showlegend=True,
                    legend=dict(
                        font=dict(color="#8e8e93", size=11),
                        orientation="h",
                        yanchor="bottom", y=-0.15,
                        xanchor="center", x=0.5,
                    ),
                    margin=dict(t=10, b=40, l=10, r=10),
                )
                ui.plotly(fig2).classes("w-full").style("pointer-events:none; width:100%; min-width:0;")

        # ── Recent Posts ────────────────────────────────────────────────
        with ui.element("div").classes("glass-card w-full").style("margin-bottom:24px;"):
            ui.label("Recent Posts").classes("section-header")

            if posts:
                with ui.element("div").classes("content-cards-grid"):
                    for p in posts[:6]:
                        plats = list(p.get("platforms", {}).keys())
                        main_plat = plats[0] if plats else "youtube"
                        plat_color = PLATFORM_COLORS.get(main_plat, ACCENT)

                        with ui.element("div").classes("content-card"):
                            with ui.element("div").classes("thumb-placeholder").style(
                                f"background: linear-gradient(135deg, {plat_color}30 0%, {plat_color}10 100%);"
                            ):
                                ui.icon(PLATFORM_ICONS.get(main_plat, "video_library"), size="32px", color=plat_color)

                            with ui.column().style("padding:14px;"):
                                caption = (p.get("caption") or "")[:80]
                                if len(p.get("caption") or "") > 80:
                                    caption += "…"
                                ui.label(caption).style(
                                    "font-size:13px; font-weight:500; color:var(--text); line-height:1.4; margin-bottom:8px;"
                                )
                                with ui.row().classes("items-center gap-2"):
                                    for plat in plats:
                                        p_color = PLATFORM_COLORS.get(plat, "#888")
                                        p_label = PLATFORM_BADGE_LABELS.get(plat, plat[:2].upper())
                                        ui.html(
                                            f'<span class="badge badge-sm" style="background:{p_color};">{p_label}</span>'
                                        )
                                    ui.label(_relative_time(p.get("downloaded_at"))).style(
                                        "font-size:11px; color:var(--text-muted); margin-left:auto;"
                                    )
            else:
                with ui.column().classes("items-center w-full").style("padding:40px 0;"):
                    ui.icon("video_library", size="48px", color=DARK_TEXT_MUTED)
                    ui.label("No posts yet").style("font-size:14px; color:var(--text-sec); margin-top:12px;")
                    ui.label("Run `xpst run` to get started").style("font-size:13px; color:var(--text-muted);")

        # ── Platform Health ─────────────────────────────────────────────
        with ui.element("div").classes("glass-card w-full"):
            ui.label("Platform Health").classes("section-header")

            with ui.element("div").classes("content-cards-grid"):
                for p in health:
                    color = p["color"]
                    status = p["status"]
                    configured = p["configured"]
                    failures = p["failures"]
                    cb = p["circuit_breaker_open"]

                    if not configured:
                        dot_cls = "status-unknown"
                        status_text = "Not Configured"
                    elif cb:
                        dot_cls = "status-error"
                        status_text = "Circuit Breaker Open"
                    elif failures > 0:
                        dot_cls = "status-degraded"
                        status_text = f"Degraded ({failures} failures)"
                    elif status == "ok":
                        dot_cls = "status-healthy"
                        status_text = "Healthy"
                    else:
                        dot_cls = "status-unknown"
                        status_text = "Unknown"

                    with ui.element("div").classes("content-card").style("flex:1; min-width:180px; padding:16px;"):
                        with ui.row().classes("items-center gap-3"):
                            with ui.element("div").style(
                                f"width:32px; height:32px; border-radius:8px; background:{color}15; display:flex; align-items:center; justify-content:center;"
                            ):
                                ui.icon(p["icon"], size="18px", color=color)
                            with ui.column().classes("gap-0"):
                                ui.label(p["label"]).style("font-size:14px; font-weight:600; color:var(--text);")
                                with ui.row().classes("items-center"):
                                    ui.html(f'<span class="status-dot {dot_cls}"></span>')
                                    ui.label(status_text).style("font-size:12px; color:var(--text-muted);")


# ── Page: Content Library (/content) ───────────────────────────────────

def _page_content(collector: AnalyticsCollector) -> None:
    """Render the Content Library page."""
    posts = collector.get_all_posts()

    with _page_shell("/content"):
        _section_header("Content Library", "Browse and manage all your cross-posted content")

        # Search & Filters
        with ui.element("div").classes("glass-card w-full").style("margin-bottom:20px; padding:16px 20px;"):
            with ui.row().classes("items-center gap-4 w-full"):
                ui.input(placeholder="Search posts...").props(
                    'outlined dense'
                ).classes("col-grow").style("max-width:380px;")

                with ui.row().classes("gap-2 filter-pills").style("overflow-x:auto; flex-wrap:nowrap;"):
                    for label, plat in [("All", "all"), ("YouTube", "youtube"), ("Instagram", "instagram"), ("X", "x"), ("TikTok", "tiktok")]:
                        ui.button(label, on_click=lambda p=plat: _filter_posts(p)).classes("filter-pill")

        posts_container = ui.column().classes("w-full")

        def _filter_posts(platform: str) -> None:
            posts_container.clear()
            filtered = posts
            if platform != "all":
                filtered = [p for p in posts if platform in p.get("platforms", {})]
            _render_posts_grid(posts_container, filtered)

        _render_posts_grid(posts_container, posts)


def _render_posts_grid(container, posts: list):
    """Render the content library grid."""
    with container:
        if not posts:
            with ui.column().classes("items-center w-full").style("padding:60px 0;"):
                ui.icon("video_library", size="48px", color=DARK_TEXT_MUTED)
                ui.label("No content found").style("font-size:15px; color:var(--text-sec); margin-top:12px;")
                ui.label("Your cross-posted content will appear here").style("font-size:13px; color:var(--text-muted);")
            return

        with ui.element("div").classes("content-cards-grid"):
            for p in posts:
                plats = list(p.get("platforms", {}).keys())
                main_plat = plats[0] if plats else "youtube"
                plat_color = PLATFORM_COLORS.get(main_plat, ACCENT)
                status = p.get("status", "posted")

                with ui.element("div").classes("content-card"):
                    with ui.element("div").classes("thumb-placeholder").style(
                        f"background: linear-gradient(135deg, {plat_color}25 0%, {plat_color}08 100%);"
                    ):
                        ui.icon(PLATFORM_ICONS.get(main_plat, "movie"), size="36px", color=plat_color)

                        if status == "posted":
                            status_html = '<span class="badge" style="position:absolute; top:10px; right:10px; background:rgba(48,209,88,0.9); font-size:10px; z-index:1;">✓ POSTED</span>'
                        elif status == "pending":
                            status_html = '<span class="badge" style="position:absolute; top:10px; right:10px; background:rgba(255,159,10,0.9); font-size:10px; z-index:1;">⏳ PENDING</span>'
                        else:
                            status_html = '<span class="badge" style="position:absolute; top:10px; right:10px; background:rgba(255,69,58,0.9); font-size:10px; z-index:1;">✗ FAILED</span>'
                        ui.html(status_html)

                    with ui.column().style("padding:14px;"):
                        caption = (p.get("caption") or "")[:100]
                        if len(p.get("caption") or "") > 100:
                            caption += "…"
                        ui.label(caption).style(
                            "font-size:13px; font-weight:500; color:var(--text); line-height:1.4; margin-bottom:8px; min-height:36px;"
                        )

                        with ui.row().classes("items-center gap-1").style("margin-bottom:8px;"):
                            for plat in plats:
                                p_color = PLATFORM_COLORS.get(plat, "#888")
                                p_label = PLATFORM_BADGE_LABELS.get(plat, plat[:2].upper())
                                url = p.get("platforms", {}).get(plat, {}).get("url", "")
                                if url:
                                    ui.html(f'<a href="{url}" target="_blank"><span class="badge badge-sm" style="background:{p_color};">{p_label}</span></a>')
                                else:
                                    ui.html(f'<span class="badge badge-sm" style="background:{p_color};">{p_label}</span>')

                        with ui.row().classes("items-center justify-between w-full"):
                            ui.label(_relative_time(p.get("downloaded_at"))).style("font-size:11px; color:var(--text-muted);")
                            ui.label(p["video_id"][:12] + "…").style("font-size:10px; color:var(--text-muted); font-family:monospace;")


# ── Page: Analytics (/analytics) ───────────────────────────────────────

def _page_analytics(collector: AnalyticsCollector) -> None:
    """Render the Analytics page."""
    with _page_shell("/analytics"):
        _section_header("Analytics", "Deep dive into your content performance")

        with ui.row().classes("gap-2 w-full analytics-tabs").style("margin-bottom:24px; overflow-x:auto; flex-wrap:nowrap;"):
            for label, plat in [("All", "all"), ("YouTube", "youtube"), ("Instagram", "instagram"), ("X / Twitter", "x"), ("TikTok", "tiktok")]:
                ui.button(label, on_click=lambda p=plat: _show_analytics(p)).classes("platform-tab")

        analytics_container = ui.column().classes("w-full")

        def _show_analytics(platform: str) -> None:
            analytics_container.clear()
            _render_analytics_content(analytics_container, collector, platform)

        _render_analytics_content(analytics_container, collector, "all")


def _render_analytics_content(container, collector: AnalyticsCollector, platform: str) -> None:
    """Render analytics charts and metrics."""
    import plotly.graph_objects as go

    stats = collector.get_summary_stats()
    posts = collector.get_all_posts()
    engagement = collector.get_engagement_data()
    top_posts = collector.get_top_posts(5)

    with container:
        # Metrics
        if platform == "all":
            total_views = sum(e["views"] for e in engagement.values())
            total_likes = sum(e["likes"] for e in engagement.values())
            total_comments = sum(e["comments"] for e in engagement.values())
            total_shares = sum(e["shares"] for e in engagement.values())
        else:
            e = engagement.get(platform, {"views": 0, "likes": 0, "comments": 0, "shares": 0})
            total_views = e["views"]
            total_likes = e["likes"]
            total_comments = e["comments"]
            total_shares = e["shares"]

        with ui.element("div").classes("metric-cards-grid").style("margin-bottom:24px;"):
            _metric_card("visibility", _fmt_num(total_views), "Total Views", ACCENT)
            _metric_card("favorite", _fmt_num(total_likes), "Total Likes", RED)
            _metric_card("chat_bubble", _fmt_num(total_comments), "Comments", "#a855f7")
            _metric_card("share", _fmt_num(total_shares), "Shares", GREEN)

        # Per-Platform Breakdown
        if platform == "all":
            with ui.element("div").classes("glass-card w-full").style("margin-bottom:24px;"):
                ui.label("Per-Platform Breakdown").classes("section-header")
                with ui.element("div").classes("content-cards-grid"):
                    for plat_name in ["youtube", "instagram", "x", "tiktok"]:
                        plat_eng = engagement.get(plat_name, {"posts": 0, "views": 0, "likes": 0, "comments": 0, "shares": 0})
                        plat_color = PLATFORM_COLORS.get(plat_name, ACCENT)
                        plat_label = PLATFORM_LABELS.get(plat_name, plat_name.title())

                        with ui.element("div").classes("content-card").style("padding:16px;"):
                            with ui.row().classes("items-center gap-3").style("margin-bottom:12px;"):
                                with ui.element("div").style(
                                    f"width:32px; height:32px; border-radius:8px; background:{plat_color}15; display:flex; align-items:center; justify-content:center;"
                                ):
                                    ui.icon(PLATFORM_ICONS.get(plat_name, "circle"), size="18px", color=plat_color)
                                ui.label(plat_label).style("font-size:14px; font-weight:600; color:var(--text);")

                            with ui.column().classes("gap-2"):
                                for metric_name, metric_key in [("Views", "views"), ("Likes", "likes"), ("Comments", "comments"), ("Posts", "posts")]:
                                    with ui.row().classes("justify-between w-full"):
                                        ui.label(metric_name).style("font-size:12px; color:var(--text-muted);")
                                        ui.label(_fmt_num(plat_eng[metric_key])).style("font-size:13px; font-weight:600; color:var(--text);")

        with ui.element("div").classes("charts-row").style("margin-bottom:24px;"):
            # Engagement Over Time
            with ui.element("div").classes("glass-card chart-main"):
                ui.label("Engagement Over Time").classes("section-header")

                posts_by_date = collector.get_posts_over_time(30)
                dates = list(posts_by_date.keys()) if posts_by_date else []
                counts = list(posts_by_date.values()) if posts_by_date else []

                if platform != "all":
                    filtered_dates = []
                    for p in posts:
                        if platform in p.get("platforms", {}):
                            ts = p.get("downloaded_at")
                            if ts:
                                with contextlib.suppress(Exception):
                                    d = datetime.fromisoformat(ts).strftime("%Y-%m-%d")
                                    filtered_dates.append(d)
                    from collections import Counter
                    c = Counter(filtered_dates)
                    dates = sorted(c.keys())
                    counts = [c[d] for d in dates]

                if not dates:
                    dates = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(29, -1, -1)]
                    counts = [0] * 30

                line_color = PLATFORM_COLORS.get(platform, ACCENT) if platform != "all" else ACCENT

                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=dates, y=counts,
                    mode="lines+markers",
                    line=dict(color=line_color, width=2, shape="spline"),
                    marker=dict(size=5, color=line_color),
                    fill="tozeroy",
                    fillcolor=f"rgba({int(line_color[1:3],16)},{int(line_color[3:5],16)},{int(line_color[5:7],16)},0.08)",
                    hovertemplate="%{x}<br>%{y} posts<extra></extra>",
                ))
                _plotly_layout(fig, 280)
                ui.plotly(fig).classes("w-full").style("pointer-events:none; width:100%; min-width:0;")

            # Platform Comparison
            with ui.element("div").classes("glass-card chart-side"):
                ui.label("Platform Comparison").classes("section-header")

                pc = stats["platform_counts"]
                platforms_list = list(pc.keys())
                counts_list = list(pc.values())
                colors_list = [PLATFORM_COLORS.get(p, "#888") for p in platforms_list]
                labels_list = [PLATFORM_LABELS.get(p, p) for p in platforms_list]

                fig2 = go.Figure(data=[go.Bar(
                    x=labels_list, y=counts_list,
                    marker_color=colors_list,
                    marker=dict(line=dict(width=0), cornerradius=6),
                    text=counts_list,
                    textposition="outside",
                    textfont=dict(color="#ebebf5", size=12, family="Inter"),
                    hovertemplate="%{x}: %{y} posts<extra></extra>",
                )])
                _plotly_layout(fig2, 280)
                ui.plotly(fig2).classes("w-full").style("pointer-events:none; width:100%; min-width:0;")

        with ui.element("div").classes("charts-row"):
            # Heatmap
            with ui.element("div").classes("glass-card chart-main"):
                ui.label("Posting Activity Heatmap").classes("section-header")

                from collections import Counter

                hour_day: dict[str, Counter] = {}
                days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
                for p in posts:
                    ts = p.get("downloaded_at")
                    if ts:
                        try:
                            dt = datetime.fromisoformat(ts)
                            day = days[dt.weekday()]
                            hour = dt.hour
                            key = f"{hour:02d}:00"
                            hour_day.setdefault(key, Counter())[day] += 1
                        except Exception:
                            pass

                if hour_day:
                    hours = sorted(hour_day.keys())
                    z = [[hour_day.get(h, {}).get(d, 0) for d in days] for h in hours]
                    fig_heat = go.Figure(data=go.Heatmap(
                        z=z, x=days, y=hours,
                        colorscale=[[0, "#2c2c2e"], [0.3, "#1e3a5f"], [0.6, "#0a84ff"], [1, "#60c0ff"]],
                        hovertemplate="%{y} %{x}: %{z} posts<extra></extra>",
                        showscale=False,
                    ))
                    _plotly_layout(fig_heat, 280)
                    ui.plotly(fig_heat).classes("w-full").style("pointer-events:none; width:100%; min-width:0;")
                else:
                    with ui.column().classes("items-center w-full").style("padding:40px 0;"):
                        ui.label("Not enough data for heatmap").style("color:var(--text-muted);")

            # Top Posts
            with ui.element("div").classes("glass-card chart-side"):
                ui.label("Top Posts by Reach").classes("section-header")

                for i, p in enumerate(top_posts):
                    plats = list(p.get("platforms", {}).keys())
                    main_plat = plats[0] if plats else "youtube"
                    plat_color = PLATFORM_COLORS.get(main_plat, ACCENT)

                    with ui.row().classes("items-center gap-3 w-full").style("padding:10px 0; border-bottom:1px solid var(--border);"):
                        ui.label(f"#{i+1}").style("font-size:13px; font-weight:700; color:var(--text-muted); min-width:24px;")

                        with ui.element("div").style(
                            f"width:44px; height:30px; border-radius:6px; background:linear-gradient(135deg, {plat_color}25, {plat_color}08); display:flex; align-items:center; justify-content:center;"
                        ):
                            ui.icon(PLATFORM_ICONS.get(main_plat, "movie"), size="14px", color=plat_color)

                        with ui.column().classes("gap-0 col-grow"):
                            caption = (p.get("caption") or "")[:40]
                            if len(p.get("caption") or "") > 40:
                                caption += "…"
                            ui.label(caption).style("font-size:13px; font-weight:500; color:var(--text);")
                            with ui.row().classes("items-center gap-1"):
                                for plat in plats:
                                    ui.html(
                                        f'<span class="badge badge-sm" style="background:{PLATFORM_COLORS.get(plat, "#888")}; font-size:9px;">{PLATFORM_BADGE_LABELS.get(plat, plat[:2])}</span>'
                                    )

                        ui.label(f"{len(plats)}").style("font-size:14px; font-weight:700; color:var(--text-muted);")


# ── Page: Connect (/connect) ───────────────────────────────────────────

def _page_connect(collector: AnalyticsCollector) -> None:
    """Render the Connect page."""
    health = collector.get_platform_health_all()

    with _page_shell("/connect"):
        _section_header("Connect Accounts", "Link your social platforms to start cross-posting")

        connected_count = sum(1 for p in health if p["configured"])
        total_platforms = len(health)

        with ui.element("div").classes("glass-card w-full").style("margin-bottom:24px; padding:16px 20px;"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label(f"{connected_count} of {total_platforms} platforms connected").style("font-size:14px; color:var(--text-sec);")
                with ui.element("div").style("width:180px; height:6px; background:var(--border); border-radius:3px; overflow:hidden;"):
                    pct = (connected_count / total_platforms * 100) if total_platforms > 0 else 0
                    ui.element("div").style(f"width:{pct}%; height:100%; background:{ACCENT}; border-radius:3px; transition:width 0.5s ease;")

        with ui.element("div").classes("content-cards-grid"):
            for p in health:
                color = p["color"]
                configured = p["configured"]
                status = p["status"]
                icon = p["icon"]

                with ui.element("div").classes("connect-card"):
                    with ui.element("div").style(
                        f"width:48px; height:48px; border-radius:12px; background:{color}15; display:flex; align-items:center; justify-content:center; margin:0 auto 16px;"
                    ):
                        ui.icon(icon, size="24px", color=color)

                    ui.label(p["label"]).style("font-size:16px; font-weight:600; color:var(--text); margin-bottom:6px;")

                    if configured:
                        with ui.row().classes("items-center justify-center gap-1").style("margin-bottom:12px;"):
                            ui.icon("check_circle", size="16px", color=GREEN)
                            ui.label("Connected").style("font-size:13px; color:#30d158; font-weight:500;")

                        if p["name"] == "tiktok":
                            username = collector.config.get("accounts", {}).get("tiktok", {}).get("username", "")
                            if username:
                                ui.label(f"@{username}").style("font-size:12px; color:var(--text-muted); margin-bottom:12px;")
                        else:
                            ui.label(f"Last active: {_relative_time(p.get('last_success'))}").style(
                                "font-size:12px; color:var(--text-muted); margin-bottom:12px;"
                            )

                        if status == "ok":
                            ui.label("All systems operational").style("font-size:12px; color:#30d158;")
                        elif p.get("circuit_breaker_open"):
                            ui.label("Temporarily disabled").style("font-size:12px; color:#ff453a;")
                        elif p.get("failures", 0) > 0:
                            ui.label(f"{p['failures']} recent failures").style("font-size:12px; color:#ff9f0a;")
                        else:
                            ui.label("Status unknown").style("font-size:12px; color:var(--text-muted);")
                    else:
                        with ui.row().classes("items-center justify-center gap-1").style("margin-bottom:12px;"):
                            ui.icon("cancel", size="16px", color=DARK_TEXT_MUTED)
                            ui.label("Not Connected").style("font-size:13px; color:var(--text-muted); font-weight:500;")

                        descriptions = {
                            "youtube": "Upload shorts to YouTube. Requires OAuth 2.0 credentials.",
                            "instagram": "Post reels to Instagram. Requires session authentication.",
                            "x": "Post videos to X/Twitter. Requires browser cookies.",
                            "tiktok": "Source platform for content. Configure username only.",
                        }
                        ui.label(descriptions.get(p["name"], "Connect this platform to start posting.")).style(
                            "font-size:13px; color:var(--text-muted); margin-bottom:16px; line-height:1.5;"
                        )

                        btn_label = "Configure" if p["name"] == "tiktok" else "Connect"
                        ui.button(
                            btn_label,
                            icon="add_link",
                            on_click=lambda name=p["name"]: ui.notify(f"Run `xpst connect {name}` to set up", type="info"),
                        ).props(f'color="{color}" rounded').style("width:100%;")

        # Help
        with ui.element("div").classes("glass-card w-full").style("margin-top:24px;"):
            with ui.row().classes("items-center gap-3"):
                ui.icon("info", size="18px", color=ACCENT)
                ui.label("Setup Guide").style("font-size:15px; font-weight:600; color:var(--text);")

            ui.label("To connect a platform, run the following command in your terminal:").style(
                "font-size:13px; color:var(--text-sec); margin-top:12px;"
            )
            ui.html(
                '<code style="background:var(--sidebar-bg); padding:8px 14px; border-radius:6px; font-size:13px; color:#0a84ff; display:block; margin-top:8px;">xpst connect &lt;platform&gt;</code>'
            )
            ui.label("Supported platforms: youtube, x, instagram, tiktok").style(
                "font-size:12px; color:var(--text-muted); margin-top:8px;"
            )


# ── Page: Settings (/settings) ─────────────────────────────────────────

def _page_settings(collector: AnalyticsCollector) -> None:
    """Render the Settings page."""
    config_dir = Path(collector.config_dir).expanduser()
    config_path = config_dir / "config.yaml"

    with _page_shell("/settings"):
        _section_header("Settings", "Configure your XPST installation")

        # General
        with ui.element("div").classes("glass-card w-full").style("margin-bottom:20px;"):
            ui.label("General").classes("settings-section-title")

            accounts = collector.config.get("accounts", {})
            tk_username = accounts.get("tiktok", {}).get("username", "")

            with ui.column().classes("gap-4"):
                ui.input("TikTok Username", value=tk_username, placeholder="e.g. tys.ais").props('outlined').classes("w-full").style("max-width:400px;")
                ui.input("Download Directory", value=str(collector.config.get("video", {}).get("download_dir", "~/.xpst/downloads")), placeholder="Path to download directory").props('outlined').classes("w-full").style("max-width:500px;")

        # Platform Toggles
        with ui.element("div").classes("glass-card w-full").style("margin-bottom:20px;"):
            ui.label("Platforms").classes("settings-section-title")

            platform_configs = collector.config.get("accounts", {})

            for name, label, color in [
                ("youtube", "YouTube", PLATFORM_COLORS["youtube"]),
                ("instagram", "Instagram", PLATFORM_COLORS["instagram"]),
                ("x", "X / Twitter", PLATFORM_COLORS["x"]),
                ("tiktok", "TikTok", PLATFORM_COLORS["tiktok"]),
            ]:
                enabled = platform_configs.get(name, {}).get("enabled", False)
                if name == "tiktok":
                    enabled = bool(platform_configs.get(name, {}).get("username"))

                with ui.row().classes("items-center justify-between w-full").style("padding:10px 0; border-bottom:1px solid var(--border);"):
                    with ui.row().classes("items-center gap-3"):
                        with ui.element("div").style(
                            f"width:32px; height:32px; border-radius:8px; background:{color}15; display:flex; align-items:center; justify-content:center;"
                        ):
                            ui.icon(PLATFORM_ICONS[name], size="16px", color=color)
                        ui.label(label).style("font-size:14px; font-weight:500; color:var(--text);")
                    ui.switch(value=enabled).props(f'color="{color}"')

        # Notifications
        with ui.element("div").classes("glass-card w-full").style("margin-bottom:20px;"):
            ui.label("Notifications").classes("settings-section-title")

            notif_cfg = collector.config.get("notifications", {})
            enabled = notif_cfg.get("enabled", False)

            with ui.column().classes("gap-4"):
                ui.switch("Enable notifications", value=enabled).props(f'color="{ACCENT}"')

                with ui.element("div").classes("settings-form-row"):
                    discord_url = notif_cfg.get("discord", {}).get("webhook_url", "")
                    ui.input("Discord Webhook URL", value=discord_url, placeholder="https://discord.com/api/webhooks/...").props('outlined').classes("col-grow")

                    tg_token = notif_cfg.get("telegram", {}).get("bot_token", "")
                    ui.input("Telegram Bot Token", value=tg_token, placeholder="123456:ABC-DEF...").props('outlined').classes("col-grow")

        # Rate Limits
        with ui.element("div").classes("glass-card w-full").style("margin-bottom:20px;"):
            ui.label("Rate Limits").classes("settings-section-title")

            rate_limits_cfg = collector.config.get("rate_limits", {})

            with ui.element("div").classes("settings-form-row"):
                rl_yt = ui.number("YouTube", value=rate_limits_cfg.get("youtube", 5), min=1, max=50, format="%d").props('outlined').style("min-width:120px;")
                rl_ig = ui.number("Instagram", value=rate_limits_cfg.get("instagram", 5), min=1, max=50, format="%d").props('outlined').style("min-width:120px;")
                rl_x = ui.number("X / Twitter", value=rate_limits_cfg.get("x", 5), min=1, max=50, format="%d").props('outlined').style("min-width:120px;")
                rl_tt = ui.number("TikTok", value=rate_limits_cfg.get("tiktok", 5), min=1, max=50, format="%d").props('outlined').style("min-width:120px;")

        # Advanced
        with ui.element("div").classes("glass-card w-full").style("margin-bottom:20px;"):
            ui.label("Advanced").classes("settings-section-title")

            reliability = collector.config.get("reliability", {})
            schedule = collector.config.get("schedule", {})

            with ui.element("div").classes("settings-form-row"):
                ui.number("Max Retries", value=reliability.get("max_retries", 3), min=1, max=10).props('outlined').style("min-width:120px;")
                ui.number("Retry Backoff (s)", value=reliability.get("retry_backoff", 2), min=1, max=60).props('outlined').style("min-width:120px;")
                ui.number("Check Interval (s)", value=schedule.get("check_interval", 900), min=60, max=3600).props('outlined').style("min-width:120px;")

        # System Paths
        with ui.element("div").classes("glass-card w-full").style("margin-bottom:20px;"):
            ui.label("System Paths").classes("settings-section-title")

            paths_info = [
                ("Config directory", str(config_dir)),
                ("State file", str(config_dir / "state.json")),
                ("Config file", str(config_path)),
                ("Downloads", str(config_dir / "downloads")),
                ("Logs", str(config_dir / "logs")),
            ]
            for label, path in paths_info:
                with ui.row().classes("items-center gap-3 path-row").style("padding:6px 0;"):
                    ui.icon("folder", size="16px", color=DARK_TEXT_MUTED)
                    ui.label(f"{label}:").style("color:var(--text-sec); min-width:120px; font-size:clamp(0.75rem, 1vw, 0.8125rem);")
                    ui.label(path).classes("path-value").style("color:var(--text); font-family:monospace; font-size:clamp(0.7rem, 0.9vw, 0.75rem);")

        # Save Buttons
        with ui.row().classes("gap-3").style("flex-wrap:wrap;"):
            async def save_settings() -> None:
                try:
                    cfg = XPSTConfig.load(str(config_path))
                    cfg.rate_limits.youtube = int(rl_yt.value or 5)
                    cfg.rate_limits.instagram = int(rl_ig.value or 5)
                    cfg.rate_limits.x = int(rl_x.value or 5)
                    cfg.rate_limits.tiktok = int(rl_tt.value or 5)
                    cfg.save(str(config_path))
                    ui.notify("Settings saved successfully!", type="positive")
                except Exception as e:
                    ui.notify(f"Error saving settings: {e}", type="negative")

            ui.button("Save Settings", icon="save", on_click=save_settings).props(
                f'color="{ACCENT}" rounded'
            ).style("padding:10px 28px;")

            ui.button("Reset to Defaults", icon="refresh", on_click=lambda: ui.notify("Reset not yet implemented", type="warning")).props(
                'color="grey-7" rounded outline'
            ).style("padding:10px 28px;")


# ── Dashboard Factory ───────────────────────────────────────────────────

def create_dashboard(config_dir: str = "~/.xpst"):
    """Register all dashboard pages with NiceGUI."""
    collector = AnalyticsCollector(config_dir)

    @ui.page("/")
    def overview() -> None:
        _page_overview(collector)

    @ui.page("/content")
    def content() -> None:
        _page_content(collector)

    @ui.page("/analytics")
    def analytics() -> None:
        _page_analytics(collector)

    @ui.page("/connect")
    def connect() -> None:
        _page_connect(collector)

    @ui.page("/settings")
    def settings() -> None:
        _page_settings(collector)
