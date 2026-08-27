# Changelog

All notable changes to xPST will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

No changes since 1.0.0.

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
