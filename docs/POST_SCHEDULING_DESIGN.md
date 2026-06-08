# xPST Post Scheduling System — Design Document

## 1. Executive Summary

Add a post scheduling system to xPST that lets users queue videos for future posting at specific dates/times, with timezone awareness, anti-bot time window enforcement, and persistence across restarts.

### Current State
- **`scheduler.py`**: Watch-mode only — polls for new TikTok videos at a fixed interval. No concept of scheduled/delayed posts.
- **`cli.py`**: `xpst post` posts immediately. No `--at` or `--schedule` option.
- **`anti_bot.py`**: Already enforces 8am–11pm posting window via `should_post_now()`.
- **`state.py`**: JSON-based persistent state with atomic writes, backup rotation, corruption recovery.
- **`dashboard/app.py`**: NiceGUI dashboard with Overview, Content, Analytics, Connect, Settings pages. No scheduling UI.
- **`engine.py`**: `CrossPostEngine.post_manual()` handles one-off uploads. `check_and_post()` handles watch-mode source fetching.

---

## 2. Architecture

### Approach: Custom Async Scheduler (zero new dependencies)

**Rationale:**
- xPST already has a robust state persistence layer (`StateManager` with atomic JSON writes)
- APScheduler adds a dependency and complexity (job stores, executors, triggers) that isn't needed
- A lightweight async timer loop is simpler, easier to debug, and fits the existing architecture
- The existing `Scheduler` class in `scheduler.py` already uses a simple poll loop — we extend it

### Data Flow

```
User Input (CLI or Dashboard)
    │
    ▼
ScheduleStore (JSON persistence at ~/.xpst/schedule.json)
    │
    ▼
PostScheduler (asyncio event loop, checks every 30s)
    │
    ├── Validates: is post time reached?
    ├── Validates: are we in posting hours (8am–11pm)?
    ├── Validates: daily rate limits not exceeded?
    │
    ▼
CrossPostEngine.post_manual() or post_manual_carousel()
    │
    ▼
UploadService → Platform Uploaders
```

---

## 3. New Module: `src/xpst/schedule_store.py`

### Purpose
Persistent storage for scheduled posts. JSON file at `~/.xpst/schedule.json`.

### Data Model

```python
@dataclass
class ScheduledPost:
    id: str                    # UUID
    video_paths: list[str]     # Absolute paths to video/image files
    caption: str               # Post caption
    platforms: list[str]       # Target platforms (e.g., ["youtube", "instagram", "x"])
    scheduled_time: str        # ISO 8601 with timezone (e.g., "2026-06-08T14:30:00-07:00")
    status: str                # "pending" | "running" | "completed" | "failed" | "cancelled"
    created_at: str            # ISO 8601 timestamp
    updated_at: str            # ISO 8601 timestamp
    completed_at: str | None   # ISO 8601 timestamp when finished
    error: str | None          # Error message if failed
    result_summary: str | None # e.g., "Posted to youtube, instagram"
    retry_count: int = 0       # Number of retries attempted
    max_retries: int = 2       # Max retries before marking failed
```

### File Format (`~/.xpst/schedule.json`)

```json
{
  "version": 1,
  "timezone": "America/Los_Angeles",
  "posts": [
    {
      "id": "a1b2c3d4-...",
      "video_paths": ["/Users/alice/video.mp4"],
      "caption": "Check out my new video!",
      "platforms": ["youtube", "instagram", "x"],
      "scheduled_time": "2026-06-08T14:30:00-07:00",
      "status": "pending",
      "created_at": "2026-06-07T10:00:00-07:00",
      "updated_at": "2026-06-07T10:00:00-07:00",
      "completed_at": null,
      "error": null,
      "result_summary": null,
      "retry_count": 0,
      "max_retries": 2
    }
  ]
}
```

### Key Methods

