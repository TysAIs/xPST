# Changelog

All notable changes to xPST will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security
- **Mutating dashboard routes now require authentication by default.**
  `POST /api/post`, `POST /api/connect/{platform}`, `POST /api/onboarding*`,
  `POST /api/preflight` and the `/bio/edit` form save were only protected when
  `monitoring.dashboard_username` / `dashboard_password_hash` happened to be
  configured — loopback is not an authorisation boundary, so any process on the
  machine (or any page open in a browser) could trigger a real post or start a
  connect flow. Every mutating route now fails closed with `401`, accepting
  either the dashboard Basic login (when configured) or the new dashboard API
  token.
- **New dashboard API token** (`xpst auth api-token`, `--rotate`): generated on
  first run and stored in the encrypted credential store
  (`dashboard_api_token`) like the platform OAuth tokens — never in
  `config.yaml`, and no default value ships with the project. Operators, scripts
  and agents can send it as `Authorization: Bearer <token>` or
  `X-API-Token: <token>`, or supply `XPST_API_TOKEN`.
- **Desktop/UI token hand-off**: the Tauri shell mints a per-launch token
  (`XPST_UI_TOKEN`) and opens its webview at
  `http://127.0.0.1:<port>/#xpst_token=…`; `xpst ui` does the same for the
  browser it opens. The token is never embedded in served HTML, and the SPA
  strips the fragment from the address bar immediately. Read-only routes keep
  their previous behaviour so the UI is never locked out; `POST /oauth/callback`
  and the Messenger webhook stay public by design. See SECURITY.md and
  docs/DASHBOARD.md.
### Added
- **Composer media preview** — the Compose screen now shows the selected asset
  before it is posted: images render from a cached, ffmpeg-generated
  thumbnail, and videos render as a real playable `<video>` element with the
  generated frame as its poster. Two new engine routes back it:
  `GET /api/media/stream` (HTTP range support — real `206 Partial Content`
  answers — serving the file through a bounded 1 MiB chunk iterator) and
  `GET /api/media/thumb` (single-frame JPEG, cached under
  `~/.xpst/cache/previews`, keyed by path + size + mtime). Selecting or
  scrubbing a large video never reads the whole file: the engine holds at most
  one chunk and the webview pulls only the ranges it needs.
- **Native file picker and drag-and-drop in the composer** — the desktop shell
  opens the OS file picker (`tauri-plugin-dialog`) and forwards native drops
  into the page as paths, so a picked or dropped file becomes the selection
  immediately. The shell grants the dialog command to the loopback engine
  origin only (`src-tauri/capabilities/default.json`). Outside the app window
  the composer says the picker is unavailable instead of inventing a path.

### Fixed
- **A platform's download source was reported as a posting destination.**
  `xpst doctor --json` derived its per-platform `connected` boolean from the
  platform-level `authenticated` flag, which for TikTok (auth mode
  `source_only`) is the verdict of its *download* probe. A healthy source
  therefore read as `"connected": true, "problem": null` — a promise that the
  posting destination exists when the engine has none until TikTok approves a
  Content Posting API app. TikTok is now reported as `source_only` /
  `can_post: false` with the reason attached, on every surface: `xpst doctor`
  (as an informational `notes` entry, never an `issue`, so a healthy machine
  still exits 0), `xpst health`, `xpst readiness`, `xpst connect --test
  --json`, `xpst onboard`, `POST /api/connect/<platform>`, `/api/providers`,
  `/api/health-status`, the MCP status tools and the desktop app's
  Connect/Compose/Accounts screens. Posting truth is computed once, in
  `xpst.provider_truth.posting_truth`, and every surface consumes that result
  instead of inferring "connected" from whichever flag was at hand.
- **`xpst doctor` paraphrased a real failure into a vague one.** A platform
  with stored credentials whose live check failed was reported as
  "Credentials found but the health check failed - token may be expired or
  revoked." even though the engine already knew the answer. Instagram's actual
  message — `Instagram session expired or invalid. Re-run: xpst connect
  instagram (username/password required for re-login)` — is now carried
  verbatim, so the reader gets the command to run and the one input it needs.
- **`xpst readiness` treated Threads as a source.** A hard-coded
  `{"tiktok", "threads"}` name list downgraded an unconfigured destination
  check to `info` severity, so an enabled Threads destination nobody had
  connected read as "nothing to do". Severity now follows the role verdict:
  source-only is `info`, an unconfigured destination is a `warning`.
