# xPST Roadmap

This roadmap tracks what shipped in v1.0.0 and what's planned next. It is a
living document — priorities may shift based on community feedback. Open an
issue to propose or upvote an item.

## Unreleased

- ✅ **Facebook Messenger platform** (opt-in, disabled by default) — ManyChat-lite
  auto-reply/chatbot. Static Page Access Token auth, `appsecret_proof` on
  outbound calls, `X-Hub-Signature-256` webhook verification, `auto_reply` +
  `reply_rules` keyword matching, `xpst auth messenger` wizard, and
  `messenger_send` / `messenger_set_rules` MCP tools.

---

## Shipped in v1.0.0

- ✅ **Five-platform posting** — YouTube Shorts, X, Instagram Reels, TikTok,
  Threads, across engine, scheduler, dashboard, desktop app, and
  analytics.
- ✅ **TikTok posting** via the official Content Posting API v2.
- ✅ **Official-API-first auth** — Instagram Graph API as primary mode; official
  paths preferred across platforms with documented fallbacks.
- ✅ **Per-video and cross-post analytics** — per-platform metrics plus combined
  metrics for the same video posted to multiple platforms.
- ✅ **Follower tracking** and **best-time-to-post** analysis.
- ✅ **28 MCP tools** for AI-agent control, plus a full CLI surface.
- ✅ **Caption suggestions** with per-platform character limits.
- ✅ **Personal knowledge base** with source provenance and zero-config
  deterministic extraction.
- ✅ **Anti-ban foundations** — user-agent rotation, proxy support, caption
  variation, account warming, per-account device IDs.
- ✅ **Security hardening** — encrypted credential storage, localhost-only
  dashboard by default, MCP read-only mode, security-audit command.
- ✅ **Desktop app** — native macOS/Windows/Linux app with consistent hover
  states, dark mode persistence, and accessibility annotations.
- ✅ **CI** — single GitHub Actions workflow with tests and a security gate.

---

## v1.0.x — Stability & Distribution

Near-term, non-breaking work focused on making xPST easy to install and run.

### Distribution
- **PyPI publication** — publish the wheel so `pip install xpst` works from the
  index, with an automated release-to-PyPI pipeline.
- **Docker image** — official container for the dashboard and MCP server, with a
  documented `docker run` / compose setup.
- **Prebuilt desktop binaries** — automated, signed builds for macOS, Windows,
  and Linux attached to GitHub releases.

### Reliability & polish
- **First-run onboarding** — guided setup wizard (pick content folder → connect
  a platform → post first video).
- **Error-handling polish** — surface all errors to the user with recovery
  suggestions; no silent failures.
- **Quota usage estimator** — show remaining uploads/posts for the day per
  platform in the dashboard and CLI.
- **Responsive desktop layout** — collapse sidebar to icons below 1100px and
  reduce grid columns on narrow windows.
- **Documentation** — screenshots in tutorials and an optional video
  walkthrough.

---

## v1.1 — Deeper Analytics & Knowledge Base

Larger features targeted at the next minor release.

### Analytics
- **Expanded per-platform analytics** — fill gaps where a platform exposes
  metrics behind a Business/Creator account or additional API scopes
  (e.g. Instagram saves/shares, richer TikTok metrics).
- **Trend history & reporting** — chart engagement over time from the persistent
  snapshot store; exportable reports.
- **Engagement-weighted suggestions** — feed performance history back into
  caption and best-time recommendations.

### Knowledge base
- **Auto-ingest from the posting engine** — after a successful cross-post,
  enqueue the source so extracted knowledge carries performance history.
- **Repurposable-clip extraction** — extend extraction beyond knowledge nuggets
  to quotable hooks, clip-worthy moments, and topic summaries.
- **Complete MCP/CLI parity** — expose the remaining knowledge-base commands
  (course, doctor, re-embed, clip search) over MCP.

### Platforms
- **Additional platforms** — evaluate Pinterest, Snapchat Spotlight, and
  Bluesky based on official API availability and demand.

---

## Under consideration

Ideas not yet scheduled. Feedback welcome.

- Bulk scheduling and a content calendar view.
- Team/multi-account workspaces.
- Plugin marketplace / discoverable third-party uploaders.
- Localization of the desktop app and dashboard.