```python
class ScheduleStore:
    def __init__(self, config_dir: str = "~/.xpst"):
        """Load or create schedule.json"""

    def add(self, post: ScheduledPost) -> str:
        """Add a scheduled post. Returns post ID."""

    def get(self, post_id: str) -> ScheduledPost | None:
        """Get a scheduled post by ID."""

    def list_all(self, status: str | None = None) -> list[ScheduledPost]:
        """List all posts, optionally filtered by status."""

    def list_pending(self) -> list[ScheduledPost]:
        """List all pending posts sorted by scheduled_time."""

    def update(self, post_id: str, **kwargs) -> bool:
        """Update fields on a scheduled post."""

    def cancel(self, post_id: str) -> bool:
        """Cancel a pending post."""

    def delete(self, post_id: str) -> bool:
        """Delete a post (any status)."""

    def mark_running(self, post_id: str) -> None: ...
    def mark_completed(self, post_id: str, summary: str) -> None: ...
    def mark_failed(self, post_id: str, error: str) -> None: ...

    def get_due_posts(self) -> list[ScheduledPost]:
        """Return posts whose scheduled_time <= now and status is 'pending'."""

    def save(self) -> None:
        """Atomic write to schedule.json (same pattern as StateManager)."""
```

### Persistence Strategy
- Same atomic-write pattern as `StateManager`: write to `.tmp`, then `os.replace()`
- File locking via `fcntl`/`msvcrt` for cross-process safety
- Backup rotation (keep last 3 backups)

---

## 4. New Module: `src/xpst/post_scheduler.py`

### Purpose
Asyncio-based scheduler that polls the `ScheduleStore` and executes due posts.

### Design

```python
class PostScheduler:
    """
    Async scheduler for executing scheduled posts.

    Polls ScheduleStore every 30 seconds for due posts.
    Respects anti-bot time windows and rate limits.
    Runs as a background task alongside watch mode or standalone.
    """

    def __init__(self, engine: CrossPostEngine, config: XPSTConfig):
        self.engine = engine
        self.config = config
        self.store = ScheduleStore(config.config_dir)
        self.anti_bot = engine.anti_bot
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self):
        """Start the scheduler loop as a background task."""

    async def stop(self):
        """Gracefully stop the scheduler."""

    async def _loop(self):
        """Main loop: check for due posts every 30 seconds."""
        while self._running:
            await self._check_and_execute()
            await asyncio.sleep(30)

    async def _check_and_execute(self):
        """Check for due posts and execute them."""
        due_posts = self.store.get_due_posts()
        for post in due_posts:
            # Re-check posting hours (scheduled time may be in-window
            # but actual execution may be delayed)
            if not self.anti_bot.should_post_now():
                logger.info("Outside posting hours, deferring %s", post.id)
                continue  # Will retry next cycle

            # Check daily rate limits for each target platform
            for platform in post.platforms:
                if not self.anti_bot.can_upload(platform):
                    logger.warning("Rate limit for %s, deferring %s", platform, post.id)
                    # Don't skip entirely — other platforms may still be OK

            await self._execute_post(post)

    async def _execute_post(self, post: ScheduledPost):
        """Execute a single scheduled post."""
        self.store.mark_running(post.id)
        try:
            media_paths = [Path(p) for p in post.video_paths]
            if len(media_paths) > 1:
                result = await self.engine.post_manual_carousel(
                    media_paths, post.caption, post.platforms
                )
            else:
                result = await self.engine.post_manual(
                    media_paths[0], post.caption, post.platforms
                )

            if result.all_success:
                self.store.mark_completed(post.id, "All platforms succeeded")
            elif result.partial_success:
                succeeded = [p for p, r in result.results.items() if r.success]
                self.store.mark_completed(post.id, f"Partial: {', '.join(succeeded)}")
            else:
                errors = [f"{p}: {r.error}" for p, r in result.results.items() if r.error]
                if post.retry_count < post.max_retries:
                    post.retry_count += 1
                    self.store.update(post.id, retry_count=post.retry_count, status="pending")
                    # Reschedule for 30 minutes from now
                    from datetime import timedelta
                    new_time = datetime.now(timezone.utc) + timedelta(minutes=30)
                    self.store.update(post.id, scheduled_time=new_time.isoformat())
                else:
                    self.store.mark_failed(post.id, "; ".join(errors))

        except Exception as e:
            logger.error("Failed to execute scheduled post %s: %s", post.id, e)
            self.store.mark_failed(post.id, str(e))
```

