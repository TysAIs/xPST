# xPST Dashboard — Complete Component Map

> Generated from source analysis of `~/XPST/src/xpst/dashboard/app.py` (1728 lines),
> `server.py`, `analytics.py`, `cli.py`, `engine.py`, `state.py`, and `config.py`.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Server & Startup](#2-server--startup)
3. [Design Tokens & Theme System](#3-design-tokens--theme-system)
4. [Pages & Routes](#4-pages--routes)
5. [Data Models](#5-data-models)
6. [Shared Components & Helpers](#6-shared-components--helpers)
7. [Page-by-Page Component Breakdown](#7-page-by-page-component-breakdown)
8. [User Actions Catalog](#8-user-actions-catalog)
9. [Charts Catalog](#9-charts-catalog)
10. [Forms Catalog](#10-forms-catalog)
11. [Backend Data Flow](#11-backend-data-flow)

---

## 1. Architecture Overview

```
┌─────────────┐    ┌──────────────────┐    ┌─────────────────┐
│  CLI         │───▶│  server.py       │───▶│  app.py         │
│  xpst        │    │  start_dashboard │    │  create_dashboard│
│  dashboard   │    │  (NiceGUI/FastAPI)│    │  5 @ui.pages    │
└─────────────┘    └──────────────────┘    └────────┬────────┘
                                                     │
                                            ┌────────▼────────┐
                                            │  AnalyticsCollector│
                                            │  analytics.py     │
                                            └────────┬────────┘
                                                     │
                                          ┌──────────┼──────────┐
                                          ▼          ▼          ▼
                                     state.json  config.yaml  Platform APIs
                                    (StateManager) (XPSTConfig) (YT/IG/X/TK)
```

- **Framework**: NiceGUI (Python web UI framework built on FastAPI + Vue/Quasar)
- **Charting**: Plotly (go.Figure, go.Scatter, go.Pie, go.Bar, go.Heatmap)
- **Styling**: Custom CSS with CSS variables, Google Fonts (Inter), Material Icons
- **Theme**: Dark mode default, light mode via `.light-mode` body class toggle
- **Auth**: Optional HTTP Basic Auth (configured via `monitoring.dashboard_username/password`)
- **Metrics**: Prometheus endpoint at `/metrics`

---

## 2. Server & Startup

### `server.py` — `start_dashboard(port, host, config_dir)`

| Parameter | Default | Description |
|-----------|---------|-------------|
| `port` | 8080 | HTTP port |
| `host` | `"0.0.0.0"` | Bind address |
| `config_dir` | `"~/.xpst"` | Config directory path |

**Startup sequence:**
1. Load dashboard auth credentials from `config.yaml` → `monitoring.dashboard_username/password`
2. Call `create_dashboard(config_dir)` — registers all 5 pages
3. Register `/metrics` Prometheus endpoint → `xpst.utils.metrics.metrics_text()`
4. If auth enabled, add `BasicAuthMiddleware` to FastAPI app
5. Call `ui.run(port, host, title="xPST — Dashboard", dark=True, favicon="📊", show=False, reload=False)`

**Auth middleware** (`_setup_basic_auth`):
- Skips auth for `/metrics` endpoint
- Supports both plaintext and `sha256:` hashed passwords
- Returns 401 with `WWW-Authenticate: Basic realm="xPST Dashboard"` on failure

### `cli.py` — `xpst dashboard` command

```python
@click.option("--port", "-p", default=8080, type=int)
def dashboard(ctx, port):
    # Loads config to get config_dir
    # Calls start_dashboard(port=port, config_dir=config_dir)
```

---

## 3. Design Tokens & Theme System

### Color Tokens

| Token | Dark Mode | Light Mode | Usage |
|-------|-----------|------------|-------|
| `--bg` | `#1c1c1e` | `#ffffff` | Page background |
| `--surface` | `#2c2c2e` | `#f2f2f7` | Card backgrounds |
| `--sidebar-bg` | `#3a3a3c` | `#e5e5ea` | Sidebar background |
| `--text` | `#ffffff` | `#1c1c1e` | Primary text |
| `--text-sec` | `#ebebf5` | `#3a3a3c` | Secondary text |
| `--text-muted` | `#8e8e93` | `#8e8e93` | Muted text (same both) |
| `--border` | `rgba(255,255,255,0.08)` | `rgba(0,0,0,0.08)` | Borders |
| `--card-shadow` | `0 1px 3px rgba(0,0,0,0.3)` | `0 1px 3px rgba(0,0,0,0.08)` | Card base shadow |
| `--card-hover-shadow` | `0 4px 12px rgba(0,0,0,0.4)` | `0 4px 12px rgba(0,0,0,0.12)` | Card hover shadow |

### Semantic Colors

| Name | Hex | Usage |
|------|-----|-------|
| `ACCENT` | `#0a84ff` | Primary action color, active nav, active tabs |
| `GREEN` | `#30d158` | Healthy status, success states, "This Week" card |
| `RED` | `#ff453a` | Error status, "Total Likes" card accent |
| `ORANGE` | `#ff9f0a` | Degraded status, warnings |

### Platform Colors

| Platform | Color | Icon | Badge Label |
|----------|-------|------|-------------|
| YouTube | `#ff0000` | `▶` | `YT` |
| Instagram | `#e1306c` | `📷` | `IG` |
| X/Twitter | `#1d9bf0` | `𝕏` | `X` |
| TikTok | `#00f2ea` | `♪` | `TK` |

### Typography

| Element | Font | Size | Weight | Other |
|---------|------|------|--------|-------|
| Font family | Inter, -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif | — | — | `!important` override |
| Page title | Inter | `clamp(1.4rem, 2.5vw, 1.75rem)` | 700 | `letter-spacing: -0.02em` |
| Page subtitle | Inter | `clamp(0.8rem, 1.2vw, 0.875rem)` | — | `color: var(--text-muted)` |
| Section header | Inter | `clamp(1rem, 1.5vw, 1.125rem)` | 600 | `letter-spacing: -0.01em` |
| Metric value | Inter | `clamp(1.4rem, 2.5vw, 1.75rem)` | 700 | `letter-spacing: -0.02em` |
| Metric label | Inter | `clamp(0.7rem, 1vw, 0.75rem)` | 500 | `text-transform: uppercase; letter-spacing: 0.06em` |
| Nav item | Inter | 14px | 500 | — |
| Badge | Inter | 11px | 600 | `letter-spacing: 0.02em` |
| Badge-sm | Inter | 10px | — | — |
| Settings section | Inter | 12px | 600 | `text-transform: uppercase; letter-spacing: 0.08em` |

### Spacing & Layout

| Element | Value |
|---------|-------|
| `--sidebar-width` | `240px` |
| Sidebar collapsed | `0px` |
| Card border-radius | `12px` |
| Card padding | `20px` |
| Metric card padding | `20px` |
| Content max-width | `1400px` |
| Content inner padding | `28px 36px` |
| Grid gap (metric cards) | `16px` |
| Grid gap (content cards) | `16px` |
| Metric card min-width | `200px` |
| Content card min-width | `240px` |
| Chart side width | `320px` (min `260px`) |

### Responsive Breakpoints

| Breakpoint | Sidebar | Cards Grid | Content Cards | Notes |
|------------|---------|------------|---------------|-------|
| > 1200px | 240px | `repeat(auto-fit, minmax(200px, 1fr))` | `repeat(auto-fill, minmax(240px, 1fr))` | Default |
| ≤ 1200px | 220px | `repeat(2, 1fr)` | `minmax(220px, 1fr)` | Tablet |
| ≤ 768px | Hidden (hamburger) | `repeat(2, 1fr)` | `minmax(160px, 1fr)` | Mobile |
| ≤ 480px | Hidden (hamburger) | `1fr` | `1fr` | Small mobile |

### Animations

| Name | Duration | Effect |
|------|----------|--------|
| `fadeIn` | 0.2s ease | `opacity: 0 → 1; translateY(8px) → 0` |
| Card hover | 0.2s ease | `translateY(-2px)` + shadow increase |
| Theme toggle | 0.3s ease | Background + color transition |
| Sidebar open | 0.3s cubic-bezier(0.4, 0, 0.2, 1) | `translateX(-100%) → 0` |

---

## 4. Pages & Routes

| Route | Page Title | Subtitle | Function | Description |
|-------|-----------|----------|----------|-------------|
| `/` | Dashboard | Your cross-platform content at a glance | `_page_overview()` | Main dashboard with metrics, charts, recent posts, platform health |
| `/content` | Content Library | Browse and manage all your cross-posted content | `_page_content()` | Browsable grid of all cross-posted content with search/filter |
| `/analytics` | Analytics | Deep dive into your content performance | `_page_analytics()` | Detailed analytics with platform tabs, charts, heatmaps |
| `/connect` | Connect Accounts | Link your social platforms to start cross-posting | `_page_connect()` | Platform connection status and setup instructions |
| `/settings` | Settings | Configure your xPST installation | `_page_settings()` | Configuration form for all xPST settings |
| `/metrics` | — | — | `metrics_page()` | Prometheus text metrics (plain text, not HTML) |

### Registration (factory pattern)

```python
def create_dashboard(config_dir: str = "~/.xpst"):
    collector = AnalyticsCollector(config_dir)   # Single shared instance

    @ui.page("/")         → _page_overview(collector)
    @ui.page("/content")  → _page_content(collector)
    @ui.page("/analytics")→ _page_analytics(collector)
    @ui.page("/connect")  → _page_connect(collector)
    @ui.page("/settings") → _page_settings(collector)
```

---

## 5. Data Models

### 5.1 State File (`~/.xpst/state.json`) — via `StateManager`

```json
{
  "version": 2,
  "posted_videos": {
    "<video_id>": {
      "tiktok_url": "string | null",
      "caption": "string | null",
      "posted_to": {
        "youtube": {"id": "string", "url": "string", "timestamp": "ISO8601"},
        "x": {"id": "string", "url": "string", "timestamp": "ISO8601"},
        "instagram": {"id": "string", "url": "string", "timestamp": "ISO8601"}
      },
      "downloaded_at": "ISO8601",
      "last_attempt": "ISO8601",
      "content_hash": "string | null"
    }
  },
  "content_hashes": {"<hash>": "<video_id>"},
  "health": {
    "platforms": {
      "youtube": {
        "status": "ok | degraded | error | unknown",
        "last_success": "ISO8601 | null",
        "last_failure": "ISO8601 | null",
        "failures": 0,
        "circuit_breaker_open": false,
        "last_error": "string | null"
      },
      "x": { ... },
      "instagram": { ... }
    },
    "total_processed": 0,
    "last_check": "ISO8601 | null"
  }
}
```

### 5.2 Analytics Summary Stats — `AnalyticsCollector.get_summary_stats()`

```python
{
    "total_posts": int,              # Count of posted_videos keys
    "total_processed": int,          # From health.total_processed
    "platform_counts": {             # Posts per platform
        "youtube": int,
        "instagram": int,
        "x": int,
        "tiktok": int,
    },
    "platform_health": dict,         # Raw health.platforms from state
    "last_check": str | None,        # ISO8601
    "posts_this_week": int,          # Posts with downloaded_at in last 7 days
    "best_platform": str | None,     # Platform with highest post count
    "total_platform_posts": int,     # Sum of all platform_counts
}
```

### 5.3 Post Data — `AnalyticsCollector.get_all_posts()`

```python
{
    "video_id": str,
    "caption": str,                  # Falls back to video_id if None
    "tiktok_url": str | None,
    "downloaded_at": str | None,     # ISO8601
    "last_attempt": str | None,      # ISO8601
    "platforms": {                   # Keyed by platform name
        "youtube": {"id": ..., "url": ..., "timestamp": ...},
        ...
    },
    "status": "posted" | "pending",  # "pending" if platforms is empty
}
```

### 5.4 Platform Health — `AnalyticsCollector.get_platform_health_all()`

```python
{
    "name": "youtube" | "instagram" | "x" | "tiktok",
    "label": "YouTube" | "Instagram" | "X / Twitter" | "TikTok",
    "color": "#ff0000" | "#e1306c" | "#1d9bf0" | "#00f2ea",
    "icon": "▶" | "📷" | "𝕏" | "♪",
    "configured": bool,              # Based on credential files existing
    "status": "ok" | "degraded" | "error" | "unknown",
    "failures": int,
    "last_success": str | None,      # ISO8601
    "last_failure": str | None,      # ISO8601
    "last_error": str | None,
    "circuit_breaker_open": bool,
}
```

### 5.5 Engagement Data — `AnalyticsCollector.get_engagement_data()`

```python
{
    "youtube":    {"posts": int, "views": int, "likes": int, "comments": int, "shares": int},
    "instagram":  {"posts": int, "views": int, "likes": int, "comments": int, "shares": int},
    "x":          {"posts": int, "views": int, "likes": int, "comments": int, "shares": int},
    "tiktok":     {"posts": int, "views": int, "likes": int, "comments": int, "shares": int},
}
```

### 5.6 Config File (`~/.xpst/config.yaml`) — via `XPSTConfig`

Sections: `accounts`, `video`, `reliability`, `monitoring`, `notifications`, `rate_limits`, `schedule`

Key config fields used by dashboard settings page:

| Section | Field | Type | Default |
|---------|-------|------|---------|
| `accounts.tiktok.username` | TikTok username | str | `""` |
| `video.download_dir` | Download directory | str | `"~/.xpst/downloads"` |
| `accounts.youtube.enabled` | YouTube enabled | bool | `True` |
| `accounts.instagram.enabled` | Instagram enabled | bool | `True` |
| `accounts.x.enabled` | X enabled | bool | `True` |
| `notifications.enabled` | Notifications on | bool | `False` |
| `notifications.discord.webhook_url` | Discord webhook | str | `""` |
| `notifications.telegram.bot_token` | Telegram bot token | str | `""` |
| `notifications.telegram.chat_id` | Telegram chat ID | str | `""` |
| `rate_limits.youtube` | YouTube daily limit | int | `5` |
| `rate_limits.instagram` | Instagram daily limit | int | `5` |
| `rate_limits.x` | X daily limit | int | `5` |
| `rate_limits.tiktok` | TikTok daily limit | int | `5` |
| `reliability.max_retries` | Max retry attempts | int | `3` |
| `reliability.retry_backoff` | Backoff seconds | int | `2` |
| `schedule.check_interval` | Check interval (s) | int | `900` |
| `monitoring.dashboard_username` | Auth username | str | `""` |
| `monitoring.dashboard_password` | Auth password | str | `""` |

---

## 6. Shared Components & Helpers

### 6.1 Page Shell (`_page_shell(current)`)

Every page uses this layout:

```
┌─────────────────────────────────────────────────────┐
│ [☰ Hamburger]  (mobile only, z-index: 1000)         │
│ [Overlay]       (mobile, click to close sidebar)    │
├──────────────┬──────────────────────────────────────┤
│   Sidebar    │   Main Content (fade-in)             │
│   240px      │   max-width: 1400px, centered        │
│              │   padding: 28px 36px                  │
│   ┌──────┐   │                                      │
│   │ xPST │   │   ── page content here ──            │
│   └──────┘   │                                      │
│   ◉ Overview │                                      │
│   ◫ Content  │                                      │
│   ◈ Analytics│                                      │
│   ⧫ Connect  │                                      │
│   ⚙ Settings │                                      │
│              │                                      │
│   ☽ Dark Mode│                                      │
│              │                                      │
│   v0.1.0     │                                      │
└──────────────┴──────────────────────────────────────┘
```

### 6.2 Sidebar (`_sidebar(current)`)

**Navigation items:**

| Path | Icon | Label | Active Check |
|------|------|-------|-------------|
| `/` | `◉` | Overview | `path == current` |
| `/content` | `◫` | Content | `path == current` |
| `/analytics` | `◈` | Analytics | `path == current` |
| `/connect` | `⧫` | Connect | `path == current` |
| `/settings` | `⚙` | Settings | `path == current` |

Active nav item styling: `background: rgba(10,132,255,0.15); color: #0a84ff;`

**Theme toggle**: Click `.theme-toggle` → `_toggle_theme()` → toggles `.light-mode` class on `<body>`, persists to `localStorage('xpst-theme')`, updates icon ☽↔☀ and label.

### 6.3 Icon System (`_ICONS` dict + `_icon()`)

Uses Unicode symbols (no external icon font CDN dependency):

| Key | Symbol | Used in |
|-----|--------|---------|
| `hub` | ⚡ | Sidebar logo |
| `dashboard` | ◉ | Overview nav |
| `video_library` | ◫ | Content nav, empty states |
| `insights` | ◈ | Analytics nav |
| `link` | ⧫ | Connect nav |
| `settings` | ⚙ | Settings nav |
| `dark_mode` | ☽ | Theme toggle |
| `light_mode` | ☀ | Theme toggle (light) |
| `smart_display` | ▶ | YouTube icon |
| `photo_camera` | 📷 | Instagram icon |
| `tag` | 𝕏 | X icon |
| `music_note` | ♪ | TikTok icon |
| `visibility` | 👁 | "Total Reach" card, "Total Views" card |
| `favorite` | ♥ | "Total Likes" card |
| `comment` | 💬 | "Comments" card |
| `share` | ↗ | "Shares" card |
| `check_circle` | ✓ | Connected status |
| `cancel` | ✗ | Not connected, failed status |
| `emoji_events` | 🏆 | "Best Platform" card |
| `schedule` | ⏱ | "This Week" card |
| `add_link` | ⛓ | Connect button |
| `search` | 🔍 | (available but not directly used in UI) |
| `refresh` | ↻ | Reset to defaults button |
| `info` | ℹ | Setup guide icon |
| `folder` | 📂 | System paths |

### 6.4 Metric Card (`_metric_card(icon, value, label, accent_color)`)

```html
<div class="metric-card">
  <div class="metric-card-icon" style="background:{accent_color}18;">
    {icon} (20px, accent_color)
  </div>
  <label class="metric-value">{value}</label>
  <label class="metric-label">{label}</label>
</div>
```

### 6.5 Section Header (`_section_header(title, subtitle)`)

```html
<label class="page-title">{title}</label>
<label class="page-subtitle">{subtitle}</label>  <!-- if provided -->
```

### 6.6 Plotly Theme (`_plotly_layout(fig, height)`)

Applied to every Plotly chart:
- `paper_bgcolor`: transparent
- `plot_bgcolor`: transparent
- `font`: `color: #ebebf5, family: Inter, size: 12`
- `xaxis`: no grid, no zeroline
- `yaxis`: grid `rgba(128,128,128,0.1)`, no zeroline, show grid
- `margin`: `t=10, b=30, l=40, r=10`
- `hovermode`: `x unified`
- Light mode overrides via CSS: text fill `#1c1c1e`, tick text `#3a3a3c`

### 6.7 Helper Functions

| Function | Input | Output | Usage |
|----------|-------|--------|-------|
| `_fmt_num(n)` | `int|float|None` | `"1.2M"`, `"3.4K"`, `"42"` | Format large numbers |
| `_relative_time(ts_str)` | ISO8601 string | `"2h ago"`, `"3d ago"`, `"just now"` | Relative timestamps |
| `_status_color(status)` | `"ok"/"degraded"/"error"/"unknown"` | Color hex | Status dot coloring |
| `_status_label(status)` | `"ok"/"degraded"/"error"/"unknown"` | `"Healthy"/"Degraded"/"Error"/"Unknown"` | Status text |

---

## 7. Page-by-Page Component Breakdown

### 7.1 Overview Page (`/`)

**Data sources:**
- `collector.get_summary_stats()` → stats dict
- `collector.get_all_posts()` → posts list
- `collector.get_platform_health_all()` → health list
- `collector.get_posts_over_time(30)` → date→count dict

#### 7.1.1 Metric Cards Row (4 cards, `metric-cards-grid`)

| # | Icon | Value Source | Label | Accent Color |
|---|------|-------------|-------|-------------|
| 1 | `video_library` | `stats["total_posts"]` | "Total Posts" | `#0a84ff` (ACCENT) |
| 2 | `visibility` | `_fmt_num(stats["total_platform_posts"])` | "Total Reach" | `#a855f7` (purple) |
| 3 | `emoji_events` | `PLATFORM_LABELS[stats["best_platform"]]` | "Best Platform" | Platform's color |
| 4 | `schedule` | `stats["posts_this_week"]` | "This Week" | `#30d158` (GREEN) |

#### 7.1.2 Charts Row (2 charts, `charts-row`)

**Left: Posts Over Time** (`chart-main`, glass-card)
- Type: Plotly `Scatter` (line + area fill)
- Data: `collector.get_posts_over_time(30)` → `{date: count}`
- Fallback: 30-day zero-fill
- Style: `line_color=#0a84ff`, `shape=spline`, `marker_size=5`, `fill=tozeroy`, `fillcolor=rgba(10,132,255,0.08)`
- Height: 260px
- Hover: `"%{x}<br>%{y} posts"`

**Right: By Platform** (`chart-side`, 320px, glass-card)
- Type: Plotly `Pie` (donut)
- Data: `stats["platform_counts"]` filtered to `> 0`
- Colors: Platform-specific (`#ff0000`, `#e1306c`, `#1d9bf0`, `#00f2ea`)
- Style: `hole=0.6`, `textfont: color=#ebebf5, size=11, family=Inter`
- Legend: horizontal, bottom, `font: color=#8e8e93, size=11`
- Height: 260px
- Fallback: `["No posts yet"]` gray circle

#### 7.1.3 Recent Posts Section (glass-card, `content-cards-grid`)

- Shows up to **6** posts (from `posts[:6]`)
- Each post is a `content-card`:
  - **Thumbnail placeholder**: 16:9 aspect ratio, gradient background from platform color
  - **Platform icon**: 32px, platform color
  - **Caption**: truncated to 80 chars
  - **Platform badges**: colored `badge-sm` for each platform posted to
  - **Relative time**: `downloaded_at` formatted as "2h ago"
- Empty state: 48px video_library icon, "No posts yet", "Run `xpst run` to get started"

#### 7.1.4 Platform Health Section (glass-card, `content-cards-grid`)

- 4 cards (one per platform), each showing:
  - Platform icon in colored 32px rounded square
  - Platform label (14px, 600 weight)
  - Status dot + status text:
    - Not configured → gray dot, "Not Configured"
    - Circuit breaker open → red dot, "Circuit Breaker Open"
    - Failures > 0 → orange dot, "Degraded ({n} failures)"
    - Status ok → green dot, "Healthy"
    - Otherwise → gray dot, "Unknown"

---

### 7.2 Content Library Page (`/content`)

**Data sources:**
- `collector.get_all_posts()` → full posts list

#### 7.2.1 Search & Filter Bar (glass-card)

- **Search input**: `ui.input(placeholder="Search posts...")`, `outlined dense`, max-width 380px
  - Events: `on("update:model-value", _on_search, throttle=0.3)`
  - Searches title and caption (case-insensitive)
- **Filter pills** (row, horizontal scroll on mobile):
  - `All` → platform: `"all"`
  - `YouTube` → platform: `"youtube"`
  - `Instagram` → platform: `"instagram"`
  - `X` → platform: `"x"`
  - `TikTok` → platform: `"tiktok"`
  - Styling: `filter-pill` class, 20px border-radius
  - Active: `background: #0a84ff; color: #ffffff; border-color: #0a84ff`

#### 7.2.2 Posts Grid (`_render_posts_grid`)

- Layout: `content-cards-grid` (`repeat(auto-fill, minmax(240px, 1fr))`)
- Each `content-card`:
  - **Thumbnail**: gradient background from platform color, platform icon 36px
  - **Status badge** (top-right overlay):
    - Posted: green (`rgba(48,209,88,0.9)`) `✓ POSTED`
    - Pending: orange (`rgba(255,159,10,0.9)`) `⏳ PENDING`
    - Failed: red (`rgba(255,69,58,0.9)`) `✗ FAILED`
  - **Caption**: truncated to 100 chars, min-height 36px
  - **Platform badges**: clickable links to post URLs where available
  - **Footer row**: relative time + video_id (monospace, first 12 chars)
- Empty state: "No content found", "Your cross-posted content will appear here"

---

### 7.3 Analytics Page (`/analytics`)

**Data sources:**
- `collector.get_summary_stats()` → stats
- `collector.get_all_posts()` → posts
- `collector.get_engagement_data()` → engagement (tries live API calls first, falls back to state)
- `collector.get_top_posts(5)` → top 5 posts by platform count

#### 7.3.1 Platform Tabs (row, `analytics-tabs`)

| Label | Platform Filter |
|-------|----------------|
| All | `"all"` |
| YouTube | `"youtube"` |
| Instagram | `"instagram"` |
| X / Twitter | `"x"` |
| TikTok | `"tiktok"` |

- Styling: `platform-tab` class
- Active: `color: #0a84ff; background: rgba(10,132,255,0.12); border-color: rgba(10,132,255,0.2)`
- On click: clears `analytics_container`, re-renders with `_render_analytics_content(container, collector, platform)`

#### 7.3.2 Engagement Metric Cards (4 cards)

| # | Icon | Value | Label | Color |
|---|------|-------|-------|-------|
| 1 | `visibility` | `_fmt_num(total_views)` | "Total Views" | `#0a84ff` |
| 2 | `favorite` | `_fmt_num(total_likes)` | "Total Likes" | `#ff453a` |
| 3 | `chat_bubble` | `_fmt_num(total_comments)` | "Comments" | `#a855f7` |
| 4 | `share` | `_fmt_num(total_shares)` | "Shares" | `#30d158` |

For `platform == "all"`: aggregates across all platforms. Otherwise: single platform only.

#### 7.3.3 Per-Platform Breakdown (only when `platform == "all"`)

- Glass card with `content-cards-grid`
- 4 platform cards, each showing:
  - Icon in colored rounded square (32px)
  - Platform label (14px, 600)
  - Metrics list: Views, Likes, Comments, Posts — each with label (muted) + formatted value (bold)

#### 7.3.4 Engagement Over Time Chart (`chart-main`)

- Type: Plotly `Scatter` (line + area fill)
- Data: `collector.get_posts_over_time(30)`, filtered per platform if not "all"
- Color: platform-specific color or ACCENT for "all"
- Height: 280px

#### 7.3.5 Platform Comparison Chart (`chart-side`)

- Type: Plotly `Bar`
- Data: `stats["platform_counts"]` for each platform
- Colors: platform-specific
- Style: `cornerradius=6`, text outside, `textfont: color=#ebebf5, size=12`
- Height: 280px

#### 7.3.6 Posting Activity Heatmap (`chart-main`)

- Type: Plotly `Heatmap`
- Data: Posts grouped by `hour × day_of_week` (Mon-Sun, HH:00)
- Colorscale: `[[0, rgba(128,128,128,0.1)], [0.3, #1e3a5f], [0.6, #0a84ff], [1, #60c0ff]]`
- Hover: `"%{y} %{x}: %{z} posts"`
- Height: 280px
- Empty state: "Not enough data for heatmap"

#### 7.3.7 Top Posts by Reach (`chart-side`)

- Shows top 5 posts ranked by platform count
- Each row: rank number (#1-#5), thumbnail gradient (44×30px), caption (40 chars), platform badges, platform count
- Bordered rows with bottom border

---

### 7.4 Connect Page (`/connect`)

**Data sources:**
- `collector.get_platform_health_all()` → health list

#### 7.4.1 Connection Progress Bar (glass-card)

- Label: `"{n} of {n} platforms connected"`
- Progress bar: 180px wide, 6px height
  - Track: `var(--border)`
  - Fill: `#0a84ff`, width = percentage, animated transition

#### 7.4.2 Platform Cards Grid (`content-cards-grid`)

4 `connect-card` elements (28px padding, centered text):

**If configured (connected):**
- Icon in colored 48×48px rounded square (12px radius)
- Platform label (16px, 600)
- Green checkmark + "Connected" (green text)
- TikTok: shows `@username`
- Others: "Last active: {relative_time}"
- Status line:
  - ok → "All systems operational" (green)
  - circuit breaker open → "Temporarily disabled" (red)
  - failures > 0 → "{n} recent failures" (orange)
  - else → "Status unknown" (muted)

**If not configured:**
- Same icon layout
- Platform label
- Muted ✗ + "Not Connected"
- Platform-specific description:
  - YouTube: "Upload shorts to YouTube. Requires OAuth 2.0 credentials."
  - Instagram: "Post reels to Instagram. Requires session authentication."
  - X: "Post videos to X/Twitter. Requires browser cookies."
  - TikTok: "Source platform for content. Configure username only."
- Button: "Connect" (or "Configure" for TikTok)
  - On click: `ui.notify("Run `xpst connect {name}` to set up", type="info")`
  - Style: platform color, rounded, full width

#### 7.4.3 Setup Guide Card (glass-card)

- ℹ icon + "Setup Guide" title
- Instructions: "To connect a platform, run the following command in your terminal:"
- Code block: `xpst connect <platform>` (blue text, gray background)
- Supported: "youtube, x, instagram, tiktok"

---

### 7.5 Settings Page (`/settings`)

**Data sources:**
- `collector.config` → raw config dict from `config.yaml`

#### 7.5.1 General Section

| Field | Type | Default | Props |
|-------|------|---------|-------|
| TikTok Username | `ui.input` | from `accounts.tiktok.username` | `outlined`, max-width 400px |
| Download Directory | `ui.input` | from `video.download_dir` | `outlined`, max-width 500px |

#### 7.5.2 Platforms Section

4 platform toggle rows (each with icon, label, and `ui.switch`):

| Platform | Color | Enabled Check |
|----------|-------|---------------|
| YouTube | `#ff0000` | `accounts.youtube.enabled` |
| Instagram | `#e1306c` | `accounts.instagram.enabled` |
| X / Twitter | `#1d9bf0` | `accounts.x.enabled` |
| TikTok | `#00f2ea` | `bool(accounts.tiktok.username)` |

Each row: icon (32px square) + label + switch (right-aligned)

#### 7.5.3 Notifications Section

| Field | Type | Source |
|-------|------|--------|
| Enable notifications | `ui.switch` | `notifications.enabled` |
| Discord Webhook URL | `ui.input` | `notifications.discord.webhook_url` |
| Telegram Bot Token | `ui.input` | `notifications.telegram.bot_token` |
| Telegram Chat ID | `ui.input` | `notifications.telegram.chat_id` |

#### 7.5.4 Rate Limits Section

4 `ui.number` inputs in a row:

| Platform | Source | Min | Max |
|----------|--------|-----|-----|
| YouTube | `rate_limits.youtube` | 1 | 50 |
| Instagram | `rate_limits.instagram` | 1 | 50 |
| X / Twitter | `rate_limits.x` | 1 | 50 |
| TikTok | `rate_limits.tiktok` | 1 | 50 |

#### 7.5.5 Advanced Section

| Field | Source | Min | Max |
|-------|--------|-----|-----|
| Max Retries | `reliability.max_retries` | 1 | 10 |
| Retry Backoff (s) | `reliability.retry_backoff` | 1 | 60 |
| Check Interval (s) | `schedule.check_interval` | 60 | 3600 |

#### 7.5.6 System Paths Section (read-only display)

| Label | Path |
|-------|------|
| Config directory | `{config_dir}` |
| State file | `{config_dir}/state.json` |
| Config file | `{config_dir}/config.yaml` |
| Downloads | `{config_dir}/downloads` |
| Logs | `{config_dir}/logs` |

Each row: folder icon + label (120px min-width) + monospace path value

#### 7.5.7 Save Buttons

| Button | Icon | Color | Action |
|--------|------|-------|--------|
| "Save Settings" | save | `#0a84ff`, rounded | `save_settings()` |
| "Reset to Defaults" | refresh | grey-7, rounded, outline | `reset_defaults()` |

**`save_settings()` logic:**
1. Loads current config via `XPSTConfig.load(config_path)`
2. Sets TikTok username (empty if toggle off)
3. Sets download directory
4. Sets platform enabled flags
5. Sets notification settings
6. Sets rate limits (cast to int)
7. Sets advanced/reliability settings (cast to int)
8. Calls `cfg.save(config_path)`
9. Shows success/error notification

**`reset_defaults()` logic:**
1. Creates `XPSTConfig()` with defaults
2. Saves to config_path
3. Shows notification
4. Reloads page via `location.reload()`

---

## 8. User Actions Catalog

| Action | Location | Trigger | Handler | Effect |
|--------|----------|---------|---------|--------|
| **Navigate to Overview** | Sidebar | Click "◉ Overview" | `ui.link(target="/")` | Page navigation |
| **Navigate to Content** | Sidebar | Click "◫ Content" | `ui.link(target="/content")` | Page navigation |
| **Navigate to Analytics** | Sidebar | Click "◈ Analytics" | `ui.link(target="/analytics")` | Page navigation |
| **Navigate to Connect** | Sidebar | Click "⧫ Connect" | `ui.link(target="/connect")` | Page navigation |
| **Navigate to Settings** | Sidebar | Click "⚙ Settings" | `ui.link(target="/settings")` | Page navigation |
| **Toggle theme** | Sidebar | Click "☽ Dark Mode" | `_toggle_theme()` | Toggles `.light-mode` on body, updates localStorage, swaps icon/label |
| **Toggle mobile sidebar** | Hamburger (mobile) | Click ☰ | `xpstToggleSidebar()` | Toggles sidebar `.open` class + overlay visibility |
| **Close mobile sidebar** | Overlay (mobile) | Click overlay | `xpstCloseSidebar()` | Removes `.open` from sidebar, hides overlay |
| **Search posts** | Content Library | Type in search input | `_on_search()` (throttled 0.3s) | Filters posts grid by caption/title |
| **Filter by platform** | Content Library | Click filter pill | `_filter_posts(platform)` | Clears container, re-renders filtered grid |
| **Switch analytics tab** | Analytics | Click platform tab | `_show_analytics(platform)` | Clears container, re-renders with platform filter |
| **Connect platform** | Connect page | Click "Connect"/"Configure" button | `ui.notify(...)` | Shows info notification: "Run `xpst connect {name}` to set up" |
| **Save settings** | Settings page | Click "Save Settings" | `save_settings()` | Loads config, applies all form values, saves to YAML, shows notification |
| **Reset to defaults** | Settings page | Click "Reset to Defaults" | `reset_defaults()` | Creates default config, saves, reloads page |
| **Click platform badge** | Content Library | Click badge link | `<a href="{url}" target="_blank">` | Opens post URL in new tab |

---

## 9. Charts Catalog

| # | Page | Chart Name | Type | Data Source | Dimensions | Key Styling |
|---|------|-----------|------|-------------|-----------|-------------|
| 1 | Overview | Posts Over Time | Scatter (line+area) | `collector.get_posts_over_time(30)` | 260px height, `chart-main` | `#0a84ff`, spline, fill to zeroy |
| 2 | Overview | By Platform | Pie (donut) | `stats["platform_counts"]` | 260px height, `chart-side` (320px) | hole=0.6, platform colors, horizontal legend |
| 3 | Analytics | Engagement Over Time | Scatter (line+area) | `collector.get_posts_over_time(30)` filtered by platform | 280px height, `chart-main` | Platform color or accent, spline |
| 4 | Analytics | Platform Comparison | Bar | `stats["platform_counts"]` | 280px height, `chart-side` (320px) | Platform colors, cornerradius=6, text outside |
| 5 | Analytics | Posting Activity Heatmap | Heatmap | Posts grouped by hour×weekday | 280px height, `chart-main` | Blue colorscale, no colorbar |
| 6 | Analytics | Top Posts by Reach | List (not chart) | `collector.get_top_posts(5)` | `chart-side` (320px) | Ranked #1-5 with thumbnails |

### Chart common settings (via `_plotly_layout`):
- Transparent backgrounds (both paper and plot)
- Font: Inter, 12px, `#ebebf5`
- X-axis: no grid, no zeroline
- Y-axis: light grid, no zeroline
- Margins: `t=10, b=30, l=40, r=10`
- Hover: `x unified`
- All charts: `pointer-events:none` (non-interactive)
- All charts: `width:100%; min-width:0` (responsive)

---

## 10. Forms Catalog

### Settings Page Forms

| Section | Field | Component | Props | Validation |
|---------|-------|-----------|-------|------------|
| General | TikTok Username | `ui.input` | `outlined`, placeholder `"e.g. tys.ais"` | None (empty = disabled) |
| General | Download Directory | `ui.input` | `outlined`, placeholder "Path to download directory" | None |
| Platforms | YouTube toggle | `ui.switch` | `color="#ff0000"` | — |
| Platforms | Instagram toggle | `ui.switch` | `color="#e1306c"` | — |
| Platforms | X toggle | `ui.switch` | `color="#1d9bf0"` | — |
| Platforms | TikTok toggle | `ui.switch` | `color="#00f2ea"` | — |
| Notifications | Enable notifications | `ui.switch` | `color="#0a84ff"` | — |
| Notifications | Discord Webhook URL | `ui.input` | `outlined`, placeholder URL | None |
| Notifications | Telegram Bot Token | `ui.input` | `outlined`, placeholder format | None |
| Notifications | Telegram Chat ID | `ui.input` | `outlined`, placeholder | None |
| Rate Limits | YouTube | `ui.number` | `outlined`, min=1, max=50, format="%d" | int cast |
| Rate Limits | Instagram | `ui.number` | `outlined`, min=1, max=50, format="%d" | int cast |
| Rate Limits | X / Twitter | `ui.number` | `outlined`, min=1, max=50, format="%d" | int cast |
| Rate Limits | TikTok | `ui.number` | `outlined`, min=1, max=50, format="%d" | int cast |
| Advanced | Max Retries | `ui.number` | `outlined`, min=1, max=10 | int cast |
| Advanced | Retry Backoff (s) | `ui.number` | `outlined`, min=1, max=60 | int cast |
| Advanced | Check Interval (s) | `ui.number` | `outlined`, min=60, max=3600 | int cast |

**Save logic**: All values collected into `XPSTConfig` → `cfg.save(config_path)` → YAML file write.

**Content Library search**:
- `ui.input` with `outlined dense`, max-width 380px
- Search throttled at 0.3s
- Filters by `caption` and `title` (case-insensitive substring match)
- Combined with platform filter (AND logic)

---

## 11. Backend Data Flow

### 11.1 Dashboard Startup Flow

```
CLI: xpst dashboard --port 8080
  └─▶ cli.py: dashboard()
        └─▶ server.py: start_dashboard(port, config_dir)
              ├─▶ _load_dashboard_auth(config_dir)
              │     └─▶ XPSTConfig.load() → monitoring.dashboard_username/password
              ├─▶ app.py: create_dashboard(config_dir)
              │     └─▶ AnalyticsCollector(config_dir) ← SINGLE instance shared across all pages
              │           ├─▶ Loads config.yaml (raw dict)
              │           └─▶ Cached clients: _yt_service, _ig_client, _x_client
              ├─▶ Register /metrics endpoint
              ├─▶ _setup_basic_auth() if credentials set
              └─▶ ui.run(port, host, dark=True)
```

### 11.2 Data Flow Per Page

```
Page Load (any @ui.page)
  └─▶ collector method calls (AnalyticsCollector)
        ├─▶ load_state(config_dir)
        │     └─▶ Reads ~/.xpst/state.json → returns raw dict
        │     └─▶ Falls back to empty structure if missing/corrupt
        ├─▶ get_summary_stats()
        │     └─▶ Iterates posted_videos → counts platforms, posts_this_week, best_platform
        ├─▶ get_all_posts()
        │     └─▶ Iterates posted_videos → builds post dicts, sorts by downloaded_at
        ├─▶ get_platform_health_all()
        │     └─▶ Reads health.platforms + checks credential files exist
        ├─▶ get_posts_over_time(30)
        │     └─▶ Groups posted_videos by downloaded_at date
        ├─▶ get_engagement_data()
        │     └─▶ Counts posts per platform from state
        │     └─▶ Tries live API calls (YouTube Data API v3, instagrapi, twikit, yt-dlp)
        │     └─▶ Falls back to post counts if APIs unavailable
        └─▶ get_top_posts(5)
              └─▶ Sorts by platform count descending
```

### 11.3 Settings Save Flow

```
Settings Page → "Save Settings" button click
  └─▶ save_settings()
        ├─▶ XPSTConfig.load(config_path)    ← Re-loads fresh config
        ├─▶ Sets all form values on config object
        ├─▶ cfg.save(config_path)
        │     └─▶ Serializes to YAML dict
        │     └─▶ Writes to ~/.xpst/config.yaml
        └─▶ ui.notify("Settings saved successfully!", type="positive")
```

### 11.4 Credential File Checks (for connection status)

| Platform | Credential File | Check |
|----------|----------------|-------|
| YouTube | `~/.xpst/credentials/youtube_token.json` | `Path.exists()` |
| X | `~/.xpst/credentials/x_cookies.json` | `Path.exists()` |
| Instagram | `~/.xpst/credentials/instagram_session.json` | `Path.exists()` |
| TikTok | `config.accounts.tiktok.username` | `bool(username)` |

### 11.5 Engine Methods (not directly called by dashboard, but relevant)

The dashboard does **not** call `CrossPostEngine` methods directly. The engine is the background process that populates `state.json`. The dashboard reads this state via `AnalyticsCollector`.

Key engine methods that create the data the dashboard displays:

| Engine Method | What it creates in state |
|---------------|------------------------|
| `check_and_post()` | New entries in `posted_videos`, updates `health` |
| `post_manual()` | Manual post entries in `posted_videos` |
| `post_manual_carousel()` | Carousel post entries |
| `backfill()` | Fills missing platform entries |
| `check_health()` | Platform health checks (doesn't modify state) |
| `check_and_post_bidirectional()` | Cross-posted entries |

### 11.6 Platform API Integration (AnalyticsCollector)

| Platform | API/Library | Auth File | Metrics Collected |
|----------|------------|-----------|-------------------|
| YouTube | `googleapiclient` (Data API v3) | `youtube_token.json` | views, likes, comments, duration |
| Instagram | `instagrapi` | `instagram_session.json` | likes, comments, reach, impressions, saves, shares |
| X/Twitter | `twikit` | `x_cookies.json` | likes, retweets, replies, views, bookmarks |
| TikTok | `yt-dlp` | N/A | views, likes, comments, shares |

All API clients are **lazily initialized** and **cached** on the `AnalyticsCollector` instance.

---

## Appendix: CSS Class Reference

| Class | Purpose | Key Properties |
|-------|---------|---------------|
| `.metric-cards-grid` | Grid for metric cards | `grid: auto-fit, minmax(200px, 1fr); gap: 16px` |
| `.content-cards-grid` | Grid for content cards | `grid: auto-fill, minmax(240px, 1fr); gap: 16px` |
| `.charts-row` | Flex row for charts | `flex; gap: 16px; flex-wrap: wrap` |
| `.chart-main` | Main chart area | `flex: 1; min-width: 0` |
| `.chart-side` | Side chart area | `width: 320px; min-width: 260px` |
| `.metric-card` | Metric card | `surface bg; border-radius: 12px; padding: 20px; border` |
| `.metric-card-icon` | Icon container in card | `36×36px; border-radius: 10px` |
| `.metric-value` | Large number | `clamp(1.4-1.75rem); font-weight: 700` |
| `.metric-label` | Label below number | `clamp(0.7-0.75rem); uppercase; text-muted` |
| `.glass-card` | Section container | `surface bg; border-radius: 12px; padding: 20px; border` |
| `.content-card` | Post/content card | `surface bg; border-radius: 12px; border; overflow: hidden` |
| `.connect-card` | Platform connection card | `surface bg; border-radius: 12px; padding: 28px; text-align: center` |
| `.badge` | Platform badge | `padding: 3px 8px; border-radius: 6px; 11px; font-weight: 600; white` |
| `.badge-sm` | Small badge | `padding: 2px 6px; 10px; border-radius: 4px` |
| `.status-dot` | Health indicator | `8×8px circle; inline-block` |
| `.status-healthy` | Green dot | `background: #30d158; box-shadow glow` |
| `.status-degraded` | Orange dot | `background: #ff9f0a; box-shadow glow` |
| `.status-error` | Red dot | `background: #ff453a; box-shadow glow` |
| `.status-unknown` | Gray dot | `background: #8e8e93` |
| `.nav-item` | Sidebar nav link | `flex; gap: 12px; padding: 10px 14px; border-radius: 8px` |
| `.nav-item.active` | Active nav link | `background: rgba(10,132,255,0.15); color: #0a84ff` |
| `.platform-tab` | Analytics tab | `padding: 8px 18px; border-radius: 8px` |
| `.filter-pill` | Content filter | `padding: 6px 14px; border-radius: 20px` |
| `.theme-toggle` | Theme switcher | `flex; gap: 8px; padding: 8px 12px; border-radius: 8px` |
| `.fade-in` | Page entrance animation | `animation: fadeIn 0.2s ease` |
| `.xpst-sidebar` | Sidebar container | `width: 240px; min-height: 100vh; border-right` |
| `.xpst-main-content` | Main content area | `flex: 1; min-width: 0` |
| `.xpst-content-inner` | Content wrapper | `max-width: 1400px; margin: 0 auto` |
| `.xpst-hamburger` | Mobile menu button | `40×40px; border-radius: 10px; fixed; z-index: 1000` |
| `.xpst-overlay` | Mobile sidebar overlay | `fixed; full screen; rgba(0,0,0,0.5); z-index: 998` |
| `.settings-form-row` | Settings form layout | `flex; gap: 16px; flex-wrap: wrap` |
| `.settings-section-title` | Section label | `12px; uppercase; letter-spacing: 0.08em; border-bottom` |
| `.page-title` | Page heading | `clamp(1.4-1.75rem); font-weight: 700` |
| `.page-subtitle` | Page description | `clamp(0.8-0.875rem); text-muted` |
| `.section-header` | Section heading | `clamp(1-1.125rem); font-weight: 600` |
| `.thumb-placeholder` | Card thumbnail area | `16:9 ratio; border-radius: 8px 8px 0 0` |
