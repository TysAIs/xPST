# xPST Desktop App Tutorial

> **Complete walkthrough** of the xPST PySide6/QML desktop application — from first launch to advanced workflows.

---

## Table of Contents

1. [Installation](#installation)
2. [First Launch](#first-launch)
3. [Connecting Platforms](#connecting-platforms)
4. [Composing & Posting](#composing--posting)
5. [Managing Content](#managing-content)
6. [Viewing Analytics](#viewing-analytics)
7. [Scheduling Posts](#scheduling-posts)
8. [Settings & Customization](#settings--customization)
9. [Keyboard Shortcuts](#keyboard-shortcuts)
10. [Troubleshooting](#troubleshooting)

---

## Installation

### Prerequisites

- Python 3.10+
- FFmpeg installed and on your PATH (`brew install ffmpeg` on macOS, `apt install ffmpeg` on Linux)
- Platform-specific requirements (see [Connecting Platforms](#connecting-platforms))

### Install with Desktop Support

```bash
pip install 'xpst[desktop]'
```

### Launch the App

```bash
xpst app
```

Or from source:

```bash
cd xPST
source .venv/bin/activate
python -m xpst app
```

---

## First Launch

When you first open xPST, you'll see the **Dashboard** page. It will show empty states because no platforms are connected and no content has been posted yet.

The sidebar on the left provides navigation to all 8 pages:

| Page | Icon | Purpose |
|------|------|---------|
| **Dashboard** | Grid icon | Overview of posts, reach, and platform health |
| **Compose** | Edit icon | Create and post new content from local files |
| **Content** | Film icon | Browse and manage posted content |
| **Analytics** | Chart icon | Cross-platform performance metrics |
| **Connect** | Link icon | Connect social media platforms |
| **Schedule** | Calendar icon | Schedule posts for future publishing |
| **Settings** | Gear icon | Configure preferences, notifications, MCP |
| **About** | Info icon | Version info, links, licenses |

### First-Run Setup

1. **Go to Connect** — Click "Connect" in the sidebar
2. **Follow the setup checklist** — The page shows a readiness checklist with blocking items
3. **Set your local content path** — Tell xPST where your video files live
4. **Connect at least one platform** — See [Connecting Platforms](#connecting-platforms) below

---

## Connecting Platforms

The **Connect** page has detailed setup guides for each platform. Here's a summary:

### YouTube (OAuth 2.0)

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a project and enable **YouTube Data API v3**
3. Create OAuth 2.0 credentials (Desktop app type)
4. Download the `client_secret_*.json` file
5. Place it at `~/.xpst/credentials/youtube_client_secret.json`
6. Click **Connect YouTube** in the app — a browser window opens for OAuth consent
7. Authorize the app — your token is stored encrypted locally

> **Note:** 2FA/2SV on your Google account works fine — the OAuth flow handles it. Default quota: 10,000 units/day.

### Instagram (Meta Graph API, session fallback)

The **primary** Instagram auth is the official Meta Graph API (`auth_mode: graph_api`), using a `graph_access_token` and `graph_ig_user_id`. The instagrapi session method (`auth_mode: session`) is an unofficial **fallback** that carries ban risk.

**Graph API (recommended):**
1. Configure your `graph_access_token` and `graph_ig_user_id` in `~/.xpst/config.yaml`

**Session fallback:**
1. Log into [instagram.com](https://instagram.com) in your browser first
2. Enter your Instagram username and password in the Connect page fields
3. Click **Connect Instagram**
4. Credentials are stored encrypted in `~/.xpst/credentials/`

> **Note:** Use a dedicated account for the session method. Max caption 2200 chars. Carousel uploads support up to 10 images/videos.

### X / Twitter (Cookie-based)

1. Log into [x.com](https://x.com) in your browser
2. Export cookies using a browser extension (e.g., EditThisCookie) or DevTools
3. Place cookies JSON at `~/.xpst/credentials/x_cookies.json`
4. Or click **Paste Cookies** in the app and paste the JSON
5. Alternatively, run `xpst auth x` in terminal for guided setup

> **Note:** X destination uses twikit cookies (community/unofficial mode). Max caption 280 chars, video up to 140s. Carousels post as threads.

### TikTok (Source + Destination)

TikTok works as both a **source** (xPST monitors it for new videos to cross-post) and a **destination** — TikTok now supports posting via the official **Content Posting API (Direct Post)**.

1. Log into [tiktok.com](https://tiktok.com) in your browser to enable cookie-based source downloads (HD / no-watermark)
2. Export cookies to `~/.xpst/credentials/tiktok_cookies.json`, or run `xpst auth tiktok` in terminal
3. To post TO TikTok, configure the official Content Posting API (OAuth 2.0 with `client_key` / `client_secret` / `access_token`)

> **Note:** TikTok destination max caption 2200 chars, 6 posts/min.

### Threads (OAuth)

Threads is a **destination** using the official Meta Threads API (container-publish model).

1. Configure OAuth credentials for the Meta Threads API
2. Run `xpst auth threads` or set credentials in `~/.xpst/config.yaml`

> **Note:** Max caption 500 chars, video up to 300s, 250 posts/day.

### LinkedIn (OAuth)

LinkedIn is a **destination** using the official LinkedIn `/v2/posts` endpoint.

1. Configure OAuth credentials for the LinkedIn API
2. Run `xpst auth linkedin` or set credentials in `~/.xpst/config.yaml`

> **Note:** Max caption 3000 chars, 150 posts/day.

---

## Composing & Posting

The **Compose** page is where you create new posts from local video files.

### Step-by-Step

1. **Select a video folder** — Click "Browse" to pick a folder containing your videos
2. **Choose a video** — The grid shows thumbnails of all video files in the folder
3. **Write a caption** — Enter your post caption in the text area (character count shown)
4. **Select platforms** — Check the boxes for which platforms to post to (YouTube, Instagram, X/Twitter, TikTok, Threads, LinkedIn)
5. **Click "Post Now"** — The upload begins

### Upload Progress

- Per-platform progress bars show upload status (0% → 100%)
- The progress overlay appears at the bottom of the window
- Each platform shows success/failure when complete

### Post Results

After posting, you'll see:
- ✅ **Success** — Post URL is displayed
- ❌ **Failure** — Error message is shown
- ⚠️ **Partial** — Some platforms succeeded, others failed

---

## Managing Content

The **Content** page shows all your posted videos.

### Features

- **Grid view** of all posted content with thumbnails
- **Filter** by platform, date, or status
- **Search** by caption or video ID
- **Click any post** to view details in the DetailPanel
- **Delete** posts from specific platforms
- **Edit** captions for existing posts
- **Checkbox** selection for bulk operations

### Detail Panel

Clicking a post opens the **Detail Panel** which shows:
- Video preview/thumbnail
- Full caption and metadata
- Per-platform tabs with analytics (views, likes, comments, shares)
- Post URLs (clickable)
- Delete button per platform

---

## Viewing Analytics

The **Analytics** page provides cross-platform performance metrics.

### Features

- **Platform selector** — View all platforms or filter to one
- **Date range picker** — Filter by week, month, or all time
- **Compare mode** — Toggle to compare current vs. previous period
- **Metric cards** — Total views, likes, comments, shares
- **Bar charts** — Per-platform breakdown of each metric
- **Trending** — Shows which platforms are growing

> **Note:** Analytics data comes from platform APIs. Some platforms may have delayed metrics.

---

## Scheduling Posts

The **Schedule** page lets you plan future posts.

### Creating a Schedule

1. **Select a video** — Pick from your local content
2. **Write a caption** — Enter the post caption
3. **Choose platforms** — Select which platforms to post to
4. **Set date and time** — When the post should go live
5. **Set recurrence** — One-time, daily, or weekly
6. **Click "Schedule"** — The post is added to the schedule

### Managing Scheduled Posts

- View all scheduled posts in a list
- See countdown to next scheduled post
- Delete scheduled posts
- Calendar view shows posts on their scheduled dates

> **Note:** The scheduler runs while xPST is open. For 24/7 scheduling, use `xpst watch` in terminal.

---

## Settings & Customization

The **Settings** page has several sections:

### General

- **Dark mode** toggle
- **Language** selector (supports i18n)
- **Local content path** — Default folder for video files

### Notifications

- **Upload completion** — Notify when a post finishes uploading
- **Upload errors** — Notify when a post fails
- **Rate limit warnings** — Notify when approaching platform limits

### Rate Limits

- **Posts per window** — Max posts per time period
- **Window duration** — Time period in minutes
- Per-platform rate limit overrides

### MCP Server

- **Start/Stop** the MCP server from the UI
- Port configuration
- Read-only mode toggle
- Confirmation requirement for mutating tools

### Keyboard Shortcuts

- View and customize all keyboard shortcuts
- Shortcuts persist across sessions

---

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl+R` | Refresh data |
| `Ctrl+Q` | Quit |
| `Ctrl+,` | Open Settings |
| `Ctrl+D` | Go to Dashboard |
| `Ctrl+C` | Go to Compose |
| `Ctrl+N` | Go to Content |
| `Ctrl+A` | Go to Analytics |
| `Ctrl+S` | Go to Schedule |

> Shortcuts are customizable in Settings → Keyboard Shortcuts.

---

## Troubleshooting

### App won't launch

```bash
# Check PySide6 is installed
pip show PySide6

# Try launching from terminal for error output
python -m xpst app --no-splash
```

### FFmpeg not found

```bash
# macOS
brew install ffmpeg

# Linux
sudo apt install ffmpeg

# Or set the path explicitly
export XPST_FFMPEG_PATH=/path/to/ffmpeg
```

### Platform connection fails

- **YouTube**: Ensure the OAuth consent screen is configured. Check that `youtube_client_secret.json` is at `~/.xpst/credentials/`
- **Instagram**: For the Graph API, verify `graph_access_token` and `graph_ig_user_id` are valid. For the session fallback, make sure you're logged into instagram.com in your browser first
- **X**: Verify cookies are valid and not expired. Re-export if needed
- **TikTok**: For source downloads, cookies expire — re-export periodically. For posting, ensure the Content Posting API OAuth tokens are valid
- **Threads**: Verify the Meta Threads API OAuth credentials are valid
- **LinkedIn**: Verify the LinkedIn API OAuth credentials are valid

### Thumbnails not showing

- Ensure FFmpeg is installed (used for frame extraction)
- Check that video files are accessible (not on an unmounted drive)

### State corruption

If the app behaves unexpectedly:

```bash
# Back up state
xpst state export --output ~/xpst-backup.json

# Reset state
rm ~/.xpst/state.json

# Restart app
xpst app
```

### Crash recovery

If the app crashes mid-upload, xPST will:
1. Detect the incomplete upload on next launch
2. Show a crash recovery dialog
3. Offer to retry or skip the failed posts

This prevents double-posting — already-uploaded posts are tracked in state.

---

## Getting Help

- 📖 [CLI Tutorial](TUTORIAL_CLI.md)
- 🤖 [MCP Tutorial](TUTORIAL_MCP.md)
- 🐛 [Report Issues](https://github.com/TysAIs/xPST/issues)
- 📚 [Documentation](https://github.com/TysAIs/xPST#readme)
