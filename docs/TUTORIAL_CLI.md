# xPST CLI Tutorial — Complete Command-Line Guide

> **In-depth tutorial for every xPST CLI command with examples.**
>
 xPST provides 37 top-level commands covering the entire cross-posting workflow.
> This guide explains each one with real examples, flags, and expected output.

---

## Table of Contents

- [Installation & First-Time Setup](#installation--first-time-setup)
- [Setup Wizard — `xpst setup`](#setup-wizard--xpst-setup)
- [Streamlined Connection — `xpst connect`](#streamlined-connection--xpst-connect)
- [Authentication — `xpst auth`](#authentication--xpst-auth)
- [Core Posting](#core-posting)
  - [`xpst run`](#xpst-run)
  - [`xpst watch`](#xpst-watch)
  - [`xpst post`](#xpst-post)
  - [`xpst backfill`](#xpst-backfill)
  - [`xpst delete`](#xpst-delete)
- [Scheduling](#scheduling)
  - [`xpst schedule add`](#xpst-schedule-add)
  - [`xpst schedule list`](#xpst-schedule-list)
  - [`xpst schedule remove`](#xpst-schedule-remove)
  - [`xpst schedule run`](#xpst-schedule-run)
  - [`xpst schedule install`](#xpst-schedule-install)
- [Analytics & Observability](#analytics--observability)
  - [`xpst analytics`](#xpst-analytics)
  - [`xpst analytics export`](#xpst-analytics-export)
  - [`xpst status`](#xpst-status)
  - [`xpst health`](#xpst-health)
  - [`xpst logs`](#xpst-logs)
  - [`xpst diagnostics`](#xpst-diagnostics)
  - [`xpst failures`](#xpst-failures)
- [Configuration Management](#configuration-management)
  - [`xpst config show`](#xpst-config-show)
  - [`xpst config set`](#xpst-config-set)
  - [`xpst config validate`](#xpst-config-validate)
  - [`xpst config fix`](#xpst-config-fix)
  - [`xpst config export` / `import`](#xpst-config-export--import)
- [State Management](#state-management)
  - [`xpst state export` / `import` / `backup`](#xpst-state-export--import--backup)
- [Readiness & Providers](#readiness--providers)
  - [`xpst readiness`](#xpst-readiness)
  - [`xpst providers`](#xpst-providers)
- [Knowledge Base — `xpst kb`](#knowledge-base--xpst-kb)
- [Surfaces](#surfaces)
  - [`xpst app`](#xpst-app)
  - [`xpst dashboard`](#xpst-dashboard)
  - [`xpst mcp`](#xpst-mcp)
- [Plugins — `xpst plugins`](#plugins--xpst-plugins)
- [Maintenance](#maintenance)
  - [`xpst update`](#xpst-update)
  - [`xpst version`](#xpst-version)
  - [`xpst build`](#xpst-build)
- [Exit Codes Reference](#exit-codes-reference)
- [JSON Output Mode for Scripting](#json-output-mode-for-scripting)
- [Dry-Run Mode](#dry-run-mode)

---

## Installation & First-Time Setup

### Install

```bash
# Recommended: install from source with uv
git clone https://github.com/TysAIs/xPST.git
cd xPST
uv venv && uv pip install -e ".[full]"
```

Or with pip:

```bash
git clone https://github.com/TysAIs/xPST.git
cd xPST
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[full]"
```

### Verify the install

```bash
xpst version
```

Expected output:

```
xPST v0.1.0

Dependencies:
  click         8.1.7
  rich          13.7.0
  yt-dlp       2025.1.1
  ...
```

### First-time setup

```bash
xpst setup       # interactive wizard
xpst health      # test connectivity (no uploads)
xpst run         # check for new videos and post
```

---

## Setup Wizard — `xpst setup`

The interactive first-time setup wizard walks you through connecting your platforms and writing `~/.xpst/config.yaml`.

```bash
xpst setup
```

The wizard will:
1. Ask which platforms you want to connect (YouTube, Instagram, X/Twitter, TikTok, and Threads)
2. Guide you through authentication for each selected platform
3. Create the `~/.xpst/` directory structure
4. Write a starter `config.yaml` with safe defaults
5. Run a health check to verify connectivity

**Example session:**

```
Welcome to xPST setup!

Which platforms would you like to connect?
  [x] YouTube   (OAuth 2.0 — official Data API v3)
  [x] Instagram (official Meta Graph API; instagrapi session fallback)
  [x] X/Twitter  (cookie-based)
  [x] TikTok    (OAuth 2.0 — official Content Posting API; source + destination)
  [x] Threads   (OAuth 2.0 — official Meta Threads API)

YouTube Authentication:
1. Go to Google Cloud Console: https://console.cloud.google.com
2. Enable YouTube Data API v3
3. Create OAuth 2.0 credentials
4. Download client_secrets.json
5. Save to: ~/.xpst/credentials/youtube_client_secrets.json

Press Enter when ready...

✅ YouTube connected
✅ Instagram connected
✅ X connected
✅ TikTok connected
✅ Threads connected

Setup complete! Run `xpst health` to verify, then `xpst run` to start.
```

---

## Streamlined Connection — `xpst connect`

A streamlined account connection wizard, faster than the full setup.

```bash
xpst connect              # connect all platforms interactively
xpst connect youtube       # connect YouTube only
xpst connect instagram     # connect Instagram only
xpst connect x             # connect X only
xpst connect tiktok        # connect TikTok (source + destination)
xpst connect --test        # test existing connections only
```

The `connect` wizard covers `tiktok`, `youtube`, `x`, `instagram`, `threads`, and
`messenger`.

Use `--test` to verify that your existing connections are still working without going through the wizard again.

**Example:**

```bash
$ xpst connect --test

Testing YouTube...  ✅ OK
Testing Instagram... ✅ OK
Testing X...       ✅ OK
Testing TikTok...  ✅ OK

All platforms connected and responding.
```

If any connection fails, the command exits with code 3 (authentication failure).

---

## Authentication — `xpst auth`

Authenticate with a specific platform or check auth status.

```bash
xpst auth youtube       # guide YouTube OAuth setup
xpst auth x              # guide X cookie setup
xpst auth instagram      # guide Instagram auth setup (Graph API or session)
xpst auth tiktok         # guide TikTok OAuth (Content Posting API) + source cookies
xpst auth threads        # guide Threads OAuth setup
xpst auth status         # show auth + quota status for all platforms
```

**Instagram auth modes.** The recommended/primary mode is the official Meta Graph
API (`auth_mode: "graph_api"`, using a `graph_access_token` and `graph_ig_user_id`).
An instagrapi session (`auth_mode: "session"`) is available as a fallback; it uses
the unofficial private API and carries account-ban risk.

**TikTok auth.** TikTok now posts as a destination via the official Content Posting
API. Destination auth is OAuth 2.0 (`client_key` / `client_secret` / `access_token`,
plus `refresh_token`); source downloads still use yt-dlp cookies.

### `xpst auth status`

Shows a table of platform authentication and quota status:

```bash
$ xpst auth status
```

```
xPST Authentication Status

Credential Storage: OS Keychain
Stored Credentials: 3
  🔑 youtube_token
  🔑 x_cookies
  🔑 instagram_session

Platform Status:
| Platform  | Auth | Quota (Daily) | Remaining | Details      |
|-----------|------|---------------|----------|--------------|
| YouTube   |  ✅  |       5       |     5     | Keyring      |
| X/Twitter |  ✅  |       5       |     5     | Keyring      |
| Instagram |  ✅  |       5       |     5     | Keyring      |
```

With `--json`:

```bash
$ xpst auth status --json
```

```json
{
  "credential_storage": "OS Keychain",
  "stored_credentials": ["youtube_token", "x_cookies", "instagram_session"],
  "platforms": {
    "youtube": { "authenticated": true, "quota_remaining": 5 },
    "x": { "authenticated": true, "quota_remaining": 5 },
    "instagram": { "authenticated": true, "quota_remaining": 5 }
  }
}
```

---

## Core Posting

### `xpst run`

The one-shot command: check a source for new videos and cross-post them to all connected destinations.

```bash
xpst run                          # default source: TikTok → all connected destinations
xpst run --source tiktok           # explicit TikTok source
xpst run --source local            # post from local files
xpst run --source all              # bidirectional: ALL sources → ALL destinations
xpst run --bidirectional           # same as --source all
xpst run --dry-run                # show what would happen without uploading
xpst run --json                   # machine-readable output
```

**Source choices:** `tiktok`, `youtube`, `x`, `instagram`, `local`, `all`

**Destinations:** xPST posts to up to five platforms — YouTube Shorts, Instagram Reels,
X/Twitter, TikTok, and Threads — for every connected destination not equal to
the source. The example outputs below show a few destinations for brevity; your run posts
to whichever destinations you have connected.

#### Example: default run

```bash
$ xpst run
```

```
xPST - Checking tiktok for new videos...

abc123 - ✅ Success
Caption: My latest video about cooking...
| Platform  | Status | URL/Error                              |
|-----------|--------|----------------------------------------|
| YouTube   | OK     | https://youtube.com/shorts/abc123      |
| Instagram | OK     | https://instagram.com/reel/abc123      |
| X         | OK     | https://x.com/i/status/123456          |
```

#### Example: dry run

```bash
$ xpst run --dry-run
```

```
Dry run — would post:
  abc123: My latest video about cooking... → youtube, instagram, x, threads
  def456: Another great video... → youtube, instagram, x, threads
```

(With a non-TikTok source, `tiktok` is also a destination. The list always reflects the
connected platforms other than the source.)

#### Example: bidirectional

```bash
$ xpst run --source all
```

```
xPST - Bidirectional cross-posting check...

In bidirectional mode, xPST monitors ALL connected sources and fans each new video out
to every other connected destination (YouTube Shorts, Instagram Reels, X, TikTok,
Threads):
- Post a Reel on Instagram → goes to YouTube Shorts, X, TikTok, and Threads
- Upload a Short on YouTube → goes to Instagram Reels, X, TikTok, and Threads
- Post a video on X → goes to YouTube Shorts, Instagram Reels, TikTok, and Threads
- Post on TikTok → goes to YouTube Shorts, Instagram Reels, X, and Threads

TikTok is both a source and a destination: it can be posted to via the official TikTok
Content Posting API (Direct Post), so it participates in cross-posting in both directions.

The engine deduplicates across platforms so content is not double-posted.
```

#### JSON output

```bash
$ xpst run --json | jq '.results[0]'
```

```json
{
  "video_id": "abc123",
  "caption": "My latest video about cooking...",
  "all_success": true,
  "partial_success": false,
  "platforms": {
    "youtube": { "success": true, "post_url": "https://...", "post_id": "abc123" },
    "instagram": { "success": true, "post_url": "https://...", "post_id": "abc123" },
    "x": { "success": true, "post_url": "https://...", "post_id": "123456" }
  }
}
```

---

### `xpst watch`

Continuous monitoring mode: checks for new videos on an interval and runs until you press Ctrl+C.

```bash
xpst watch                        # default: TikTok, every 900s (from config)
xpst watch --interval 300          # check every 5 minutes
xpst watch --source local          # watch local files
xpst watch --source all            # bidirectional watch (all sources)
xpst watch --bidirectional         # same as --source all
```

**Example:**

```bash
$ xpst watch --interval 300 --source local
```

```
xPST - Source: local watching every 300s (Ctrl+C to stop)

abc123 - ✅ Success
...

Next check in 300s...
Next check in 300s...
^C
Stopped by user
```

Features during watch:
- **Crash recovery** — checks for partially-completed uploads on startup and queues retries
- **Catch-up** — if your machine was asleep, it runs a catch-up cycle
- **Jittered intervals** — anti-bot timing adds randomness to the interval (bidirectional mode)

---

### `xpst post`

Manually post a local video file or carousel to platforms. Use multiple `--video` flags for a carousel.

```bash
# Single video to all platforms
xpst post -v ./my-video.mp4 -c "My awesome video!"

# Single video to specific platforms
xpst post -v ./my-video.mp4 -c "Check this out!" -p youtube,x

# Carousel (multiple files = carousel post)
xpst post -v ./img1.jpg -v ./img2.jpg -v ./img3.jpg -c "Swipe to see more!" -p instagram,x

# Dry run
xpst post -v ./my-video.mp4 -c "Test" --dry-run

# JSON output
xpst post -v ./my-video.mp4 -c "Test" --json
```

**Example:**

```bash
$ xpst post -v ./cooking-short.mp4 -c "3-minute pasta recipe! 🍝" -p youtube,instagram
```

```
Posting to: youtube, instagram

abc123 - ✅ Success
Caption: 3-minute pasta recipe! 🍝
| Platform  | Status | URL                                    |
|-----------|--------|----------------------------------------|
| YouTube   | OK     | https://youtube.com/shorts/abc123      |
| Instagram | OK     | https://instagram.com/reel/abc123      |
```

**Carousel example:**

```bash
$ xpst post -v ./slide1.jpg -v ./slide2.jpg -v ./slide3.jpg -c "Thread 🧵" -p x
```

```
Posting carousel (3 items) to: x

carousel123 - ✅ Success
```

---

### `xpst backfill`

Retry failed or incomplete posts from history. Scans the state for videos that didn't make it to all platforms and re-attempts them.

```bash
xpst backfill                     # retry all incomplete posts (limit 10)
xpst backfill --platforms youtube   # only backfill YouTube
xpst backfill --limit 50           # increase the limit
xpst backfill --dry-run           # show what would be backfilled
xpst backfill --json
```

**Example dry run:**

```bash
$ xpst backfill --dry-run
```

```
Dry run — 3 videos need backfilling:
  abc123 → instagram
  def456 → youtube, x
  ghi789 → x
```

---

### `xpst delete`

Delete a posted video from one or all platforms.

```bash
xpst delete abc123                          # delete from all platforms
xpst delete abc123 --platform youtube         # delete from YouTube only
xpst delete abc123 -p instagram,x             # delete from Instagram and X
xpst delete abc123 --yes                     # skip confirmation
xpst delete abc123 --json
```

**Example:**

```bash
$ xpst delete abc123 --platform youtube
```

```
Delete abc123 from youtube? [y/N]: y
  ✓ Deleted from youtube
```

---

## Scheduling

### `xpst schedule add`

Schedule a post for later publishing.

```bash
xpst schedule add video.mp4 --caption 'My video' --at '2026-06-20 10:00'
xpst schedule add video.mp4 -c 'My video' --at '2026-06-20T10:00:00' -p youtube,instagram
xpst schedule add video.mp4 -c 'Daily update' --at '2026-06-20 10:00' --repeat daily
```

**Time formats accepted:** `YYYY-MM-DD HH:MM`, `YYYY-MM-DDTHH:MM:SS`, `YYYY-MM-DD HH:MM:SS`

**Repeat rules:** `none`, `daily`, `weekly`, `monthly`

**Example:**

```bash
$ xpst schedule add ./promo.mp4 --caption "Big announcement!" --at '2026-06-25 14:00' -p youtube,x
```

```
✓ Scheduled post
  ID:        sched_a1b2c3
  File:      /path/to/promo.mp4
  Caption:   Big announcement!
  Time:      2026-06-25 14:00
  Platforms: youtube, x
```

---

### `xpst schedule list`

List all scheduled posts with their status.

```bash
xpst schedule list
xpst schedule list --json
```

**Example:**

```bash
$ xpst schedule list
```

```
Scheduled Posts
| ID          | File       | Caption              | Scheduled       | Platforms | Status   |
|-------------|------------|----------------------|-----------------|-----------|----------|
| sched_a1b2c3| promo.mp4  | Big announcement!   | 2026-06-25 14:00| youtube,x | pending  |
| sched_d4e5f6| daily.mp4  | Daily update        | 2026-06-20 10:00| all       | completed|
| sched_g7h8i9| failed.mp4 | Oops                | 2026-06-18 09:00| instagram | failed    |
```

---

### `xpst schedule remove`

Remove a scheduled post by ID.

```bash
xpst schedule remove sched_a1b2c3
xpst schedule remove sched_a1b2c3 --json
```

---

### `xpst schedule run`

Process all due scheduled posts. Typically called by cron or the OS scheduler, but can be run manually.

```bash
xpst schedule run                 # process all due posts
xpst schedule run --dry-run       # show what would be posted
xpst schedule run --json
```

**Example:**

```bash
$ xpst schedule run
```

```
Found 2 due post(s)
  ✓ sched_a1b2c3: posted successfully
  ⚠ sched_g7h8i9: partial — x: rate limit exceeded

Processed 2 scheduled post(s)
```

---

### `xpst schedule install`

Install an OS-level scheduler to run `xpst schedule run` periodically.

```bash
xpst schedule install                      # install (default: every 15 min)
xpst schedule install --interval 30        # every 30 minutes
xpst schedule install --remove            # uninstall the scheduler
```

- **macOS:** Creates a LaunchAgent at `~/Library/LaunchAgents/com.xpst.schedule.plist`
- **Linux:** Adds a crontab entry
- **Windows:** Creates a Scheduled Task named `XpstScheduleRun`

**Example on macOS:**

```bash
$ xpst schedule install --interval 15
```

```
✓ LaunchAgent installed: ~/Library/LaunchAgents/com.xpst.schedule.plist
  Runs every 15 minutes
  Logs: ~/.xpst/logs/launchagent.log
  Uninstall: xpst schedule install --remove
```

---

## Analytics & Observability

### `xpst analytics`

Show cross-platform engagement metrics (views, likes, comments, shares) for all your cross-posted videos.

```bash
xpst analytics                    # show summary for all platforms
xpst analytics -p youtube,x        # specific platforms only
xpst analytics --refresh           # force refresh (ignore cache)
xpst analytics --json
```

**Example:**

```bash
$ xpst analytics
```

```
xPST - Cross-Platform Analytics

Fetching analytics for 47 posts across 3 platforms...

Platform Analytics
| Platform  | Posts | Views   | Likes  | Comments | Shares |
|-----------|-------|---------|---------|----------|---------|
| YouTube   |   20  |  12,345 |  1,234  |    156    |    -    |
| Instagram |   15  |   8,901 |ibu89    |     89    |    45   |
| X         |   12  |   5,678 |  567     | 78 (replies)| 234 (RTs)|
| **TOTAL** | **47** |**26,924**|**2,890**|  **323**  | **279**|

Top Posts by Views:
| # | Platform | Post ID     | Views  | Likes | Comments |
|---|----------|-------------|---------|-------|----------|
| 1 | YouTube  | abc123...   | 5,432   | 5432 |
```

---

### `xpst analytics export`

Export analytics data to a JSON or CSV file.

```bash
xpst analytics export -o ./analytics.json
xpst analytics export -o ./analytics.csv --format csv
xpst analytics export -o ./data.json -p youtube --refresh
```

---

### `xpst status`

Show cross-posting statistics and overall health status.

```bash
xpst status
xpst status --json
```

Shows: total videos posted, per-platform breakdown, quota remaining, last check time, and any issues.

---

### `xpst health`

Test connectivity to all platforms and sources. **No uploads are made.**

```bash
xpst health
xpst health --json
```

**Example:**

```bash
$ xpst health
```

```
xPST Health Check

YouTube:       ✅ OK (authenticated, quota: 5/5)
Instagram:    ✅ OK (authenticated, quota: 5/5)
X:            ✅ OK (authenticated, quota: 5/5)
TikTok:       ✅ OK (authenticated — source + Content Posting API)
Threads:      ✅ OK (authenticated, quota: 5/5)

All platforms healthy.
```

If any platform fails, the exit code is `10` (platform unavailable).

---

### `xpst logs`

View recent log entries (last 50 lines by default).

```bash
xpst logs
xpst logs --json
```

---

### `xpst diagnostics`

Export a redacted local diagnostics bundle as a zip file — useful for filing bug reports.

```bash
xpst diagnostics                       # creates a timestamped zip
xpst diagnostics -o ./report.zip       # specify output path
xpst diagnostics --log-lines 500      # include more log lines
```

```
Diagnostics bundle written: ~/.xpst/diagnostics-20260619-143022.zip
Review diagnostics.json before sharing if logs may contain private details.
```

---

### `xpst failures`

Inspect and retry failed uploads from the dead-letter queue.

```bash
xpst failures list                     # list all failed uploads
xpst failures list --json
xpst failures retry abc123 --platform x  # retry a specific failure
```

**Example:**

```bash
$ xpst failures list
```

```
Failed Uploads (dead-letter queue)
| Video   | Platform  | Error                        | Count |
|---------|-----------|------------------------------|-------|
| abc123  | x         | Rate limit exceeded           |   3   |
| def456  | instagram | Session expired, re-auth needed|  1   |

Retry one with: xpst failures retry <video_id> --platform <name>
```

```bash
$ xpst failures retry abc123 --platform x
```

```
Retrying abc123 on x from abc123_source.mp4...
Retry succeeded: https://x.com/i/status/123456
```

---

## Configuration Management

### `xpst config show`

Display the current configuration as YAML. Sensitive values (passwords, tokens, secrets) are masked by default.

```bash
xpst config show
xpst config show --raw         # show raw values (no masking)
xpst config show --json
```

---

### `xpst config set`

Set a configuration value using dotted keys. Supports booleans, integers, floats, and strings.

```bash
xpst config set accounts.youtube.enabled true
xpst config set rate_limits.youtube 10
xpst config set monitoring.log_level DEBUG
xpst config set schedule.check_interval 600
```

```
✓ Set rate_limits.youtube = 10
```

---

### `xpst config validate`

Validate configuration for errors. Checks required fields, path existence, and platform config validity.

```bash
xpst config validate
xpst config validate --json
```

Exit code `0` if valid, `2` if invalid (config error).

**Example:**

```bash
$ xpst config validate
```

```
Configuration Validation
| Check                           | Status | Details                    |
|--------------------------------|---------|---------------------------|
| Config file loaded             |  PASS   | OK                        |
| Config file exists             |  PASS   | ~/.xpst/config.yaml       |
| Download directory accessible  |  PASS   | ~/.xpst/downloads/other   |
| YouTube credentials            |  PASS   | ~/.xpst/credentials/...   |
| Instagram credentials          |  FAIL   | session not configured     |
| YouTube rate limit             |  PASS   | 5                         |

5 of 6 checks passed ✓
```

---

### `xpst config fix`

Detect and auto-fix common configuration issues: missing credentials directory, stale paths, invalid ports, missing required fields.

```bash
xpst config fix
xpst config fix --yes          # apply fixes without confirmation
xpst config fix --json
```

---

### `xpst config export` / `import`

Export and import configuration to/from files.

```bash
# Export (masked by default)
xpst config export ./my-config-backup.yaml
xpst config export ./my-config-backup.yaml --raw   # unmasked

# Import (merge by default)
xpst config import ./my-config.yaml
xpst config import ./my-config.yaml --replace       # replace entirely
xpst config import ./my-config.yaml --yes            # skip confirmation
xpst config import ./my-config.yaml --strict         # fail on warnings
```

Import shows a diff of changes before applying:

```
Added keys:
  + rate_limits.tiktok
Changed values:
  ~ schedule.check_interval: 900 → 600

Apply these changes? (y/n): y
✓ Config merged from ./my-config.yaml
```

---

## State Management

State (`~/.xpst/state.json`) is the single source of truth for what has been posted. Losing it means the next watch cycle re-posts the recent catalog publicly. These commands make it durable.

### `xpst state export` / `import` / `backup`

```bash
# Export state to a file (validated copy)
xpst state export ./state-backup.json

# Import state from a file (current state is backed up first)
xpst state import ~/state-from-other-machine.json
xpst state import ~/state.json --yes    # skip confirmation

# Snapshot state with rotation (keeps last 10 by default)
xpst state backup
xpst state backup --keep 20             # retain 20 backups
```

**Example:**

```bash
$ xpst state export ./state-backup.json
Exported ~/.xpst/state.json -> ./state-backup.json

$ xpst state import ./state-backup.json
Current state backed up to state.json.pre-import-20260619-143022
Imported 47 posted-video records. Review with `xpst status` before the next run.
```

---

## Readiness & Providers

### `xpst readiness`

Show first-run readiness and recommended next actions. Useful after a fresh install or after changing config.

```bash
xpst readiness
xpst readiness --fix        # create missing local folders and save safe defaults
xpst readiness --json
```

---

### `xpst providers`

Show the supported source and destination provider catalog with capabilities.

```bash
xpst providers
xpst providers --json
```

**Example:**

```bash
$ xpst providers
```

```
Sources (where xPST pulls from):
| Provider   | Type     | Capabilities              |
|------------|----------|--------------------------|
| TikTok     | download | HD downloads, cookies      |
| YouTube    | download| Channel/video downloads     |
| Instagram  | download| Session-based listing       |
| X          | download| yt-dlp with twikit metadata|
| Local      | files    | Folders, videos, carousels  |

Destinations (where xPST posts):
| Provider   | API type                          | Auth method           |
|------------|-----------------------------------|----------------------|
| YouTube    | Data API v3 (official)            | OAuth 2.0            |
| Instagram  | Meta Graph API (official, primary); instagrapi session (fallback) | OAuth token / Session |
| X          | twikit (unofficial)               | Cookies              |
| TikTok     | Content Posting API — Direct Post (official) | OAuth 2.0     |
| Threads    | Meta Threads API (official)       | OAuth 2.0            |
```

> **TikTok now posts as a destination.** It uses the official TikTok Content Posting
> API (Direct Post), so TikTok is both a source and a destination.

---

## Knowledge Base — `xpst kb`

The knowledge base ingests your videos, transcribes them, extracts cited knowledge "nuggets," embeds them, and stores them locally for semantic search.

> **Requires the `knowledge` extra:** `pip install "xpst[knowledge]"`

### `xpst kb add`

Ingest a local file or URL: transcribe, extract cited nuggets, embed, and store.

```bash
xpst kb add ./my-video.mp4
xpst kb add https://youtube.com/watch?v=abc123
xpst kb add ./my-video.mp4 --workspace my-project
```

### `xpst kb query`

Semantic search over your stored content. Returns cited nuggets with provenance and similarity scores.

```bash
xpst kb query "cooking techniques"
xpst kb query "what did I say about marketing?" --limit 10
xpst kb query "Pasta recipes" --json
```

Queries are embedded and vector-searched against the store, with an automatic substring-match fallback when embeddings are unavailable. Every hit carries provenance (source URL, timestamps) and a similarity score.

### `xpst kb organize`

Discover knowledge areas, tag difficulty, and assign nuggets to areas.

```bash
xpst kb organize
xpst kb organize --threshold 0.75
xpst kb organize --workspace my-project
```

### `xpst kb areas`

List discovered knowledge areas in course order (beginner → advanced).

```bash
xpst kb areas
```

### `xpst kb course`

Emit the organized, cited outline as a course.

```bash
xpst kb course
```

### `xpst kb doctor`

Read-only health check of the knowledge workspace.

```bash
xpst kb doctor
```

### `xpst kb reembed`

Re-embed all nuggets with the currently configured embedding model (useful after model changes).

```bash
xpst kb reembed
xpst kb reembed --force
```

### `xpst kb migrate-store`

Migrate the store format (run after upgrading xPST if the store schema changed).

```bash
xpst kb migrate-store
```

---

## Surfaces

### `xpst app`

Launch the native desktop app (PySide6/QML). It appears in your dock/taskbar.

```bash
xpst app
xpst app --no-splash     # skip the splash screen
```

If PySide6 is not installed, `xpst app` prints an install hint and exits
gracefully:

```bash
Desktop app not installed. Run: pip install xpst[desktop]
```

See [TUTORIAL_APP.md](TUTORIAL_APP.md) for the full desktop app walkthrough.

---

### `xpst dashboard`

Launch the local web API dashboard at `http://127.0.0.1:8080`.

```bash
xpst dashboard
xpst dashboard --port 9000 --host 0.0.0.0
```

The dashboard provides:
- Upload history
- Per-platform health
- Analytics charts
- Quota tracking
- Dead-letter queue of failed posts
- Real-time updates via WebSocket

By default, binds to `127.0.0.1` (loopback only) for security.

---

### `xpst mcp`

Start the MCP (Model Context Protocol) server over stdio. Used by AI agents (Claude Desktop, Claude Code, etc.).

```bash
xpst mcp
```

Or use the dedicated entry point:

```bash
xpst-mcp
```

See [TUTORIAL_MCP.md](TUTORIAL_MCP.md) for the full MCP walkthrough.

---

## Plugins — `xpst plugins`

Manage xPST plugins. Place `.py` files in `~/.xpst/plugins/` to add custom uploaders and sources.

```bash
xpst plugins list                      # list installed plugins
xpst plugins docs                       # generate markdown docs for all plugins
xpst plugins docs -o ./plugin-docs.md   # write docs to a file
```

---

## Maintenance

### `xpst update`

Update xPST dependencies to latest versions.

```bash
xpst update                # update all dependencies
xpst update --check        # check for updates without installing
xpst update --components    # show app, helper, and provider metadata update status
```

---

### `xpst version`

Show xPST version and all dependency versions.

```bash
xpst version
xpst version --json
```

**JSON output:**

```json
{
  "xpst": "0.1.0",
  "python": "3.11.15",
  "click": "8.1.7",
  "rich": "13.7.0",
  "yt-dlp": "2025.1.1"
}
```

---

### `xpst build`

Build a standalone executable using PyInstaller.

```bash
xpst build                    # build for current OS
xpst build --target macos      # build for macOS
xpst build --target windows     # cross-compile for Windows (via Docker)
xpst build --target linux       # build for Linux
xpst build --spec-file ./custom.spec
```

Cross-compilation requires Docker. The command auto-detects the appropriate `.spec` file (`build_macos.spec`, `build_windows.spec`, `build_linux.spec`).

---

## Exit Codes Reference

xPST uses meaningful exit codes for scripting and agent integration:

| Code | Constant | Meaning |
|------|----------|---------|
| `0` | `EXIT_SUCCESS` | Success |
| `1` | `EXIT_GENERAL` | General error |
| `2` | `EXIT_CONFIG_ERROR` | Configuration error (also Click usage errors) |
| `3` | `EXIT_AUTH_FAILURE` | Authentication failure |
| `4` | `EXIT_RATE_LIMIT` | Rate limit exceeded |
| `10` | `EXIT_PLATFORM_UNAVAILABLE` | Platform unavailable |

Use these in shell scripts:

```bash
if xpst run --quiet; then
    echo "Posted successfully"
elif [ $? -eq 3 ]; then
    echo "Auth failure — run xpst auth"
elif [ $? -eq 4 ]; then
    echo "Rate limited — wait or increase rate_limits"
fi
```

---

## JSON Output Mode for Scripting

All commands accept `--json` for machine-readable output. Additionally, the CLI **auto-enables JSON mode when stdout is not a TTY** (piped to another process).

This makes xPST ideal for scripting and agent integration:

```bash
# Pipe to jq for filtering
xpst status --json | jq '.stats.posted'
xpst run --dry-run --json | jq '.videos[].video_id'
xpst analytics --json | jq '.platforms.youtube'

# Use in shell scripts
POSTED=$(xpst status --json | jq '.stats.posted')
if [ "$POSTED" -gt 0 ]; then
    echo "You have $POSTED videos cross-posted"
fi

# Combine with other tools
xpst health --json | jq '.platforms | to_entries | map(select(.value.ok == false)) | .[].key'
```

When stdout is a pipe, you can omit `--json`:

```bash
xpst status | jq .          # auto JSON because piped
xpst run | jq .             # auto JSON because piped
```

---

## Dry-Run Mode

All posting commands support `--dry-run` to preview what would happen without uploading. This is the safest way to see what xPST would do before committing.

```bash
xpst run --dry-run                     # preview cross-posting from default source
xpst run --dry-run --source all         # preview bidirectional cross-posting
xpst post -v ./video.mp4 -c "Test" --dry-run
xpst backfill --dry-run
xpst schedule run --dry-run
```

Dry-run mode makes **no network calls and no uploads**. It only fetches metadata (for `run`) to show what would be posted, or reads local state (for `backfill`) to show what would be retried.

**Example:**

```bash
$ xpst run --dry-run --source tiktok
```

```
Dry run — would post:
  abc123: My latest video about cooking... → youtube, instagram, x, threads
  def456: Another great video... → youtube, instagram, x, threads
```

The destination list reflects the connected platforms other than the source (here, TikTok).
With all five platforms connected, a non-TikTok source would also include `tiktok`.

```bash
$ xpst run --dry-run --json --source tiktok | jq '.videos | length'
2
```

---

## Next Steps

- **Desktop app walkthrough:** [TUTORIAL_APP.md](TUTORIAL_APP.md)
- **MCP integration guide:** [TUTORIAL_MCP.md](TUTORIAL_MCP.md)
- **Architecture details:** [ARCHITECTURE.md](ARCHITECTURE.md)
- **Per-platform auth guides:** [QUICKSTART.md](QUICKSTART.md)