### Key Behaviors
1. **30-second polling interval** — lightweight, no missed posts
2. **Anti-bot enforcement** — checks `should_post_now()` before each execution
3. **Rate limit awareness** — checks `can_upload()` per platform
4. **Retry logic** — up to 2 retries with 30-minute delay before marking failed
5. **Graceful integration** — can run alongside watch mode or standalone
6. **Timezone handling** — stores all times as ISO 8601 with timezone offset

---

## 5. Anti-Bot Time Window Enforcement

### Current Implementation (in `anti_bot.py`)
```python
def should_post_now(self) -> bool:
    now = self._get_local_time()
    return 8 <= now.hour < 23
```

### Enhancement Needed
When a scheduled post's time is outside 8am–11pm, the scheduler should:
1. **Reject at scheduling time** — CLI/dashboard should warn the user
2. **Defer at execution time** — if the post was scheduled for 10:55pm but runs at 11:02pm, defer to next morning 8am
3. **Add jitter** — don't post at exactly 8:00:00, add ±5 min random offset

### Config Addition
```yaml
schedule:
  check_interval: 900
  catchup_window: 172800
  catchup_times_per_day: 3
  # New fields:
  posting_hours_start: 8    # 8 AM
  posting_hours_end: 23     # 11 PM
  timezone: "America/Los_Angeles"  # IANA timezone name
```

---

## 6. Timezone Handling

### Strategy
- **Store all times as ISO 8601 with timezone offset** (e.g., `2026-06-08T14:30:00-07:00`)
- **Use `zoneinfo.ZoneInfo`** (Python 3.9+ stdlib) for IANA timezone support
- **Display times in user's configured timezone** in CLI and dashboard
- **Default timezone**: system local timezone (auto-detected)

### Implementation
```python
from zoneinfo import ZoneInfo
from datetime import datetime, timezone

def get_user_timezone(config: XPSTConfig) -> ZoneInfo:
    """Get user's configured timezone."""
    tz_name = config.schedule.timezone
    if tz_name:
        return ZoneInfo(tz_name)
    # Auto-detect from system
    import time
    offset = time.timezone if time.daylight == 0 else time.altzone
    # Fallback: use system local time
    return ZoneInfo("UTC")  # Will use datetime.now() which returns local time

def parse_scheduled_time(time_str: str, user_tz: ZoneInfo) -> datetime:
    """Parse user-provided time string into timezone-aware datetime."""
    # Accept formats:
    # "2026-06-08 14:30"      → interpreted in user's timezone
    # "2026-06-08T14:30:00"   → interpreted in user's timezone
    # "2026-06-08T14:30:00-07:00" → used as-is
    ...
```

---

## 7. CLI Integration

### New Commands

```bash
# Schedule a post for a specific time
xpst schedule add \
    --video /path/to/video.mp4 \
    --caption "My awesome video" \
    --platforms youtube,instagram,x \
    --at "2026-06-08 14:30" \
    --timezone "America/Los_Angeles"

# Queue multiple posts (interactive or from file)
xpst schedule add \
    --video /path/to/video1.mp4 \
    --caption "Video 1" \
    --at "2026-06-08 10:00"
xpst schedule add \
    --video /path/to/video2.mp4 \
    --caption "Video 2" \
    --at "2026-06-08 14:00"

# List upcoming scheduled posts
xpst schedule list
xpst schedule list --status pending
xpst schedule list --status all

# View details of a specific scheduled post
xpst schedule show <id>

# Edit a scheduled post
xpst schedule edit <id> --at "2026-06-09 10:00"
xpst schedule edit <id> --caption "Updated caption"
xpst schedule edit <id> --platforms youtube,instagram

# Cancel a scheduled post
xpst schedule cancel <id>

# Delete a scheduled post
xpst schedule delete <id>

# Start the scheduler daemon (runs continuously)
xpst schedule start
xpst schedule start --with-watch   # Run alongside watch mode
```

