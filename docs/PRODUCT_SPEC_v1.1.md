# xPST v1.1 — Product & Scope Spec

**Status:** Draft for review — correct this, don't extend it. · **License:** MIT OR Apache-2.0 · **Target release:** v1.1

## 1. What xPST is

xPST is a local-first, open-source **cross-poster**: it takes short-form video that already exists (on a connected source platform, or as a local file) and publishes it to every other platform the user owns, at full native fidelity, from one control surface. It is a distribution tool, not a content tool.

## 2. Target user

Two equal operators. Neither is a special case:

1. **Anyone (human).** Any creator, small business, or individual. "Anyone" means: install + FFmpeg is the only prerequisite, first post inside 5 minutes via 3 commands, and every error message says exactly what to do next. No assumed technical skill.
2. **AI agents.** The CLI is the agent interface, machine-optimized by default: auto-JSON on non-TTY, `--json`/`--quiet`, stable exit codes, stable error schema, plus an MCP server exposing the whole product. An agent must be able to install → auth (with a human browser step only where the platform requires one) → schedule → post → verify → read analytics with zero interactive UI.

**Rule:** any human-facing feature without a machine path is not done. Agent behavior must not degrade the human CLI (JSON only when non-TTY or requested).

## 3. Hard non-negotiables (veto gates)

| # | Non-negotiable | Gate |
|---|----------------|------|
| 1 | **Lightweight** | Section 6 budgets. No daemons by default, no telemetry, no required cloud. |
| 2 | **Encrypted creds** | Zero plaintext creds on disk. OS keychain primary, Fernet+scrypt `.enc` fallback. Enforced by `xpst security-audit`. |
| 3 | **Cross-platform** | One codebase → macOS/Windows/Linux via `.spec` builds + Docker. All 6 platforms work on all 3 OSes. |
| 4 | **Open source** | Dual-licensed MIT OR Apache-2.0. No personal data in distributables. No SaaS, no accounts, no license keys. |
| 5 | **Agent-friendly** | Auto-JSON CLI on non-TTY; clean exit codes (0 success, 1 general, 2 config, 3 auth, 4 rate-limit, 10 platform-unavailable); full MCP server; stable output schema — breaking changes need a major version. |

Quality gate: full CI suite (1467 tests) green on all three OSes.

## 4. Product boundary

**In scope — distribution only:**
- Fetch existing short-form video from connected sources (or local files)
- Technical per-platform adaptation (orientation/duration/resolution/profile)
- Fan-out publish with user- or agent-supplied metadata (title, caption, tags, schedule)
- Post lifecycle: retry, dead-letter, crash recovery, delete
- Read-only post analytics (per-post engagement, follower counts)
- Facebook Messenger auto-reply — **opt-in, off by default**, rule-based canned replies only

**Out of scope — content deciding. Explicit:**
- xPST does not decide **what** to post, **when** to post, or **whether** to post
- xPST does not write, rewrite, generate, or edit post copy (captions/titles/hashtags) — not by AI, not otherwise
- xPST does not curate or rank the user's back-catalog as a recommendation engine
- xPST does not give editorial advice ("you should post more / this will do better")

Re-scoped from today's tree: `xpst suggest-caption` is a content-deciding feature — **out for v1.1**. Agents that want captions generate them upstream and pass them via MCP. (Reviewer: confirm kill vs. flag-behind-opt-in.)

Messenger is the one deliberate exception to "cross-poster only," bounded to inbound page messages with canned rule-based replies. It never posts outbound content.

## 5. Required capabilities

### 5.1 Platform support — 6 video + 1 opt-in

**Flawless for anyone in v1.1:**
- YouTube Shorts, X, Instagram Reels, TikTok, Threads, LinkedIn: each verified end-to-end against a real account (auth → publish → read-back → delete), not just mocked
- Per-platform auth: X = twikit cookie session; YouTube = Google OAuth; IG = instagrapi session or Meta Graph API; TikTok/Threads/LinkedIn = OAuth 2.0
- Messenger off by default; activates only on explicit config + `xpst auth messenger`
- `xpst auth status --json`: one machine-readable table of all platform health, each failure with a concrete remediation

**Out:** live streaming; comments/DMs posting (Messenger inbound-reply excepted); platforms beyond this set; "all platforms" catch-all claims.

### 5.2 Credential & auth lifecycle

**Flawless for anyone in v1.1:**
- Per-platform `xpst auth <platform>`; interactive browser approval only where the platform requires it — human-in-the-loop is design, not a flaw
- Encrypted at rest: OS keychain (Keychain / Secret Service / Credential Manager), Fernet+scrypt `.enc` fallback, automatic migration, never plaintext
- Token/session auto-refresh; expiry surfaces as exit 3 with the exact fix, not a crash
- Machine-local only: no cross-machine or cross-user credential sharing, no cloud sync

