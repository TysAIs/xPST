"""
XPST Dashboard App — Premium YouTube Studio × Apple Store Hybrid

Dark theme with frosted glass effects, card-based layout, and clean typography.
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

# ── Design Tokens ──────────────────────────────────────────────────────
BG = "#0f0f0f"
SURFACE = "#1e1e1e"
SURFACE_HOVER = "#252525"
BORDER = "#2a2a2a"
ACCENT = "#3ea6ff"
TEXT_PRIMARY = "#ffffff"
TEXT_SECONDARY = "#aaaaaa"
TEXT_MUTED = "#666666"
SUCCESS = "#2ba640"
WARNING = "#f59e0b"
ERROR = "#ef4444"


# ── Premium CSS ────────────────────────────────────────────────────────

CUSTOM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');

* {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
}

:root {
    --q-dark: #0f0f0f !important;
    --q-dark-page: #0f0f0f !important;
    --glass-bg: rgba(30, 30, 30, 0.7);
    --glass-border: rgba(255, 255, 255, 0.06);
}

body, .q-page, .q-layout {
    background: #0f0f0f !important;
    color: #ffffff !important;
}

.q-drawer {
    background: #141414 !important;
    border-right: 1px solid rgba(255, 255, 255, 0.04) !important;
}

/* ── Sidebar ─────────────────────────────────────────── */
.sidebar-container {
    padding: 24px 16px;
    height: 100%;
    display: flex;
    flex-direction: column;
}

.sidebar-logo {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 40px;
    padding: 0 8px;
}

.sidebar-logo-text {
    font-size: 1.3rem;
    font-weight: 800;
    color: #ffffff;
    letter-spacing: -0.03em;
}

.nav-item {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 12px 16px;
    border-radius: 12px;
    color: #aaaaaa;
    text-decoration: none;
    font-size: 0.9rem;
    font-weight: 500;
    transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
    cursor: pointer;
    margin-bottom: 4px;
}

.nav-item:hover {
    background: rgba(255, 255, 255, 0.06);
    color: #ffffff;
}

.nav-item.active {
    background: rgba(62, 166, 255, 0.12);
    color: #3ea6ff;
}

.nav-item.active .q-icon {
    color: #3ea6ff;
}

/* ── Metric Cards ───────────────────────────────────── */
.metric-card {
    background: #1e1e1e;
    border-radius: 16px;
    padding: 24px;
    position: relative;
    overflow: hidden;
    border: 1px solid rgba(255, 255, 255, 0.04);
    transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
    flex: 1;
    min-width: 200px;
}

.metric-card:hover {
    border-color: rgba(62, 166, 255, 0.15);
    transform: translateY(-2px);
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
}

.metric-card::before {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 2px;
    background: var(--accent-color, #3ea6ff);
    opacity: 0.6;
}

.metric-value {
    font-size: 2rem;
    font-weight: 700;
    color: #ffffff;
    line-height: 1.1;
    letter-spacing: -0.02em;
}

.metric-label {
    font-size: 0.8rem;
    color: #aaaaaa;
    margin-top: 6px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-weight: 500;
}

.metric-icon {
    width: 40px;
    height: 40px;
    border-radius: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    margin-bottom: 16px;
}

/* ── Content Cards ───────────────────────────────────── */
.content-card {
    background: #1e1e1e;
    border-radius: 16px;
    border: 1px solid rgba(255, 255, 255, 0.04);
    overflow: hidden;
    transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
}

.content-card:hover {
    border-color: rgba(255, 255, 255, 0.08);
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.2);
}

.glass-card {
    background: rgba(30, 30, 30, 0.6);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    border-radius: 16px;
    border: 1px solid rgba(255, 255, 255, 0.06);
    padding: 24px;
}

/* ── Platform Badges ─────────────────────────────────── */
.badge {
    display: inline-flex;
    align-items: center;
    padding: 3px 10px;
    border-radius: 6px;
    font-size: 0.7rem;
    font-weight: 600;
    color: #ffffff;
    letter-spacing: 0.03em;
}

.badge-sm {
    padding: 2px 8px;
    font-size: 0.65rem;
    border-radius: 4px;
}

/* ── Status Indicators ───────────────────────────────── */
.status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    display: inline-block;
    margin-right: 6px;
}

.status-posted { background: #2ba640; box-shadow: 0 0 8px rgba(43, 166, 64, 0.4); }
.status-pending { background: #f59e0b; box-shadow: 0 0 8px rgba(245, 158, 11, 0.4); }
.status-failed { background: #ef4444; box-shadow: 0 0 8px rgba(239, 68, 68, 0.4); }
.status-unknown { background: #666666; }

/* ── Section Headers ─────────────────────────────────── */
.section-header {
    font-size: 1.1rem;
    font-weight: 600;
    color: #ffffff;
    margin-bottom: 20px;
    letter-spacing: -0.01em;
}

.page-title {
    font-size: 1.6rem;
    font-weight: 700;
    color: #ffffff;
    margin-bottom: 8px;
    letter-spacing: -0.02em;
}

.page-subtitle {
    font-size: 0.9rem;
    color: #aaaaaa;
    margin-bottom: 32px;
}

/* ── Tab Styling ─────────────────────────────────────── */
.platform-tab {
    padding: 8px 20px;
    border-radius: 10px;
    font-size: 0.85rem;
    font-weight: 500;
    color: #aaaaaa;
    cursor: pointer;
    transition: all 0.2s;
    border: 1px solid transparent;
}

.platform-tab:hover {
    color: #ffffff;
    background: rgba(255, 255, 255, 0.04);
}

.platform-tab.active {
    color: #ffffff;
    background: rgba(62, 166, 255, 0.12);
    border-color: rgba(62, 166, 255, 0.2);
}

/* ── Thumbnail Placeholders ──────────────────────────── */
.thumb-placeholder {
    width: 100%;
    aspect-ratio: 16/9;
    border-radius: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 2rem;
    position: relative;
    overflow: hidden;
}

.thumb-placeholder::after {
    content: '';
    position: absolute;
    inset: 0;
    background: linear-gradient(135deg, rgba(255,255,255,0.1) 0%, transparent 60%);
}

/* ── Connect Cards ───────────────────────────────────── */
.connect-card {
    background: #1e1e1e;
    border-radius: 20px;
    border: 1px solid rgba(255, 255, 255, 0.04);
    padding: 32px;
    text-align: center;
    transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    position: relative;
    overflow: hidden;
}

.connect-card:hover {
    transform: translateY(-4px);
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.4);
    border-color: rgba(255, 255, 255, 0.08);
}

.connect-card::before {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 3px;
    background: var(--platform-color, #3ea6ff);
}

/* ── Filter Pills ────────────────────────────────────── */
.filter-pill {
    padding: 6px 16px;
    border-radius: 20px;
    font-size: 0.8rem;
    font-weight: 500;
    color: #aaaaaa;
    cursor: pointer;
    transition: all 0.2s;
    border: 1px solid rgba(255, 255, 255, 0.08);
    background: transparent;
}

.filter-pill:hover {
    color: #ffffff;
    border-color: rgba(255, 255, 255, 0.15);
}

.filter-pill.active {
    color: #ffffff;
    background: #3ea6ff;
    border-color: #3ea6ff;
}

/* ── Settings Form ───────────────────────────────────── */
.settings-section {
    margin-bottom: 32px;
}

.settings-section-title {
    font-size: 0.75rem;
    font-weight: 600;
    color: #aaaaaa;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 16px;
    padding-bottom: 12px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}

/* ── Toast Overrides ─────────────────────────────────── */
.q-notification {
    border-radius: 12px !important;
    animation: slideIn 0.3s ease-out;
}

/* ── Scrollbar ───────────────────────────────────────── */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.2); }

/* ── Plotly Overrides ────────────────────────────────── */
.js-plotly-plot .plotly { background: transparent !important; }

/* ── Animations ──────────────────────────────────────── */
.fade-in { animation: fadeIn 0.3s ease-in; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
.card-hover { transition: transform 0.2s, box-shadow 0.2s; }
.card-hover:hover { transform: scale(1.02); box-shadow: 0 8px 32px rgba(0,0,0,0.3); }
.btn-transition { transition: background-color 0.2s, transform 0.1s; }
.btn-transition:hover { transform: scale(1.05); }
.pulse { animation: pulse 2s infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
.slide-in { animation: slideIn 0.3s ease-out; }
@keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
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
    """Map health status string to a hex color code."""
    return {"ok": SUCCESS, "degraded": WARNING, "error": ERROR, "unknown": TEXT_MUTED}.get(status, TEXT_MUTED)


def _status_label(status: str) -> str:
    """Map health status string to a human-readable label."""
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
            ui.icon("hub", size="28px", color=ACCENT)
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

        # Footer
        with ui.column().style("padding:0 8px;"):
            ui.label("XPST v1.0").style(f"font-size:0.7rem; color:{TEXT_MUTED};")
            ui.label("Powered by NiceGUI").style(f"font-size:0.65rem; color:{TEXT_MUTED};")


# ── Page Shell ──────────────────────────────────────────────────────────

def _page_shell(current: str = "/"):
    """Build sidebar + main content layout."""
    ui.add_head_html(CUSTOM_CSS)
    # Material Icons
    ui.add_head_html('<link href="https://fonts.googleapis.com/icon?family=Material+Icons|Material+Icons+Outlined" rel="stylesheet">')

    with ui.row().classes("w-full no-wrap").style("min-height:100vh;"):
        with ui.column().style("width:240px; min-width:240px; background:#141414; min-height:100vh; border-right:1px solid rgba(255,255,255,0.04);"):
            _sidebar(current)
        with ui.column().classes("col-grow fade-in").style(f"background:{BG}; min-height:100vh;") as main:
            with ui.column().classes("w-full").style("padding:32px 40px; max-width:1400px;"):
                pass  # caller appends inside this
    return main


# ── Metric Card Component ──────────────────────────────────────────────

def _metric_card(icon: str, value: str, label: str, color: str = ACCENT, accent_color: str = ACCENT) -> None:
    """Render a metric card component with icon, value, and label."""

    with ui.element("div").classes("metric-card").style(f"--accent-color:{accent_color};"):
        with ui.element("div").classes("metric-icon").style(f"background:{accent_color}20;"):
            ui.icon(icon, size="22px", color=accent_color)
        ui.label(value).classes("metric-value")
        ui.label(label).classes("metric-label")


def _section_header(title: str, subtitle: str = "") -> None:
    """Render a page section header with optional subtitle."""

    ui.label(title).classes("page-title")
    if subtitle:
        ui.label(subtitle).classes("page-subtitle")


# ── Page: Overview (/) ──────────────────────────────────────────────────

def _page_overview(collector: AnalyticsCollector) -> None:
    """Render the Overview dashboard page with metrics, charts, and health status."""

    import plotly.graph_objects as go

    stats = collector.get_summary_stats()
    posts = collector.get_all_posts()
    health = collector.get_platform_health_all()

    with _page_shell("/"):
        _section_header("Dashboard", "Your cross-platform content at a glance")

        # ── Metric Cards ────────────────────────────────────────────────
        with ui.row().classes("gap-4 w-full").style("margin-bottom:28px;"):
            _metric_card(
                "video_library",
                str(stats["total_posts"]),
                "Total Posts",
                ACCENT,
                ACCENT,
            )
            _metric_card(
                "visibility",
                _fmt_num(stats["total_platform_posts"]),
                "Total Reach",
                ACCENT,
                "#a855f7",
            )
            best = stats.get("best_platform")
            best_label = PLATFORM_LABELS.get(best, "—") if best else "—"
            best_color = PLATFORM_COLORS.get(best, TEXT_MUTED) if best else TEXT_MUTED
            _metric_card("emoji_events", best_label, "Best Platform", best_color, best_color)
            _metric_card(
                "schedule",
                str(stats.get("posts_this_week", 0)),
                "Posts This Week",
                ACCENT,
                SUCCESS,
            )

        # ── Charts Row ──────────────────────────────────────────────────
        with ui.row().classes("gap-4 w-full").style("margin-bottom:28px;"):
            # Line chart — Posts over time
            with ui.element("div").classes("glass-card card-hover col-grow"):
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
                    line=dict(color=ACCENT, width=2.5, shape="spline"),
                    marker=dict(size=6, color=ACCENT, line=dict(width=0)),
                    fill="tozeroy",
                    fillcolor="rgba(62, 166, 255, 0.08)",
                    hovertemplate="%{x}<br>%{y} posts<extra></extra>",
                ))
                fig.update_layout(
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(color=TEXT_PRIMARY, family="Inter"),
                    xaxis=dict(gridcolor="rgba(255,255,255,0.04)", zeroline=False, showgrid=False),
                    yaxis=dict(gridcolor="rgba(255,255,255,0.04)", zeroline=False, dtick=1, showgrid=True),
                    margin=dict(t=10, b=30, l=40, r=10),
                    height=260,
                    hovermode="x unified",
                )
                ui.plotly(fig).classes("w-full")

            # Pie chart — Platform breakdown
            with ui.element("div").classes("glass-card card-hover").style("width:340px; min-width:280px;"):
                ui.label("By Platform").classes("section-header")

                pc = stats["platform_counts"]
                labels = [PLATFORM_LABELS[k] for k, v in pc.items() if v > 0]
                values = [v for v in pc.values() if v > 0]
                colors = [PLATFORM_COLORS[k] for k, v in pc.items() if v > 0]

                if not values:
                    labels = ["No posts yet"]
                    values = [1]
                    colors = [BORDER]

                fig2 = go.Figure(data=[go.Pie(
                    labels=labels, values=values,
                    hole=0.6,
                    marker=dict(colors=colors, line=dict(color=SURFACE, width=2)),
                    textfont=dict(color="white", size=11, family="Inter"),
                    hovertemplate="%{label}: %{value}<extra></extra>",
                    sort=False,
                )])
                fig2.update_layout(
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(color=TEXT_PRIMARY, family="Inter"),
                    showlegend=True,
                    legend=dict(
                        font=dict(color=TEXT_SECONDARY, size=11),
                        orientation="h",
                        yanchor="bottom", y=-0.15,
                        xanchor="center", x=0.5,
                    ),
                    margin=dict(t=10, b=40, l=10, r=10),
                    height=260,
                )
                ui.plotly(fig2).classes("w-full")

        # ── Recent Posts Grid ───────────────────────────────────────────
        with ui.element("div").classes("glass-card card-hover w-full").style("margin-bottom:28px;"):
            ui.label("Recent Posts").classes("section-header")

            if posts:
                with ui.row().classes("gap-3 w-full").style("flex-wrap:wrap;"):
                    for p in posts[:6]:
                        plats = list(p.get("platforms", {}).keys())
                        main_plat = plats[0] if plats else "youtube"
                        plat_color = PLATFORM_COLORS.get(main_plat, ACCENT)

                        with ui.element("div").classes("content-card").style("width:calc(33.333% - 12px); min-width:250px; cursor:pointer;"):
                            # Thumbnail
                            with ui.element("div").classes("thumb-placeholder").style(
                                f"background: linear-gradient(135deg, {plat_color}40 0%, {plat_color}15 100%);"
                            ):
                                ui.icon(PLATFORM_ICONS.get(main_plat, "video_library"), size="36px", color=plat_color)

                            with ui.column().style("padding:16px;"):
                                # Caption
                                caption = (p.get("caption") or "")[:80]
                                if len(p.get("caption") or "") > 80:
                                    caption += "…"
                                ui.label(caption).style(
                                    f"font-size:0.85rem; font-weight:500; color:{TEXT_PRIMARY}; line-height:1.4; margin-bottom:8px;"
                                )

                                # Platform badges + date
                                with ui.row().classes("items-center gap-2"):
                                    for plat in plats:
                                        p_color = PLATFORM_COLORS.get(plat, "#888")
                                        p_label = PLATFORM_BADGE_LABELS.get(plat, plat[:2].upper())
                                        ui.html(
                                            f'<span class="badge badge-sm" style="background:{p_color};">{p_label}</span>'
                                        )
                                    ui.label(_relative_time(p.get("downloaded_at"))).style(
                                        f"font-size:0.7rem; color:{TEXT_MUTED}; margin-left:auto;"
                                    )
            else:
                with ui.column().classes("items-center w-full").style("padding:40px 0;"):
                    ui.icon("video_library", size="48px", color=TEXT_MUTED)
                    ui.label("No posts yet").style(f"font-size:1rem; color:{TEXT_SECONDARY}; margin-top:12px;")
                    ui.label("Run `xpst run` to get started").style(f"font-size:0.85rem; color:{TEXT_MUTED};")

        # ── Platform Health ─────────────────────────────────────────────
        with ui.element("div").classes("glass-card card-hover w-full"):
            ui.label("Platform Health").classes("section-header")

            with ui.row().classes("gap-3 w-full").style("flex-wrap:wrap;"):
                for p in health:
                    color = p["color"]
                    status = p["status"]
                    configured = p["configured"]
                    failures = p["failures"]
                    cb = p["circuit_breaker_open"]

                    if not configured:
                        dot_cls = "status-unknown"
                        status_text = "Not Configured"
                        status_c = TEXT_MUTED
                    elif cb:
                        dot_cls = "status-failed"
                        status_text = "Circuit Breaker Open"
                        status_c = ERROR
                    elif failures > 0:
                        dot_cls = "status-pending"
                        status_text = f"Degraded ({failures} failures)"
                        status_c = WARNING
                    elif status == "ok":
                        dot_cls = "status-posted pulse"
                        status_text = "Healthy"
                        status_c = SUCCESS
                    else:
                        dot_cls = "status-unknown"
                        status_text = "Unknown"
                        status_c = TEXT_MUTED

                    with ui.element("div").classes("content-card").style("flex:1; min-width:200px; padding:20px;"):
                        with ui.row().classes("items-center gap-3"):
                            with ui.element("div").style(
                                f"width:36px; height:36px; border-radius:10px; background:{color}15; display:flex; align-items:center; justify-content:center;"
                            ):
                                ui.icon(p["icon"], size="20px", color=color)
                            with ui.column().classes("gap-0"):
                                ui.label(p["label"]).style(f"font-size:0.95rem; font-weight:600; color:{TEXT_PRIMARY};")
                                with ui.row().classes("items-center gap-1"):
                                    ui.html(f'<span class="status-dot {dot_cls}"></span>')
                                    ui.label(status_text).style(f"font-size:0.75rem; color:{status_c};")


# ── Page: Content Library (/content) ───────────────────────────────────

def _page_content(collector: AnalyticsCollector) -> None:
    """Render the Content Library page with post listing and filters."""

    posts = collector.get_all_posts()

    with _page_shell("/content"):
        _section_header("Content Library", "Browse and manage all your cross-posted content")

        # ── Search & Filters ────────────────────────────────────────────
        with ui.element("div").classes("glass-card card-hover w-full").style("margin-bottom:24px; padding:16px 24px;"):
            with ui.row().classes("items-center gap-4 w-full"):
                ui.input(placeholder="Search posts...").props(
                    'outlined dense bg-color=transparent'
                ).classes("col-grow").style("max-width:400px;")

                with ui.row().classes("gap-2"):
                    ui.button("All", on_click=lambda: _filter_posts("all")).classes("filter-pill active")
                    ui.button("YouTube", on_click=lambda: _filter_posts("youtube")).classes("filter-pill")
                    ui.button("Instagram", on_click=lambda: _filter_posts("instagram")).classes("filter-pill")
                    ui.button("X", on_click=lambda: _filter_posts("x")).classes("filter-pill")
                    ui.button("TikTok", on_click=lambda: _filter_posts("tiktok")).classes("filter-pill")

        # ── Posts Grid ──────────────────────────────────────────────────
        posts_container = ui.column().classes("w-full")

        def _filter_posts(platform: str) -> None:
            """Re-render posts list filtered by platform."""
            # Re-render posts with filter
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
                ui.icon("video_library", size="56px", color=TEXT_MUTED)
                ui.label("No content found").style(f"font-size:1.1rem; color:{TEXT_SECONDARY}; margin-top:16px;")
                ui.label("Your cross-posted content will appear here").style(f"font-size:0.85rem; color:{TEXT_MUTED};")
            return

        with ui.row().classes("gap-4 w-full").style("flex-wrap:wrap;"):
            for p in posts:
                plats = list(p.get("platforms", {}).keys())
                main_plat = plats[0] if plats else "youtube"
                plat_color = PLATFORM_COLORS.get(main_plat, ACCENT)
                status = p.get("status", "posted")

                with ui.element("div").classes("content-card").style("width:calc(25% - 12px); min-width:240px;"):
                    # Thumbnail
                    with ui.element("div").classes("thumb-placeholder").style(
                        f"background: linear-gradient(135deg, {plat_color}35 0%, {plat_color}10 100%);"
                    ):
                        ui.icon(PLATFORM_ICONS.get(main_plat, "movie"), size="40px", color=plat_color)

                        # Status badge
                        if status == "posted":
                            status_html = '<span class="badge" style="position:absolute; top:12px; right:12px; background:rgba(43,166,64,0.9); font-size:0.65rem; z-index:1;">✓ POSTED</span>'
                        elif status == "pending":
                            status_html = '<span class="badge" style="position:absolute; top:12px; right:12px; background:rgba(245,158,11,0.9); font-size:0.65rem; z-index:1;">⏳ PENDING</span>'
                        else:
                            status_html = '<span class="badge" style="position:absolute; top:12px; right:12px; background:rgba(239,68,68,0.9); font-size:0.65rem; z-index:1;">✗ FAILED</span>'
                        ui.html(status_html)

                    with ui.column().style("padding:16px;"):
                        # Caption
                        caption = (p.get("caption") or "")[:100]
                        if len(p.get("caption") or "") > 100:
                            caption += "…"
                        ui.label(caption).style(
                            f"font-size:0.85rem; font-weight:500; color:{TEXT_PRIMARY}; line-height:1.4; margin-bottom:8px; min-height:40px;"
                        )

                        # Platform badges
                        with ui.row().classes("items-center gap-1").style("margin-bottom:8px;"):
                            for plat in plats:
                                p_color = PLATFORM_COLORS.get(plat, "#888")
                                p_label = PLATFORM_BADGE_LABELS.get(plat, plat[:2].upper())
                                url = p.get("platforms", {}).get(plat, {}).get("url", "")
                                if url:
                                    ui.html(
                                        f'<a href="{url}" target="_blank"><span class="badge badge-sm" style="background:{p_color};">{p_label}</span></a>'
                                    )
                                else:
                                    ui.html(
                                        f'<span class="badge badge-sm" style="background:{p_color};">{p_label}</span>'
                                    )

                        # Date & video ID
                        with ui.row().classes("items-center justify-between w-full"):
                            ui.label(_relative_time(p.get("downloaded_at"))).style(
                                f"font-size:0.7rem; color:{TEXT_MUTED};"
                            )
                            ui.label(p["video_id"][:12] + "…").style(
                                f"font-size:0.65rem; color:{TEXT_MUTED}; font-family:monospace;"
                            )


# ── Page: Analytics (/analytics) ───────────────────────────────────────

def _page_analytics(collector: AnalyticsCollector) -> None:
    """Render the Analytics page with engagement charts and metrics."""

    with _page_shell("/analytics"):
        _section_header("Analytics", "Deep dive into your content performance")

        # ── Platform Tabs ───────────────────────────────────────────────
        with ui.row().classes("gap-2 w-full").style("margin-bottom:28px;"):
            ui.button("All", on_click=lambda: _show_analytics("all")).classes("platform-tab active")
            ui.button("YouTube", on_click=lambda: _show_analytics("youtube")).classes("platform-tab")
            ui.button("Instagram", on_click=lambda: _show_analytics("instagram")).classes("platform-tab")
            ui.button("X / Twitter", on_click=lambda: _show_analytics("x")).classes("platform-tab")
            ui.button("TikTok", on_click=lambda: _show_analytics("tiktok")).classes("platform-tab")

        analytics_container = ui.column().classes("w-full")

        def _show_analytics(platform: str) -> None:
            """Re-render analytics content for the selected platform filter."""
            analytics_container.clear()
            _render_analytics_content(analytics_container, collector, platform)

        _render_analytics_content(analytics_container, collector, "all")


def _render_analytics_content(container, collector: AnalyticsCollector, platform: str) -> None:
    """Render analytics charts and metric cards inside the given container."""
    import plotly.graph_objects as go

    stats = collector.get_summary_stats()
    posts = collector.get_all_posts()
    engagement = collector.get_engagement_data()
    top_posts = collector.get_top_posts(5)

    with container:
        # ── Metrics Cards ───────────────────────────────────────────────
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

        with ui.row().classes("gap-4 w-full").style("margin-bottom:28px;"):
            _metric_card("visibility", _fmt_num(total_views), "Total Views", ACCENT, ACCENT)
            _metric_card("favorite", _fmt_num(total_likes), "Total Likes", ACCENT, "#ef4444")
            _metric_card("comment", _fmt_num(total_comments), "Comments", ACCENT, "#a855f7")
            _metric_card("share", _fmt_num(total_shares), "Shares", ACCENT, "#22c55e")

        with ui.row().classes("gap-4 w-full").style("margin-bottom:28px;"):
            # ── Engagement Over Time ─────────────────────────────────────
            with ui.element("div").classes("glass-card card-hover col-grow"):
                ui.label("Engagement Over Time").classes("section-header")

                posts_by_date = collector.get_posts_over_time(30)
                dates = list(posts_by_date.keys()) if posts_by_date else []
                counts = list(posts_by_date.values()) if posts_by_date else []

                if platform != "all":
                    # Filter by platform
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
                    line=dict(color=line_color, width=2.5, shape="spline"),
                    marker=dict(size=5, color=line_color),
                    fill="tozeroy",
                    fillcolor=f"rgba({int(line_color[1:3],16)},{int(line_color[3:5],16)},{int(line_color[5:7],16)},0.08)",
                    hovertemplate="%{x}<br>%{y} posts<extra></extra>",
                ))
                fig.update_layout(
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(color=TEXT_PRIMARY, family="Inter"),
                    xaxis=dict(gridcolor="rgba(255,255,255,0.04)", zeroline=False, showgrid=False),
                    yaxis=dict(gridcolor="rgba(255,255,255,0.04)", zeroline=False, dtick=1, showgrid=True),
                    margin=dict(t=10, b=30, l=40, r=10),
                    height=280,
                    hovermode="x unified",
                )
                ui.plotly(fig).classes("w-full")

            # ── Platform Comparison ──────────────────────────────────────
            with ui.element("div").classes("glass-card card-hover").style("width:340px; min-width:280px;"):
                ui.label("Platform Comparison").classes("section-header")

                pc = stats["platform_counts"]
                platforms_list = list(pc.keys())
                counts_list = list(pc.values())
                colors_list = [PLATFORM_COLORS.get(p, "#888") for p in platforms_list]
                labels_list = [PLATFORM_LABELS.get(p, p) for p in platforms_list]

                fig2 = go.Figure(data=[go.Bar(
                    x=labels_list, y=counts_list,
                    marker_color=colors_list,
                    marker=dict(
                        line=dict(width=0),
                        cornerradius=8,
                    ),
                    text=counts_list,
                    textposition="outside",
                    textfont=dict(color=TEXT_PRIMARY, size=12, family="Inter"),
                    hovertemplate="%{x}: %{y} posts<extra></extra>",
                )])
                fig2.update_layout(
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(color=TEXT_PRIMARY, family="Inter"),
                    xaxis=dict(gridcolor="rgba(255,255,255,0.04)", showgrid=False),
                    yaxis=dict(gridcolor="rgba(255,255,255,0.04)", zeroline=False, showgrid=True),
                    margin=dict(t=20, b=40, l=40, r=10),
                    height=280,
                )
                ui.plotly(fig2).classes("w-full")

        with ui.row().classes("gap-4 w-full").style("margin-bottom:28px;"):
            # ── Best Posting Times Heatmap ───────────────────────────────
            with ui.element("div").classes("glass-card card-hover col-grow"):
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
                        colorscale=[[0, "#1a1a1a"], [0.3, "#1e3a5f"], [0.6, "#3ea6ff"], [1, "#60c0ff"]],
                        hovertemplate="%{y} %{x}: %{z} posts<extra></extra>",
                        showscale=False,
                    ))
                    fig_heat.update_layout(
                        paper_bgcolor="rgba(0,0,0,0)",
                        plot_bgcolor="rgba(0,0,0,0)",
                        font=dict(color=TEXT_SECONDARY, family="Inter", size=11),
                        margin=dict(t=10, b=30, l=50, r=10),
                        height=280,
                    )
                    ui.plotly(fig_heat).classes("w-full")
                else:
                    with ui.column().classes("items-center w-full").style("padding:40px 0;"):
                        ui.label("Not enough data for heatmap").style(f"color:{TEXT_MUTED};")

            # ── Top Posts ────────────────────────────────────────────────
            with ui.element("div").classes("glass-card card-hover").style("width:400px; min-width:300px;"):
                ui.label("Top Posts by Reach").classes("section-header")

                for i, p in enumerate(top_posts):
                    plats = list(p.get("platforms", {}).keys())
                    main_plat = plats[0] if plats else "youtube"
                    plat_color = PLATFORM_COLORS.get(main_plat, ACCENT)

                    with ui.row().classes("items-center gap-3 w-full").style("padding:10px 0; border-bottom:1px solid rgba(255,255,255,0.04);"):
                        # Rank
                        ui.label(f"#{i+1}").style(
                            f"font-size:0.8rem; font-weight:700; color:{TEXT_MUTED}; min-width:24px;"
                        )

                        # Mini thumbnail
                        with ui.element("div").style(
                            f"width:48px; height:32px; border-radius:6px; background:linear-gradient(135deg, {plat_color}30, {plat_color}10); display:flex; align-items:center; justify-content:center;"
                        ):
                            ui.icon(PLATFORM_ICONS.get(main_plat, "movie"), size="16px", color=plat_color)

                        with ui.column().classes("gap-0 col-grow"):
                            caption = (p.get("caption") or "")[:45]
                            if len(p.get("caption") or "") > 45:
                                caption += "…"
                            ui.label(caption).style(f"font-size:0.8rem; font-weight:500; color:{TEXT_PRIMARY};")
                            with ui.row().classes("items-center gap-1"):
                                for plat in plats:
                                    ui.html(
                                        f'<span class="badge badge-sm" style="background:{PLATFORM_COLORS.get(plat, "#888")}; font-size:0.55rem;">{PLATFORM_BADGE_LABELS.get(plat, plat[:2])}</span>'
                                    )

                        # Platform count
                        ui.label(f"{len(plats)}").style(
                            f"font-size:1rem; font-weight:700; color:{ACCENT};"
                        )


# ── Page: Connect (/connect) ───────────────────────────────────────────

def _page_connect(collector: AnalyticsCollector) -> None:
    """Render the Connect page with platform connection cards and status."""

    health = collector.get_platform_health_all()

    with _page_shell("/connect"):
        _section_header("Connect Accounts", "Link your social platforms to start cross-posting")

        # ── Progress Indicator ──────────────────────────────────────────
        connected_count = sum(1 for p in health if p["configured"])
        total_platforms = len(health)

        with ui.element("div").classes("glass-card card-hover w-full").style("margin-bottom:32px; padding:20px 28px;"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label(f"{connected_count} of {total_platforms} platforms connected").style(
                    f"font-size:0.9rem; color:{TEXT_SECONDARY};"
                )
                # Progress bar
                with ui.element("div").style(f"width:200px; height:6px; background:{BORDER}; border-radius:3px; overflow:hidden;"):
                    pct = (connected_count / total_platforms * 100) if total_platforms > 0 else 0
                    ui.element("div").style(f"width:{pct}%; height:100%; background:{ACCENT}; border-radius:3px; transition:width 0.5s ease;")

        # ── Platform Cards ──────────────────────────────────────────────
        with ui.row().classes("gap-4 w-full").style("flex-wrap:wrap;"):
            for p in health:
                color = p["color"]
                configured = p["configured"]
                status = p["status"]
                icon = p["icon"]

                with ui.element("div").classes("connect-card").style(f"--platform-color:{color}; width:calc(25% - 12px); min-width:250px;"):
                    # Platform icon
                    with ui.element("div").style(
                        f"width:56px; height:56px; border-radius:16px; background:{color}15; display:flex; align-items:center; justify-content:center; margin:0 auto 20px;"
                    ):
                        ui.icon(icon, size="28px", color=color)

                    # Platform name
                    ui.label(p["label"]).style(
                        f"font-size:1.1rem; font-weight:700; color:{TEXT_PRIMARY}; margin-bottom:8px;"
                    )

                    # Status
                    if configured:
                        with ui.row().classes("items-center justify-center gap-1").style("margin-bottom:16px;"):
                            ui.icon("check_circle", size="16px", color=SUCCESS)
                            ui.label("Connected").style(f"font-size:0.8rem; color:{SUCCESS}; font-weight:500;")

                        # Account info
                        if p["name"] == "tiktok":
                            username = collector.config.get("accounts", {}).get("tiktok", {}).get("username", "")
                            if username:
                                ui.label(f"@{username}").style(f"font-size:0.8rem; color:{TEXT_MUTED}; margin-bottom:16px;")
                        else:
                            ui.label(f"Last active: {_relative_time(p.get('last_success'))}").style(
                                f"font-size:0.75rem; color:{TEXT_MUTED}; margin-bottom:16px;"
                            )

                        # Status detail
                        if status == "ok":
                            ui.label("All systems operational").style(f"font-size:0.75rem; color:{SUCCESS};")
                        elif p.get("circuit_breaker_open"):
                            ui.label("Temporarily disabled").style(f"font-size:0.75rem; color:{ERROR};")
                        elif p.get("failures", 0) > 0:
                            ui.label(f"{p['failures']} recent failures").style(f"font-size:0.75rem; color:{WARNING};")
                        else:
                            ui.label("Status unknown").style(f"font-size:0.75rem; color:{TEXT_MUTED};")
                    else:
                        with ui.row().classes("items-center justify-center gap-1").style("margin-bottom:16px;"):
                            ui.icon("cancel", size="16px", color=TEXT_MUTED)
                            ui.label("Not Connected").style(f"font-size:0.8rem; color:{TEXT_MUTED}; font-weight:500;")

                        # Description
                        descriptions = {
                            "youtube": "Upload shorts to YouTube. Requires OAuth 2.0 credentials.",
                            "instagram": "Post reels to Instagram. Requires session authentication.",
                            "x": "Post videos to X/Twitter. Requires browser cookies.",
                            "tiktok": "Source platform for content. Configure username only.",
                        }
                        ui.label(descriptions.get(p["name"], "Connect this platform to start posting.")).style(
                            f"font-size:0.8rem; color:{TEXT_MUTED}; margin-bottom:20px; line-height:1.5;"
                        )

                        # Connect button
                        btn_label = "Configure" if p["name"] == "tiktok" else "Connect"

                        ui.button(
                            btn_label,
                            icon="add_link",
                            on_click=lambda name=p["name"]: ui.notify(f"Run `xpst connect {name}` to set up", type="info"),
                        ).props(f'color="{color}" rounded').classes("btn-transition").style("width:100%;")

        # ── Help Text ───────────────────────────────────────────────────
        with ui.element("div").classes("glass-card card-hover w-full").style("margin-top:32px;"):
            with ui.row().classes("items-center gap-3"):
                ui.icon("info", size="20px", color=ACCENT)
                ui.label("Setup Guide").style(f"font-size:0.95rem; font-weight:600; color:{TEXT_PRIMARY};")

            ui.label(
                "To connect a platform, run the following command in your terminal:"
            ).style(f"font-size:0.85rem; color:{TEXT_SECONDARY}; margin-top:12px;")

            ui.html(
                '<code style="background:#252525; padding:8px 16px; border-radius:8px; font-size:0.85rem; color:#3ea6ff; display:block; margin-top:8px;">xpst connect &lt;platform&gt;</code>'
            )

            ui.label("Supported platforms: youtube, x, instagram, tiktok").style(
                f"font-size:0.8rem; color:{TEXT_MUTED}; margin-top:8px;"
            )


# ── Page: Settings (/settings) ─────────────────────────────────────────

def _page_settings(collector: AnalyticsCollector) -> None:
    """Render the Settings page with config editing and save functionality."""

    config_dir = Path(collector.config_dir).expanduser()
    config_path = config_dir / "config.yaml"

    with _page_shell("/settings"):
        _section_header("Settings", "Configure your XPST installation")

        # ── General Section ─────────────────────────────────────────────
        with ui.element("div").classes("glass-card card-hover w-full").style("margin-bottom:24px;"):
            ui.label("General").classes("settings-section-title")

            accounts = collector.config.get("accounts", {})
            tk_username = accounts.get("tiktok", {}).get("username", "")

            with ui.column().classes("gap-4"):
                ui.input(
                    "TikTok Username",
                    value=tk_username,
                    placeholder="e.g. tys.ais",
                ).props('outlined').classes("w-full").style("max-width:400px;")

                ui.input(
                    "Download Directory",
                    value=str(collector.config.get("video", {}).get("download_dir", "~/.xpst/downloads")),
                    placeholder="Path to download directory",
                ).props('outlined').classes("w-full").style("max-width:500px;")

        # ── Platform Toggles ────────────────────────────────────────────
        with ui.element("div").classes("glass-card card-hover w-full").style("margin-bottom:24px;"):
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

                with ui.row().classes("items-center justify-between w-full").style("padding:12px 0; border-bottom:1px solid rgba(255,255,255,0.04);"):
                    with ui.row().classes("items-center gap-3"):
                        with ui.element("div").style(
                            f"width:32px; height:32px; border-radius:8px; background:{color}15; display:flex; align-items:center; justify-content:center;"
                        ):
                            ui.icon(PLATFORM_ICONS[name], size="18px", color=color)
                        ui.label(label).style(f"font-size:0.9rem; font-weight:500; color:{TEXT_PRIMARY};")

                    ui.switch(value=enabled).props(f'color="{color}"')

        # ── Notifications ───────────────────────────────────────────────
        with ui.element("div").classes("glass-card card-hover w-full").style("margin-bottom:24px;"):
            ui.label("Notifications").classes("settings-section-title")

            notif_cfg = collector.config.get("notifications", {})
            enabled = notif_cfg.get("enabled", False)

            with ui.column().classes("gap-4"):
                ui.switch("Enable notifications", value=enabled).props(f'color="{ACCENT}"')

                with ui.row().classes("gap-4"):
                    discord_url = notif_cfg.get("discord", {}).get("webhook_url", "")
                    ui.input(
                        "Discord Webhook URL",
                        value=discord_url,
                        placeholder="https://discord.com/api/webhooks/...",
                    ).props('outlined').classes("col")

                    tg_token = notif_cfg.get("telegram", {}).get("bot_token", "")
                    ui.input(
                        "Telegram Bot Token",
                        value=tg_token,
                        placeholder="123456:ABC-DEF...",
                    ).props('outlined').classes("col")

        # ── Rate Limits ──────────────────────────────────────────────
        with ui.element("div").classes("glass-card card-hover w-full").style("margin-bottom:24px;"):
            ui.label("Rate Limits").classes("settings-section-title")

            rate_limits_cfg = collector.config.get("rate_limits", {})

            with ui.row().classes("gap-4 w-full").style("flex-wrap:wrap;"):
                rl_yt = ui.number(
                    "YouTube",
                    value=rate_limits_cfg.get("youtube", 5),
                    min=1, max=50,
                    format="%d",
                ).props('outlined').style("width:150px;")

                rl_ig = ui.number(
                    "Instagram",
                    value=rate_limits_cfg.get("instagram", 5),
                    min=1, max=50,
                    format="%d",
                ).props('outlined').style("width:150px;")

                rl_x = ui.number(
                    "X / Twitter",
                    value=rate_limits_cfg.get("x", 5),
                    min=1, max=50,
                    format="%d",
                ).props('outlined').style("width:150px;")

                rl_tt = ui.number(
                    "TikTok",
                    value=rate_limits_cfg.get("tiktok", 5),
                    min=1, max=50,
                    format="%d",
                ).props('outlined').style("width:150px;")

        # ── Advanced ────────────────────────────────────────────────
        with ui.element("div").classes("glass-card card-hover w-full").style("margin-bottom:24px;"):
            ui.label("Advanced").classes("settings-section-title")

            reliability = collector.config.get("reliability", {})
            schedule = collector.config.get("schedule", {})

            with ui.row().classes("gap-4 w-full").style("flex-wrap:wrap;"):
                ui.number(
                    "Max Retries",
                    value=reliability.get("max_retries", 3),
                    min=1, max=10,
                ).props('outlined').style("width:150px;")

                ui.number(
                    "Retry Backoff (s)",
                    value=reliability.get("retry_backoff", 2),
                    min=1, max=60,
                ).props('outlined').style("width:150px;")

                ui.number(
                    "Check Interval (s)",
                    value=schedule.get("check_interval", 900),
                    min=60, max=3600,
                ).props('outlined').style("width:150px;")

        # ── System Paths ────────────────────────────────────────────────
        with ui.element("div").classes("glass-card card-hover w-full").style("margin-bottom:24px;"):
            ui.label("System Paths").classes("settings-section-title")

            paths_info = [
                ("Config directory", str(config_dir)),
                ("State file", str(config_dir / "state.json")),
                ("Config file", str(config_path)),
                ("Downloads", str(config_dir / "downloads")),
                ("Logs", str(config_dir / "logs")),
            ]
            for label, path in paths_info:
                with ui.row().classes("items-center gap-3").style("padding:8px 0;"):
                    ui.icon("folder", size="16px", color=TEXT_MUTED)
                    ui.label(f"{label}:").style(f"color:{TEXT_SECONDARY}; min-width:130px; font-size:0.85rem;")
                    ui.label(path).style(f"color:{TEXT_PRIMARY}; font-family:monospace; font-size:0.8rem;")

        # ── Save Button ─────────────────────────────────────────────────
        with ui.row().classes("gap-3"):
            async def save_settings() -> None:
                """Persist dashboard settings changes to config file."""
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
            ).classes("btn-transition").style("padding:10px 32px;")

            ui.button("Reset to Defaults", icon="refresh", on_click=lambda: ui.notify("Reset not yet implemented", type="warning")).props(
                'color="grey-8" rounded outline'
            ).style("padding:10px 32px;")


# ── Dashboard Factory ───────────────────────────────────────────────────

def create_dashboard(config_dir: str = "~/.xpst"):
    """Register all dashboard pages with NiceGUI."""
    collector = AnalyticsCollector(config_dir)

    @ui.page("/")
    def overview() -> None:
        """Route handler for the Overview dashboard page."""
        _page_overview(collector)

    @ui.page("/content")
    def content() -> None:
        """Route handler for the Content Library page."""
        _page_content(collector)

    @ui.page("/analytics")
    def analytics() -> None:
        """Route handler for the Analytics page."""
        _page_analytics(collector)

    @ui.page("/connect")
    def connect() -> None:
        """Route handler for the Connect page."""
        _page_connect(collector)

    @ui.page("/settings")
    def settings() -> None:
        """Route handler for the Settings page."""
        _page_settings(collector)