### CLI Implementation (Click)

```python
@main.group(invoke_without_command=True)
@click.pass_context
def schedule(ctx: click.Context):
    """Manage scheduled posts"""
    if ctx.invoked_subcommand is None:
        # Show list by default
        _schedule_list(ctx, status="pending")

@schedule.command("add")
@click.option("--video", "-v", required=True, multiple=True, type=click.Path(exists=True))
@click.option("--caption", "-c", required=True)
@click.option("--platforms", "-p", default=None)
@click.option("--at", "scheduled_time", required=True, help="Scheduled time (e.g., '2026-06-08 14:30')")
@click.option("--timezone", "-tz", default=None, help="IANA timezone (default: system local)")
@click.option("--max-retries", default=2, help="Max retry attempts")
@click.pass_context
def schedule_add(ctx, video, caption, platforms, scheduled_time, timezone, max_retries):
    """Schedule a post for a specific date/time"""
    ...

@schedule.command("list")
@click.option("--status", "-s", default="pending", help="Filter by status (pending/completed/failed/cancelled/all)")
@click.option("--limit", "-l", default=20, help="Max posts to show")
@click.pass_context
def schedule_list(ctx, status, limit):
    """List scheduled posts"""
    ...

@schedule.command("show")
@click.argument("post_id")
@click.pass_context
def schedule_show(ctx, post_id):
    """Show details of a scheduled post"""
    ...

@schedule.command("edit")
@click.argument("post_id")
@click.option("--at", "scheduled_time", default=None)
@click.option("--caption", "-c", default=None)
@click.option("--platforms", "-p", default=None)
@click.pass_context
def schedule_edit(ctx, post_id, scheduled_time, caption, platforms):
    """Edit a scheduled post"""
    ...

@schedule.command("cancel")
@click.argument("post_id")
@click.pass_context
def schedule_cancel(ctx, post_id):
    """Cancel a scheduled post"""
    ...

@schedule.command("delete")
@click.argument("post_id")
@click.pass_context
def schedule_delete(ctx, post_id):
    """Delete a scheduled post"""
    ...

@schedule.command("start")
@click.option("--with-watch", is_flag=True, help="Also run watch mode")
@click.pass_context
def schedule_start(ctx, with_watch):
    """Start the scheduler daemon"""
    ...
```

### Display Format

```
╭─────────────────────────────────────────────────────────╮
│  xPST — Scheduled Posts                                 │
╰─────────────────────────────────────────────────────────╯

  ID          Scheduled For        Platforms          Caption              Status
  ──────────  ───────────────────  ─────────────────  ───────────────────  ─────────
  a1b2c3d4    Jun 08, 2026 2:30PM  YT, IG, X          Check out my new...  ● Pending
  e5f6g7h8    Jun 08, 2026 6:00PM  YT, IG             Summer vibes...      ● Pending
  i9j0k1l2    Jun 09, 2026 10:00AM YT, X, IG          Behind the scenes... ● Pending

  3 pending posts · Next: Jun 08, 2:30PM
```

---

## 8. Dashboard Integration (NiceGUI)

### New Page: `/schedule`

Add to `_NAV_ITEMS`:
```python
_NAV_ITEMS = [
    ("/", "◉", "Overview"),
    ("/content", "◫", "Content"),
    ("/schedule", "⏱", "Schedule"),    # NEW
    ("/analytics", "◈", "Analytics"),
    ("/connect", "⧫", "Connect"),
    ("/settings", "⚙", "Settings"),
]
```