**Out:** cookie import from arbitrary browsers (only TikTok's opt-in `cookies_from_browser`); team/org auth.

### 5.3 Cross-post engine

**Flawless for anyone in v1.1:**
- Bidirectional: monitor every connected source for new videos, fan out to every connected destination
- Download once, upload N; smart passthrough probe skips re-encode when the source already meets the target profile (no generation loss)
- Per-platform circuit breakers: one platform failing never blocks others; repeat offenders auto-disable and auto-recover
- Crash recovery: partial uploads detected and resumed; dead-letter queue with `xpst retry`
- Per-platform daily upload quotas, machine-readable via `xpst quota`

**Out:** live streaming; comment/moderation management; paid-boost automation.

### 5.4 Per-platform adaptation (technical only)

**Flawless for anyone in v1.1:**
- Orientation-aware encoding (e.g. 9:16 → 1:1 / 16:9) that never degrades beyond what the target profile requires
- Platform limits (duration, resolution, file size, title length) enforced automatically — "platform rejected my post" is a bug class that must not exist
- Transformation is purely technical: re-encode, crop/pad to profile, bitrate

**Out:** overlays, transitions, subtitles, music/audio swaps, color grading — content editing is not a cross-poster feature.

### 5.5 CLI (the agent interface)

**Flawless for anyone in v1.1:**
- 34 commands covering the whole workflow; every command has a machine path
- Auto-JSON on non-TTY (stdout is a stable JSON document when piped), `--json` forces, `--quiet` silences
- Exit codes: 0 / 1 general / 2 config / 3 auth / 4 rate-limit / 10 platform-unavailable — documented, stable, machine-actionable
- Every error is a stable object: `{code, message, remediation}` — remediation is a concrete command
- `--dry-run` on every mutating command
- No interactive prompt on non-TTY: missing input exits 2, never hangs

**Out:** human-only subcommands; interactive wizards on non-TTY; output-format churn between patch versions.

### 5.6 MCP server (the agent surface)

**Flawless for anyone in v1.1:**
- 23 tools spanning the whole workflow (post, health, config, state, platforms, scheduling, analytics, KB, captions, transcripts, search)
- Tools mirror CLI semantics: JSON in / JSON out, same error contract as the CLI
- Single-instance guard; destructive tools require explicit args (no destructive defaults); argument/path validation is fail-closed
- Tool schema is versioned — no breaking renames without a major version

**Out:** chat-style agent UX; any tool that crosses the boundary (no "decide what to post" tool).

### 5.7 Scheduling

**Flawless for anyone in v1.1:**
- OS-native schedulers (LaunchAgent / cron / Task Scheduler) plus in-app scheduling; `xpst schedule` manages entries, `--json` introspectable
- Deterministic: a scheduled run behaves identically to a manual run — same engine, same error contract

**Out:** hosted/cloud cron; distributed multi-machine scheduling.

### 5.8 Analytics (read-only)

**Flawless for anyone in v1.1:**
- Normalized per-post engagement schema across all 6 video platforms (views/likes/comments/shares + platform-specific signals)
- Follower tracking with growth history; best-time suggestions derived from the user's own data
- Cross-post correlation: one video's performance across every platform it landed on, in one view
- Persistent local history (SQLite); JSON/CSV export; honest capability matrix documenting what each platform actually exposes

**Out:** benchmarking against other users; audience insight beyond platform-provided data; editorial recommendations (content-deciding).

### 5.9 Security & hardening

**Flawless for anyone in v1.1:**
- Encrypted creds (gate 2), atomic state writes (write-then-rename + pidfile), bcrypt for local passwords
- `xpst security-audit`: one command grading the install's credential hygiene
- Redacted diagnostics bundle for support — no creds, no content
- Zero telemetry: the only network traffic is the platform API calls the user configured

**Out:** compliance (SOC2 etc.) claims; hosted audit/monitoring; key escrow.

### 5.10 Packaging & portability

**Flawless for anyone in v1.1:**
- macOS/Windows/Linux: PyInstaller `.spec` builds + Docker; install via pip/uv or a single build; `xpst build --target` for cross-compilation
- Headless-first: the full product works on a display-less server; GUI is a convenience, never a requirement
- Dependencies: Python + FFmpeg, both auto-detected; nothing else required at runtime

**Out:** mobile; feature-gating behind the GUI; proprietary installers.

### 5.11 Messenger auto-reply (opt-in)

**Flawless for anyone in v1.1:**
- `enabled: false` by default — nothing runs until the user opts in explicitly
- Static Page Access Token + App Secret in the encrypted CredentialStore; webhook with signature verification checked against both config and store
- Canned rule-based replies only; `messenger_send` / `messenger_set_rules` via CLI and MCP

**Out:** conversational AI replies; outbound content posting; anything that decides what to say.

## 6. Performance budget (the lightweight contract)

Targets for review — correct the numbers, don't remove the gate:

- **Idle:** ≤ 200 MB RSS for any resident process; default posture is run-then-exit (no daemon). Messenger opt-in is the only resident exception: ≤ 300 MB RSS
- **CPU:** ≤ 5% of one core when idle; encoding is job-scoped and may use all cores
- **Startup:** CLI command to ready < 5 s cold; MCP server to ready < 10 s cold
- **Memory growth:** no unbounded caches; state/analytics live in SQLite on disk, not resident in RAM
- **Bloat rule:** any new dependency or resident service requires a spec change first. "It's in the test suite" is not a ship reason.

## 7. Open questions (for reviewer)

1. `suggest-caption`: kill, or keep flag-behind-opt-in? (Spec position: out for v1.1.)
2. Messenger resident budget: 300 MB acceptable, or tighter?
3. IG auth in v1.1: both instagrapi session and Meta Graph API, or pick one? (Spec position: both, session preferred for personal accounts.)
4. Should platform-unavailable exit codes split per platform (10–15) instead of a single 10?