- **`xpst doctor` on a fresh machine depended on the developer's quota
  ledger.** The doctor tests invoked the CLI without `--config`, so the report
  was built from the real `~/.xpst`; `test_all_clear_json` failed on any box
  whose X quota was already used up. They now pass their fixture config (with
  the JSON payload still parsed by skipping the CLI's log preamble).
- **Undefined design token** — `--xpst-color-primary-soft` was referenced by
  the selected/hover states but never defined in `tokens.css`, so those states
  silently rendered transparent in both themes. Defined for light, dark-theme
  and `prefers-color-scheme: dark`.
- **`xpst health` and `xpst doctor` can no longer disagree about who is
  connected.** `health` probed platforms through the engine while `doctor`
  rendered the canonical collector, so one machine could answer
  `instagram: session expired, re-run connect` and `instagram: ready` for the
  same account minutes apart (the old `doctor` only checked that a session
  *file existed*). Both now render one implementation —
  `xpst.auth_status.platform_health_entries` — and the platform block of
  `xpst_health` (MCP) does the same. TikTok's source-only state stops reading
  as "not authenticated" in `health` while every other surface says
  `source_only`.
- **A failed live probe is no longer reported as an expiry it never proved.**
  Every Instagram probe failure — transport errors, `429`/`5xx`, challenges and
  Instagram's anti-bot 302 redirect loop — used to print "Instagram session
  expired or invalid. Re-run: xpst connect instagram (username/password
  required for re-login)", a diagnosis nothing had observed and one that sends
  the user into the ban-risky password path. Probe failures are now classified
  (`xpst.utils.probe_errors`): a provider rejection (Instagram's
  `403 login_required` + "You've been logged out") keeps the re-login
  instruction and carries the provider's raw response; anything unproven is
  reported as an **unverified** probe (raw error + retry, `badge: unknown`, not
  `needs_reauth`) on every surface — `health`, `doctor`, `auth status`, MCP and
  the web UI. The raw error is no longer discarded at `logger.debug`.
### Added
- `xpst doctor --json` gained a `notes` array (facts that are not failures), plus
  `posting_destination`, `posting_role`, `posting_state`, `posting_error`,
  `can_post`, `source_only` and `source_ready` per platform. `xpst connect
  --test --json` gained the same `source_only` / `posting_truth` /
  `ready_sources` keys, and `xpst onboard --dry-run --json` reports source-only
  platforms under `source_only` / `ready_sources` instead of listing them as
  things to connect.
- **Honest token state + truthful badges** — `xpst auth status --json` now
  reports, per platform, a `token_state` / `badge` (`connected`, `expiring`,
  `needs_reauth`, `source_only`, `disabled`, `unknown`), a `badge_reason`
  explaining it and the `checked_at` timestamp the live check was taken. A
  green `connected` badge requires a passing live check on a fresh probe:
  a stored credential, a stale check or an unchecked platform renders
  `unknown`/`needs_reauth`, and source-only (TikTok) or disabled platforms
  never render as connected. The same badge is what the web UI, the desktop
  app and the MCP `xpst_auth_status` tool render, so no surface can disagree.
- **`xpst refresh-tokens`** and **`xpst auth status --refresh`** — bounded
  automatic refresh of expiring/expired access tokens (attempt budget,
  exponential backoff, wall-clock deadline; no prompts; nothing token-shaped
  is ever printed or persisted). The desktop health tick, the web API's
  background probe and `POST /api/refresh-tokens` run the same job, and the
  outcome is recorded in `~/.xpst/token_refresh.json` (0600) so a failed
  refresh keeps the badge at `needs_reauth` instead of promising a silent
  retry.

## [1.1.0] - 2026-09-04

### Added
- **Max-fidelity video pipeline** (`xpst/media`) — per-platform ingest
  spec matrix + `verify_media` pre-flight; transcode decision tree that
  prefers passthrough, then ZERO-LOSS stream-copy remux (`-c copy` into
  MP4 `+faststart` when only the container is foreign), then the
  platform-profile transcode; two-pass EBU R128 loudness normalization
  per platform (YouTube/TikTok/Instagram −14 LUFS, X −16 LUFS, TP −1.5)
  in linear mode; two-pass x264 encoding for bitrate-targeted profiles
  (YouTube 8 Mbps, X 10 Mbps); pre-upload spec verification wired into
  every upload (hard errors block, warnings attach to quality metadata).
- **`xpst verify-media FILE [-p PLATFORM] [--plan] [--json]`** — check a
  media file against platform specs before upload, with an optional
  dry-run transformation plan; exit 1 on blocking errors.
- **`xpst serve` daemon supervisor** — a single supervised long-running
  process that acquires the engine pidfile (safely rejecting a live holder,
  overwriting stale pidfiles left by crashed processes, releasing on
  graceful shutdown), runs the configured scheduler loop (reusing the
  existing Scheduler/ScheduleManager; scheduled posts + new-video watch
  checks), and optionally serves the FastAPI dashboard. Handles
  SIGTERM/SIGINT (clean shutdown) and SIGHUP (continue) and emits
  launchctl/systemd-friendly start/health/stop log lines. Flags:
  `--no-dashboard`, `--port`, `--host`, `--interval`, `--source`.
- **Consistent pidfile handling across commands** — `run`, `watch`, `post`,
  and the desktop `app` all route through the shared pidfile helper
  (`xpst.utils.pidfile`). Automatic engine loops (`run`/`watch`/`serve`)
  hold the exclusive lock and release it on exit (a one-shot `run` no longer
  leaves a stale pidfile behind); manual actions (`post`, desktop) use
  advisory verify-and-warn semantics so they work alongside a running
  daemon.
- **`xpst schedule install` now launches `xpst serve`** — the macOS
  LaunchAgent runs the supervisor continuously (RunAtLoad + KeepAlive), the
  Linux crontab entry is a pidfile-guarded, idempotent keep-alive tick, and
  the Windows scheduled task runs `serve` at logon. Legacy `schedule run`
  cron/plist/task entries are cleaned up on install and uninstall.
- **`get_last_wake_check()` on state managers** — implements the accessor
  the scheduler's sleep/wake catch-up heuristic already depended on
  (previously a latent `AttributeError` silently swallowed by `watch`).

### Fixed
- A one-shot `xpst run` no longer leaves a stale `xpst.pid` behind (the
  pidfile is always released on exit).

## [1.1.0] - 2026-09-06

Desktop reliability and release metadata fixes: the macOS bundle now reports
its project version, embeds the exact source commit, restores off-screen windows
onto the primary display, and enables Qt Quick accessibility before QML loads.

## [1.0.0] - 2026-08-18

First public release. xPST is an open-source, cross-platform suite for posting
short-form video to every major platform from one place, with built-in
analytics, scheduling, a desktop app, and AI-agent integration.

### Added
- **Six-platform posting**: YouTube Shorts, X, Instagram Reels, TikTok,
  and Threads — supported across the engine, scheduler, web dashboard,
  desktop app, analytics, and connection flow.
- **Facebook Messenger platform** (opt-in, disabled by default) — a
  ManyChat-lite auto-reply/chatbot option. Static Page Access Token auth (no
  refresh), direct `httpx` against the Graph API (`v22.0/me/messages`),
  `appsecret_proof` on outbound calls, and `X-Hub-Signature-256` verification
  on inbound webhooks in the dashboard. Includes `auto_reply` + `reply_rules`
  keyword matching, an `xpst auth messenger` wizard, and
  `messenger_send` / `messenger_set_rules` MCP tools.
- **TikTok posting** via the official Content Posting API v2 (previously
  source-only).
- **Per-video analytics**: views, likes, comments, and shares broken out by
  platform, viewable per post in the desktop app's detail panel.
- **Cross-post analytics**: one video posted to several platforms is correlated
  into a single entry with combined metrics.
- **Follower tracking** and **best-time-to-post** analysis across all platforms.
- **28 MCP tools** for AI-agent control, including cross-post analytics,
  follower stats, best-time suggestions, security audit, caption suggestions,
  transcript retrieval, and content search.
- **Caption suggestions** with platform-specific character limits, via CLI and
  MCP.
- **Security audit** command (CLI and MCP) covering credential storage, file
  permissions, dashboard binding, and provider configuration.
- **Dark mode** in the desktop app, with the preference persisted across
  sessions.
- **Personal knowledge base** (optional extra): ingest local files or URLs,
  transcribe, extract cited knowledge with source provenance, and search
  locally — works with zero configuration via a deterministic extractor.

### Changed
- **Instagram** now uses the official Graph API as its primary authentication
  mode, falling back to session-based access when Graph API is unavailable.
- **Encoding** for TikTok and Threads uses a Reels-grade profile
  (1080×1920, CRF 20, 10 Mbps).
- **Desktop app** has consistent hover states across all ten pages and refreshed
  navigation (Library, Accounts, Automations).
- **Web dashboard** binds to `127.0.0.1` by default; remote access is opt-in via
  an explicit `--host` flag with a warning.
- **Credentials** are stored with Fernet encryption (scrypt key derivation) and
  `0600` permissions, with no plaintext fallback.
- **Scheduled tasks** on macOS use the modern `launchctl` bootstrap/bootout API,
  eliminating password prompts.
- The MCP server is production-hardened with audit logging, a retry policy, a
  tool registry, and a CI security gate.

### Fixed
- State store race condition that could corrupt persisted post history under
  concurrent writes.
- Analytics key mismatch that prevented per-platform metrics from being
  associated with their posts.
- Detail panel field-name mismatch that hid per-platform analytics in the
  desktop app.
- Backfill operating on stale state, which could skip or re-attempt posts
  incorrectly.

### Security
- Independent security review: 0 critical, 0 high findings, no known CVEs in
  dependencies, and no personal or customer data in the repository.
- 1555 tests passing (2 skipped).