### Schedule Page Components

1. **Header**: "Scheduled Posts" with count badge
2. **Quick Stats Cards**:
   - Pending count
   - Next scheduled time
   - Completed today
   - Failed count
3. **"Schedule New Post" Button** → opens a dialog/form:
   - Video file picker (drag-drop or path input)
   - Caption textarea
   - Platform checkboxes (YouTube, Instagram, X, TikTok)
   - Date/time picker with timezone dropdown
   - Max retries number input
   - "Schedule" button
4. **Scheduled Posts Table**:
   - Columns: Time, Caption (truncated), Platforms, Status, Actions
   - Status badges: Pending (blue), Running (yellow), Completed (green), Failed (red), Cancelled (grey)
   - Actions: Edit, Cancel/Delete buttons
5. **Edit Dialog**:
   - Modify time, caption, platforms
   - Save/Cancel buttons

### Implementation Notes
- Use NiceGUI's `ui.dialog()` for add/edit forms
- Use `ui.table()` or custom card grid for the post list
- Use `ui.date()` and `ui.time()` for date/time pickers
- Auto-refresh every 30 seconds to show status updates
- Wire to `ScheduleStore` for data operations

---

## 9. Config Changes

### `config.py` — New `ScheduleConfig` Fields

```python
@dataclass
class ScheduleConfig:
    """Scheduling configuration"""
    check_interval: int = 900        # Existing
    catchup_window: int = 172800     # Existing
    catchup_times_per_day: int = 3   # Existing
    # New fields:
    timezone: str = ""               # IANA timezone (empty = system local)
    posting_hours_start: int = 8     # 8 AM
    posting_hours_end: int = 23      # 11 PM
    scheduler_poll_interval: int = 30  # How often to check for due posts
    max_retries: int = 2             # Default retry count for scheduled posts
```

### `anti_bot.py` — Configurable Hours

```python
def __init__(self, timezone_offset=None, daily_limits=None,
             posting_hours_start=8, posting_hours_end=23):
    self._posting_start = posting_hours_start
    self._posting_end = posting_hours_end
    ...

def should_post_now(self) -> bool:
    now = self._get_local_time()
    return self._posting_start <= now.hour < self._posting_end
```

---

## 10. Persistence & Restart Handling

### On App Restart
1. `PostScheduler.__init__()` loads `ScheduleStore`
2. `ScheduleStore` reads `~/.xpst/schedule.json`
3. Any posts with `status="running"` are reset to `status="pending"` (interrupted mid-execution)
4. Scheduler checks for overdue posts (scheduled_time < now, status=pending) and executes them immediately (respecting anti-bot windows)

### Edge Cases
- **Mac Sleep/Wake**: Same catch-up logic as existing `Scheduler._needs_catch_up()` — if posts were due during sleep, execute them on wake
- **File moved/deleted**: If `video_paths` no longer exist, mark post as failed with descriptive error
- **Clock skew**: Use UTC internally, convert to local for display only
- **Concurrent instances**: File locking prevents two scheduler instances from executing the same post

---

## 11. Files to Create/Modify

### New Files
| File | Purpose |
|------|---------|
| `src/xpst/schedule_store.py` | Persistent storage for scheduled posts |
| `src/xpst/post_scheduler.py` | Async scheduler that executes due posts |
| `tests/test_schedule_store.py` | Unit tests for ScheduleStore |
| `tests/test_post_scheduler.py` | Unit tests for PostScheduler |

### Modified Files
| File | Changes |
|------|---------|
| `src/xpst/cli.py` | Add `schedule` command group with subcommands |
| `src/xpst/config.py` | Extend `ScheduleConfig` with timezone/hours/poll fields |
| `src/xpst/anti_bot.py` | Make posting hours configurable |
| `src/xpst/dashboard/app.py` | Add `/schedule` page, update nav items |
| `src/xpst/dashboard/server.py` | No changes needed (pages auto-registered) |
| `src/xpst/scheduler.py` | Integrate `PostScheduler` as optional background task |
| `pyproject.toml` | No new dependencies needed |

---

## 12. Dependencies

**No new external dependencies required.** Everything uses stdlib:
- `asyncio` — async scheduler loop
- `json` — schedule persistence
- `uuid` — post IDs
- `datetime` + `zoneinfo` — timezone-aware scheduling
- `fcntl`/`msvcrt` — file locking (already used in `StateManager`)
- `pathlib` — file paths
- `dataclasses` — data models

---

## 13. Example User Workflows

### Workflow 1: Schedule a single post
```bash
$ xpst schedule add \
    --video ~/Videos/summer_vibe.mp4 \
    --caption "Summer vibes ☀️ #shorts" \
    --platforms youtube,instagram \
    --at "2026-06-08 14:30" \
    --timezone "America/Los_Angeles"

✓ Post scheduled for Jun 08, 2026 at 2:30 PM PDT
  ID: a1b2c3d4-e5f6-7890-abcd-ef1234567890
  Platforms: YouTube, Instagram
  Caption: "Summer vibes ☀️ #shorts"
```

### Workflow 2: Queue multiple posts
```bash
$ xpst schedule add -v video1.mp4 -c "First post" --at "2026-06-08 10:00"
$ xpst schedule add -v video2.mp4 -c "Second post" --at "2026-06-08 14:00"
$ xpst schedule add -v video3.mp4 -c "Third post" --at "2026-06-08 18:00"
$ xpst schedule list

  3 pending posts queued for Jun 08
```

### Workflow 3: Edit a scheduled post
```bash
$ xpst schedule edit a1b2c3d4 --at "2026-06-09 10:00" --caption "Updated caption"
✓ Post a1b2c3d4 updated
  New time: Jun 09, 2026 at 10:00 AM PDT
  New caption: "Updated caption"
```

### Workflow 4: Start the scheduler
```bash
$ xpst schedule start --with-watch

  xPST Scheduler + Watch Mode
  ✓ Scheduler running (polling every 30s)
  ✓ Watch mode running (checking every 900s)
  ✓ 3 posts scheduled
  ✓ Next post: Jun 08 at 2:30 PM

  Press Ctrl+C to stop
```

### Workflow 5: Dashboard scheduling
In the NiceGUI dashboard `/schedule` page:
1. Click "Schedule New Post"
2. Enter video path, caption, select platforms
3. Pick date/time from datetime picker
4. Click "Schedule" → post appears in the table
5. Click "Edit" or "Cancel" on any pending post

---

## 14. Implementation Order

1. **Phase 1: Core** — `schedule_store.py` + `post_scheduler.py` + unit tests
2. **Phase 2: CLI** — `xpst schedule` command group
3. **Phase 3: Config** — Extend `ScheduleConfig` + `AntiBotProtection`
4. **Phase 4: Dashboard** — `/schedule` page with NiceGUI
5. **Phase 5: Integration** — Wire into existing `scheduler.py` and `watch` command
6. **Phase 6: Polish** — Error messages, edge cases, documentation

---

## 15. Testing Strategy

### Unit Tests
- `ScheduleStore`: add, get, list, update, cancel, delete, get_due_posts, persistence across reload
- `PostScheduler`: mock engine, verify execution, retry logic, anti-bot enforcement
- Timezone handling: parse various formats, DST transitions, UTC conversion

### Integration Tests
- Schedule → restart app → verify posts still pending
- Schedule → execute → verify posts moved to completed
- Schedule outside posting hours → verify deferral
- File deleted → verify graceful failure

### Edge Cases to Test
- Scheduling for past time (should warn/reject)
- Scheduling for exactly posting hours boundary (7:59am, 11:00pm)
- Very long caption (>500 chars)
- Empty platforms list (default to all)
- Concurrent CLI and dashboard scheduling
- Schedule file corruption → recovery from backup
