# xPST Architecture Decision — Lightweight UI & Product Evolution

**Status:** DECISION DRAFT (for review)
**Date:** 2026-08-26
**Scope:** UI/deployment model, analytics, upload & delete flows, optional native AI responder, roadmap
**Governing constraint (Tyler, hard rule):** the app stays LIGHTWEIGHT — never a heavy Electron monster. Python backend stays. Everything optional is config-gated, nothing optional becomes a required service.

---

## 0. Decisions in one place

| # | Decision | Selected option | Rejected |
|---|----------|-----------------|----------|
| D1 | Product UI | **Local-web UI served by the existing in-process FastAPI server** (`127.0.0.1:<port>`); no JS build toolchain; static HTML/JS/CSS shipped inside the PyInstaller bundle | Electron (D~120–250 MB), Neutralino (DX too thin), Textual (terminal-only) |
| D2 | Desktop shell | **Phase 3: Tauri 2 (Rust) shell wrapping the PyInstaller Python backend as a `sidecar`** → ~10–15 MB app with native tray/notifications/updater. Interim (Phases 1–2): open default browser + optional `pystray`/existing Qt tray | Electron forever, raw PWA-only (no tray), PySide6/QML as the primary UI |
| D3 | Desktop app today | **PySide6/QML app stays supported as a legacy optional extra** (`xpst[desktop]`), but is no longer the evolution target. Its current bundle is already **346 MB** — Qt is the size problem, not Electron-only | — |
| D4 | Analytics | Official APIs where free/viable (YouTube ✓, TikTok ✓, Instagram Graph ✓ with auth work, X via **twikit** (free, shipped) + optional X API v2); single `AnalyticsCollector` with per-platform metric contracts + 15-min cache (exists) | Paying for X API as a default dependency |
| D5 | Delete/unpublish | Official hard-deletes everywhere; **soft-delete** where offered (YouTube `privacyStatus=private`); TikTok API delete gap addressed with web-session fallback + surfaced limitation | Pretending TikTok API delete exists |
| D6 | AI auto-responder | **Native in-process optional module** — per-platform listeners (IG webhooks + polling; YT/X/TikTok polling) + existing OpenAI-compatible LLM client; stored memory via existing KB/SQLite. Config-gated like the Messenger adapter | Chatwoot, Papercups, n8n (all need a whole server stack) |
| D7 | Shipping | PyInstaller `build_{macos,windows,linux}.spec` (already in use) + static web assets in `datas`; Tauri `externalBin` sidecar at Phase 3 | Switching to Electron tooling |

Security invariant that survives every option: **credentials and tokens NEVER enter the browser/JS layer.** The web UI is a client of the localhost backend only; SessionManager stays the single source of truth (matches existing architecture principle). Browser JS sees `{platform: "youtube", health: "ok"}`, never a client secret.

---

## 1. Lightweight desktop/UI options — sized for THIS Python codebase

### 1.0 Current state (measured 2026-08-26 on this Mac)

- `dist/xPST.app` (PySide6/QML desktop entry) = **346 MB** on disk; `dist/xPST` = 343 MB.
- The product already has: CLI (37 commands), FastAPI dashboard (port 8080, bcrypt auth, WebSocket, Messenger webhook), PySide6/QML desktop app (8+ pages, tray, splash), MCP server, analytics collector.
- PyInstaller specs exist for macOS/Windows/Linux (PySide6 hiddenimports, QML datas, excludes for tkinter/matplotlib/numpy/pandas).

**Interpretation:** the current bundle is already a "monster" by size — driven by PySide6/Qt + Python + googleapiclient, not by Electron. So the decision is not "Electron vs not-Electron"; it is "what UI stack do we evolve on". The lightweight future cannot be "keep Qt and just add chrome"; it must be a thin client over the Python backend.

### 1.1 Options and tradeoffs

| Option | Installed size | Runtime deps | Fits Python backend? | Desktop integration | Dev cost / risk |
|---|---|---|---|---|---|
| **Tauri 2** (Rust shell + OS webview + web frontend) | **3–15 MB** typical (Hello World 3.2 MB; complex app ~8.6 MB) | OS-native WebView only; Rust toolchain only at build time; Python must run as **sidecar binary** or over localhost | Medium-high: needs sidecar packaging of the PyInstaller output + IPC or localhost; **JavaScript frontend required** (no QML) | Full: native tray, notifications, autostart, updater, window chrome | Rust build toolchain (rustup, no brew needed on mac); cross-compilation for Win/Linux via CI; smaller ecosystem than Electron but production-proven (v2 stable since Oct 2024; used by AppFlowy, Hoppscotch, Spacedrive) |
| **Electron** (Chromium bundled) | **120–250 MB+** (min ~85 MB; Slack ~650 MB installed) | Bundles Chromium + Node | Low-medium: runs Python via child process (`spawn`, `PORT=…`) or localhost; same localhost pattern as Tauri | Full: tray, notifications, auto-update (electron-updater) | Easiest ecosystem, huge community; but violates the size constraint outright (RAM 168 MB+ idle) |
| **Neutralino** (~2 MB / 0.5 MB compressed) | ~2 MB | Uses OS webview (like Tauri) but **no built-in sidecar/process story** — would still run Python as separate localhost server | Low: thin JS API; you'd hand-roll the process bridge; minimal plugin/ecosystem | Partial: tray exists via extensions, but everything is DIY; few maintained examples for Python backends | Cheapest binary, wrong level of abstraction for a real product UI; ecosystem/maintenance risk |
| **PySide6 / PyQt native** (current) | **346 MB measured** (this bundle) | Qt + PySide6 wheel set; heavy font/QML assets | Perfect (it's Python) | Full: tray, notifications, autostart | Already built (8 pages, i18n, plugins) — but big, Qt-version/ABI churn, QML styling is slow to iterate, hard to make “Meta-quality” polished web-like UI; keeps 346 MB bundle unless cut hard |
| **Textual / TUI** | ~5–10 MB | Nothing extra | Perfect | **None** (terminal) — TUI can’t do analytics charts/tray the way a GUI product needs | Good power-user/agent surface (xPST already is agent-friendly), but not the “real UI” Tyler asked for; keep as an *additional* surface |
| **Local-web + PWA** (FastAPI serves static SPA; installed via “app mode”/Chromium PWA) | **~2–6 MB** (static assets only) | Default browser only | Perfect (backend already exists) | Weak: no native tray (workaround: pystray or keep Qt tray), no system auto-updater; browser tab feels less “app” | Cheapest to build (reuses dashboard); zero new runtime; ideal **Phase 1–2** vehicle and the natural UI to later wrap in Tauri |

Size/RAM numbers sourced from: Tauri-vs-Electron measurements (tech-insider.org/tauri-vs-electron-2026; digitalapplied.com 2026; betterprogramming.pub tray-apps comparison), neutralino.js.org (official ~2 MB), and the on-disk measurement of the existing xPST.app above.

### 1.2 RECOMMENDATION (D1 + D2)

**Keep the Python backend. Evolve the UI in two decoupled layers:**

1. **Product UI = web UI served by the existing FastAPI process** on `127.0.0.1` (reuse dashboard server; add SPA routes / static files). No Electron, no new runtime, static assets only. This is where “a real UI” is built: compose + schedule + content library + analytics dashboards + connect wizard + inbox, all calling the same engine the CLI/MCP already use.
2. **Desktop shell = Tauri 2 at Phase 3**, wrapping the PyInstaller Python sidecar. Target desktop artifact: **~15 MB**, native tray + notifications + auto-updater. The web UI is shell-agnostic, so nothing about the shell choice blocks UI development now.

Interim desktop posture (Phase 1–2): `xpst ui` starts the localhost server and opens the default browser (optionally with `--no-browser` for servers/agents); keep the existing PySide6 app shipping for users who already use it, but stop expanding it. This costs zero new infra and de-risks the Tauri migration completely.

Why not pick the other options as primary:

- **Electron:** outright violates the size constraint (120–250 MB, 168 MB+ RAM idle). Only reason to ever reconsider: if a Chromium-identical rendering surface across OSes becomes a hard requirement or we decide auto-update/signing must be native today — and Tauri 2 now covers updater + signing for the main platforms.
- **Neutralino:** binary is tiny but the product would still need the same localhost Python backend (so no size win over Tauri), with a thinner ecosystem and no first-class sidecar pattern. No advantage.
- **PySide6/QML as primary:** it is already 346 MB — it *is* the current size problem; QML iteration cost and Qt freight keep it from being the lightweight future. It stays as a supported legacy surface (D3), not the evolution target.
- **Textual:** keep as a bonus power-user surface (consistent with xPST’s agent-first ethos), but it cannot be “the real UI”.
- **Raw PWA only:** fine for Phases 1–2; not enough for a desktop *product* (tray, notifications, auto-update) in the long term — hence Tauri, not PWA, as the final shell.

### 1.3 Shipping (D7)

- Continue using the existing `build_{macos,windows,linux}.spec`. Required change: add the built static web UI directory to `datas` (like QML is today), and ensure the FastAPI `static` + SPA route serve from the frozen `_MEIPASS` path (there is already a `resource_path` helper pattern in `desktop_app/`).
- `xpst ui` entry: start uvicorn on `127.0.0.1:<port>` (configurable; default 8080), bcrypt-auth per existing dashboard, open browser. Agent/server mode via `--no-browser` + health-check print.
- At Phase 3, Tauri consumes the PyInstaller binary as `externalBin` **sidecar**; the Rust shell opens `http://127.0.0.1:<port>` in its WebView, and the Tauri tray/system-tray plugin becomes the OS integration point. macOS proof-of-fix first (rustup works without Homebrew); Windows/Linux cross-build in GitHub Actions CI.
- Never ship Electron; never require Node at runtime.

---

## 2. Analytics (D4) — official APIs, endpoints, rate limits, data, auth (verified Aug 2026)

Ground truth: analytics code already exists (`src/xpst/analytics.py`, 15-min cache, per-platform collectors; YouTube metrics already wired live; X via twikit; TikTok/Instagram collectors present). This section states what each *official* API exposes so we implement to contract, not to guesswork.

### 2.1 X / Twitter

| Item | Detail |
|---|---|
| Endpoint (own posts + engagement) | `GET /2/users/{id}/tweets?tweet.fields=public_metrics,organic_metrics,non_public_metrics,created_at` (max 100/request); `GET /2/tweets/{id}` for a single post. `public_metrics` = `impression_count, like_count, retweet_count, reply_count, quote_count, bookmark_count`; `organic_metrics` (organic vs promoted breakout) & `non_public_metrics` need **user-context** auth |
| Usage/debt visibility | `GET /2/usage/tweets` (Bearer, app-level) ~ 50 req / 15 min |
| Other useful reads | `GET /2/users/me` (own id); `GET /2/users/{id}/mentions`; recent search `GET /2/tweets/search/recent` (7-day window) |
| Rate limits | Tier-dependent (legacy Basic ≈ $200/mo: ~10–15k reads/mo project cap; post lookup 3,500 app / 5,000 user per 15 min; recent search 450 app / 300 user; user lookup 500/day; create post 100/user per 15 min). **Free tier is discontinued for new developers (2026);** new apps are on pay-per-use credit model (~$0.001/read) |
| Auth | OAuth 2.0 user-context (read own private/organic metrics), or OAuth 1.0a/twikit cookie session (no cost, no monthly cap). xPST already auths X via **twikit cookies** (free, no API plan needed) |
| Data gaps | No official API for post *comments* on your own posts in v2 self-serve (comment counts only via `public_metrics`); comment text requires polling mentions or scraping — see §4.1 |

**xPST decision:** primary analytics source = **twikit** (free, already authenticated, no plan). Add optional X API v2 integration behind an optional config flag for users who have a plan (metrics parity + reply counts). Never make a paid X plan a hard dependency. Sources: docs.x.com/x-api (metrics, rate-limits, usage), postproxy.dev/blog/x-api-pricing-2026, socialcrawl.dev 2026.

### 2.2 YouTube

| Item | Detail |
|---|---|
| Endpoint (metrics) | `GET /youtube/v3/videos?part=statistics,contentDetails&id=<videoId>` (up to 50 IDs/call — batch!). Returns `viewCount, likeCount, commentCount, favoriteCount`. Channel totals: `GET /youtube/v3/channels?part=statistics&mine=true` → `subscriberCount, videoCount, viewCount` |
| Endpoint (comments) | `GET /youtube/v3/commentThreads?part=snippet,replies&videoId=<id>` (100 threads/page); `GET /youtube/v3/comments?part=snippet&parentId=<id>` (100/page). Watch for `commentsDisabled` errors |
| Quota | **10,000 units/day/project** (resets midnight PT). `videos.list`=1 unit; `commentThreads.list`=1; `comments.list`=1; `channels.list`=1; `search.list`=100; `playlistItems.list`=1. Uploads moved to 1 unit + **separate 100 calls/day bucket** (Dec 2025 cost cut; June 2026 separate bucket); ~30 req/sec typical rate limit; over-quota → HTTP 403 `quotaExceeded` |
| Auth | OAuth 2.0 (already live in PRODUCTION for xPST since 2026-08-26 → long-lived refresh tokens; scopes `youtube`, `youtube.upload`, `youtube.readonly`, `youtube.force-ssl`) |
| Data gaps | No “real-time impressions/x” style creator-studio analytics (avg view duration etc.) in v3 — only the statistics above; those extra metrics live in Studio only |

**xPST status:** collector already live (`_collect_youtube`, uploads-playlist discovery added recently). Budget usage: a daily analytics pass for ≤50 own videos costs ≤3–5 units. Sources: developers.google.com/youtube/v3/determine_quota_cost (quota calculator), blotato.com/blog/youtube-api-pricing (Dec 2025/June 2026 changes), socialcrawl.dev 2026.

### 2.3 TikTok

| Item | Detail |
|---|---|
| Endpoint (own videos + engagement) | **Content Posting API**: `POST /v2/post/publish/video/list/query/` and `GET /v2/post/publish/video/feed/` (own published videos; fields include `view_count, like_count, comment_count, share_count, play_count`). **Display API**: `POST /v2/video/list/`, `POST /v2/video/query/`, `GET /v2/user/info/` (same engagement fields incl. `view_count, like_count, comment_count, share_count`) |
| Endpoint (comments) | ⚠️ **Not available in Display/Content Posting APIs.** Comment text/senders only via **Research API** `Query Video Comments` (`POST /query/video/comments/`) — a separate audited product (1,000 req/day, 100,000 records/day default) — or by scraping. Engagement *counts* are available; comment *content* is not, on the app token |
| Rate limits | Content Posting: **6 requests/min per user token**, plus unpublished daily video cap (~15–25/account/day shared across clients). Display: **600 req/min per endpoint**, one-minute sliding window, 429 `rate_limit_exceeded`. Tokens: access_token 24h, refresh_token 365d |
| Webhooks | Available but limited to: `authorization.removed`, `video.upload.failed`, `video.publish.completed`, `portability.download.ready`. **No comment, no DM events.** Helps the upload flow (publish status), not the responder |
| Auth | OAuth 2.0 (`client_key`/`client_secret`), scopes per product (e.g. `video.list`, `video.publish`, `user.info.basic`); **app must pass TikTok audit before production posts become public** (xPST app exists, in review as of 2026-08-26). HTTPS callback required for webhooks (the GitHub-Pages xPST site can host a redirect/callback) |
| Data gaps | No DMs on the consumer OAuth API at all (TikTok *Business/Ads* Messaging is a separate advertiser product). So: no DM support on TikTok for the AI responder (see §4.1) |

Sources: developers.tiktok.com (rate limits page, Display API get-started, webhooks overview/events), blotato.com/blog/tiktok-api-pricing, getphyllo.com 2026, stackoverflow “Getting Direct Messages via official TikTok API”.

### 2.4 Instagram (Graph API — NOT yet implemented; instagrapi session is 403-invalid today)

| Item | Detail |
|---|---|
| Requirement | Account must be a **Business or Creator professional account**; analytics/insights/comments/messaging all go through the **Instagram Graph API** (Meta Graph). `instagrapi` (current xPST approach) cannot do this |
| Endpoint (media + counts) | `GET /{ig-user-id}/media?fields=id,caption,timestamp,media_type,permalink,like_count,comments_count` |
| Endpoint (insights) | `GET /{media-id}/insights?metric=…&period=lifetime|day` (metrics: `reach, impressions, engagement, saved, shares, likes, comments, views, plays, video_views, ig_reels_avg_watch_time, ig_reels_video_view_total_time, total_interactions …`; REELS & days_28 variants; `impressions` deprecated for media created after 2024-07-02). Account-level: `GET /{ig-user-id}/insights?metric=impressions,reach,profile_views,follower_count&period=day` |
| Endpoint (comments + replies) | `GET /{media-id}/comments`, `GET /{comment-id}/replies`, `POST /{comment-id}/replies`, `POST /{media-id}/comments` (reply as the account), `DELETE /{comment-id}` |
| Endpoint (DM threads) | Threads: `GET /{ig-id}/conversations`, `GET /{ig-id}/conversations/{thread-id}`; messages: `GET /{ig-id}/messages`; **send** via Messenger platform: `POST /me/messages` or `POST /{ig-id}/messages` (Graph API messaging, messaging_type) |
| Webhooks | **Native support:** subscribe fields `comments`, `mentions`, `messages`, `message_reactions`, `messaging_seen` (IG Login perms) / Messenger-platform messaging. Real-time DM + comment events — the only platform that offers both natively |
| Rate limits | 200 calls/hour/user (Standard Access; Business Use Case scales ~200 × active users; rolling 60-min window). DM send: ~100 messages / 24h / recipient (consumer escalation). 429 on breach |
| Auth | Meta developer app. Instagram Login perms `instagram_business_basic`, `instagram_business_content_publish`, `instagram_business_manage_comments`, `instagram_business_manage_messages` (or Facebook Login + Page roles + Page Access Token, `instagram_basic` etc.). Tokens: short-lived 24h; long-lived ~60 days (renewable) — reuse the existing CredentialStore/refresh patterns. **xPST has no Meta app yet — Phase 1-2 work item (Tyler to pick account-approval once, per the connect-wizard rule).** |

Sources: developers.facebook.com (Instagram webhooks, IG media insights, Graph-API rate-limiting, Instagram messaging webhooks), zernio.com 2026, getphyllo.com 2026.

### 2.5 Analytics architecture rules (carried forward)

- Single `AnalyticsCollector` facade; **per-platform metric contract** (`PlatformMetrics`) already defined; add `metrics_latency`, `auth_state`, and per-platform `available_metrics` capability tables so the UI renders only what a connected account can actually show.
- Cache TTL stays 15 min (already implemented) — respects every rate limit above on a per-user/per-day cadence.
- Store rolling history in the existing analytics store/SQLite so “trends over time” work without re-calling APIs.
- All analytics calls go through the existing quota/circuit-breaker helpers (`utils/quota.py`, `utils/circuit_breaker.py`).

---

## 3. Upload flow & deletion/unpublish flow (D5) — exact calls, soft vs hard delete

### 3.1 YouTube

**Upload** (exists, fixed 2026-08-26): resumable OAuth upload via `googleapiclient` `videos.insert` (`snippet`+`status`+`contentDetails` parts, `media_body`, `media_mime_type`), using the YouTube Shorts constraints (≤60 s, vertical). Now costs **1 unit** + the 100-uploads/day bucket (Dec 2025 change). Poll `videos.list(part=status)` for `uploadStatus == "processed"` before UI shows success.

**Delete / unpublish:**
- Hard delete: `DELETE https://www.googleapis.com/youtube/v3/videos?id=<videoId>` (1 unit; OAuth `youtube` scope). IRREVERSIBLE.
- **Soft delete/unpublish (use for “hide”):** `videos.update` with `status.privacyStatus = "private"` (also `"unlisted"`). The docs-uploader already sets privacyStatus — extend the settings to drive this. Recommend UI offers both: **“Unpublish” (private, reversible) and “Delete” (irreversible)**.

### 3.2 X

**Upload** (exists via twikit): media upload (chunked, `media/upload`) → post text with media ids → returns tweet. API-v2 alternative: `POST /2/tweets` (media via v1.1 `media/upload` chunked endpoint, 15-min media lifetime).

**Delete:** hard delete only — twikit `deleteTweet(tweet_id)` or `DELETE /2/tweets/{id}` (OAuth user context + `tweet.write`). X has **no soft delete / unpublish**; deletion cannot be undone (UI must warn; keep the state row with a “deleted” tombstone instead of removing context).

### 3.3 TikTok

**Upload** (exists, container model): `POST /v2/post/publish/video/init/` → (option A) `PUT` video bytes to returned `upload_url` (PULL_FROM_URL/PUSH_TO_URL variants) → poll `POST /v2/post/publish/status/fetch/` or (newer) rely on the `video.publish.completed` webhook. 6 req/min/user — our flow already respects this; keep retries on 429 with exponential backoff (quota helper exists).

**Delete:** ⚠️ **The Content Posting API has no delete endpoint** (confirmed in code comment and API reference; do not pretend otherwise). Options, recommended order:
1. **Best-effort web-session delete** (fallback, cookie jar we already export for the source side): authenticated `DELETE https://www.tiktok.com/api/post/item/delete/?video_id=<id>` with the account cookies (works today, unofficial).
2. Mark the state row `deleted="via-web"` when the fallback succeeds; otherwise mark `delete_pending` and surface it in the UI with the share URL so the user can remove it in-app in one tap.
3. Track TikTok’s changelog; if they ship an official delete, wire it behind the same `PlatformUploader.delete` contract immediately (adapter pattern means the UI never changes).

TikTok always = **hard delete** when it happens; no soft delete exists. (Photo Post path exists as well — `POST /v2/post/publish/photo/init/` if we later support images.)

### 3.4 Instagram (Graph API)

**Upload** (to be implemented in Phase 2): for REELS, `POST /{ig-user-id}/media?media_type=REELS&video_url=…&caption=…&share_to_feed=true` → get a `creation_id`, then `POST /{ig-user-id}/media_publish?creation_id=<id>`. (Container model like TikTok; publish + poll.)

**Delete / unpublish:**
- Hard delete: `DELETE /{media-id}` (Graph API; requires `instagram_business_content_publish`-grade token). Stories: `DELETE /{story-media-id}`.
- **Archive (“soft”) is NOT exposed by the Graph API** — there is no official “archive post” call; archive/unarchive is UI-only in the app. So: offer hard delete via API, and (optional) a documented “archive manually” hint. 
- Comment-level delete exists: `DELETE /{comment-id}` — use for AI-responder moderation.

### 3.5 Threads (secondary)

Already implemented in `platforms/threads.py` (publish + `DELETE /{media-id}`). Reuse; no change.

### 3.6 Cross-cutting delete policy

- `engine.delete_post` already resolves platform ids from state (fixed 2026-08-26 — the `post_id` vs `id` bug). UI calls the same engine.
- **State discipline:** on hard delete, keep the tombstone (platform, id, url, deleted_at, reason) so analytics can mark a row “removed from platform” rather than losing history. On soft delete (YouTube private), mark `visibility=private` in state and keep metrics.
- Every delete path returns a per-platform result (deleted / soft-hidden / pending / unsupported) so the UI can show exactly what happened — no silent “No post data found” style failures (already fixed).

---

## 4. Native open-source AI auto-responder (D6)

Goal (Tyler): ManyChat-like, self-hosted, optional, an AI agent may reply to **comments/DMs**, built-in — not a separate heavy service.

### 4.1 What the platforms actually offer a listener (verified Aug 2026)

| Platform | Comment events | DM events | Notes |
|---|---|---|---|
| Instagram | ✅ native webhook field `comments` + polling fallback (`GET /{media-id}/comments`) | ✅ native webhook `messages` (IG Messaging) + thread polling | Only platform with native webhooks for both |
| YouTube | ⚠️ no comment webhook — **poll** `commentThreads.list` on own uploads (1 unit/call; cheap) | n/a (comments only) | Reply: `comments.insert` / `commentThreads.insert` + `comments.setModerationStatus` (moderation for free) |
| X | ⚠️ no comment webhook on self-serve tiers (`Account Activity API` = paid Enterprise product; new webhook paths not on PAYG) — **poll** mentions (`GET /2/users/{id}/mentions`) or twikit notifications | ⚠️ DMs: **poll** `GET /2/dm_conversations` / `dm_events` (v2) or twikit; send via `POST /2/dm_conversations/{id}/messages` (Basic+: 1/min practical send rate) | twikit route = free, no X API plan needed for DMs |
| TikTok | ⚠️ **no comment endpoint** on the app token (Research API only, 1,000 req/day, separate audited product) — **poll is not even possible via official API**; comment *counts* only. Optional: occasional Research-API pull if approved | ⚠️ no DMs on consumer OAuth API at all | Earliest/lowest-value responder target; suggest “replies via web-session” later or defer |
| Messenger (existing adapter) | — | ✅ native webhooks already implemented in xPST (`/webhook/messenger`, verified signatures, rules engine) | Mature pattern to copy |

### 4.2 Option comparison

**(i) Per-platform listeners + local LLM via any OpenAI-compatible endpoint**
- What: a `listeners/` package (webhook handlers where platforms offer them: IG comments+messages, Messenger; polling workers elsewhere) + an `ai_responder` service that calls **any OpenAI-compatible endpoint** (`/v1/chat/completions`, configurable base_url + api_key; works with Ollama, llama.cpp `-server`, LM Studio, vLLM on our own DGX Spark cluster, or hosted).
- Cost/size: **zero new services**, ~300–600 lines of Python; reuses existing FastAPI webhook infra, CredentialStore, the knowledge-base LLM client (`xpst.knowledge` already talks to an OpenAI-compatible endpoint and has a KB/vector store for memory), quota/retry helpers, state store.
- Strengths: fully native, no Docker, runs offline-enabled (local model), agent can use tooling (MCP is already in-tree), per-platform opt-in config flags (mirror the Messenger pattern: `enabled=False` by default; `X% of comments` + “draft-only/auto-send” modes; reply-later/never-repeat guard via `comment_id` dedupe).
- Weaknesses: we build it (it’s small); X/TikTok latency to discover events is polling-bounded (minutes), which is acceptable for comments.

**(ii) Self-hosted Chatwoot**
- Ruby on Rails + PostgreSQL + Redis; **minimum 4 GB RAM (8 GB recommended), 20–60 GB storage**, Docker compose of several services. It is a full customer-support inbox product (teams, SLA, CSAT, inboxes).
- Fit: overkill for “auto-respond to my comments/DMs”, adds a permanent server dependency, and its social-channel ingestion still needs the same per-platform bridging we’d write anyway. Anti-lightweight. 

**(iii) Self-hosted Papercups**
- Elixir/Phoenix + Postgres + Redis; project is essentially **dormant/abandoned** (YC S20; no active maintenance; live-chat-focused, no native ingestion for IG/YT/X/TikTok comments/DMs). Would be a liability, not leverage.

**(iv) n8n (or Comet-type automation)**
- Node.js workflow automation; real production sizing ≈ **2–4 GB RAM + Postgres + reverse proxy**; Docker image ~hundreds of MB. Powerful, but it is a second automation platform bolted onto a Python app that already *has* an engine, CLI, MCP and schedulers. (“Comet” is ambiguous in this context — no single mature OSS “Comet” product covers all four channels; CometChat is a chat SDK and Comet/Cosmos-family tools are small utilities — none replace an in-process responder.)
- Fit: fine for a company wanting a general automation rack, wrong for “keep xPST lightweight”.

### 4.3 RECOMMENDATION

**Option (i): native in-process AI responder as an OPTIONAL, config-gated module — no Chatwoot, no Papercups, no n8n.**

Architecture sketch (all inside the existing process, all optional):

```
xpst/listeners/            (new, optional extra "ai-responder")
  base.py                 # Listener contract: fetch_events(), ack(event_id), reply(...)
  instagram.py            # webhook handlers (comments, messages) + polling fallback
  youtube.py              # poll commentThreads on own uploads
  x.py                    # poll mentions/DMs (twikit first; v2 API if configured)
  tiktok.py               # (v2) research-API pull or web-session; skipped unless enabled
  messenger.py            # reuse existing webhook
  hub.py                  # dedupe by event id, fan-out to enabled listeners, backoff
xpst/ai_responder/        # (new, optional)
  provider.py             # ANY OpenAI-compatible /v1/chat/completions client (reuse KB llm client)
  policy.py               # enable flags, platform filters, auto-send|draft-only, cooldowns
  memory.py               # per-conversation history via existing KB/SQLite; never repeat
  reviewer.py             # optional human-approval queue (drafts surfaced in the UI inbox)
```

Hard rules that keep it safe and lightweight:
- Default `enabled: false` (mirrors Messenger). Installing the extra adds **no new dependencies beyond** an HTTP client we already ship.
- Dedupe: every event stored by platform+object id; a comment only ever gets one AI reply (or is marked “replied”); no reply loops (store `replied_comment_ids` in state).
- Never reply without guardrails: length caps, no-ping-pong (skip if user just replied), draft-only mode, per-platform human-in-the-loop.
- LLM endpoint is **user-supplied** — xPST must never require a hosted API key; point anywhere OpenAI-compatible (including the in-house DGX Spark vLLM endpoints from the homelab).
- Reply through each platform’s official “reply as the account” call (IG `POST /{comment-id}/replies`, YT comment insert, X DM/message create, Messenger `me/messages`) — identical code paths as the manual-reply UI.

Earliest win ordering: **Messenger (exists) → Instagram comments+DMs (native webhooks) → YouTube comments (poll) → X DMs/mentions (poll) → TikTok (defer/optional).**

---

## 5. Pragmatic phased roadmap (D1–D7) on the existing Python codebase, lightweight throughout

Invariant for every phase: **no Electron, no new always-on service, no Node at runtime, optional features behind config flags, static-only frontend, CI-enforced bundle-size budget.**

### Phase 1 — Foundation & analytics completeness (≈2–4 weeks)
**Goal: make the data layer and flows correct and complete; freeze the UI burden.**
1. **Analytics to contract:** finish per-platform collectors — TikTok `video/list` + `video/query` metrics via Content Posting token (**no new auth**), YouTube already live, X twikit metrics, and define the `available_metrics` capability table. Add a `xpst analytics --json` aggregate report + trend history in the analytics store.
2. **Delete/unpublish completeness:** TikTok web-session delete fallback (§3.3) + surfaced limitation; YouTube soft-delete (`privacyStatus=private`) behind the delete menu; tombstones in state; per-platform delete result contract (deleted/soft-hidden/pending/unsupported). Tests for each path.
3. **`xpst ui` command:** serve the existing dashboard + open browser; `--no-browser` agent mode; bind 127.0.0.1 only; bcrypt auth retained. No new UI yet.
4. **Instagram auth (one-time Tyler step):** create the Meta app, add Instagram Login product, publish to production (same playbook as the GCP/YouTube fix), user-approves once via the connect wizard. Unblocks analytics, uploads, webhooks, and the responder on IG — the single highest-leverage unlock.
5. **CI gates:** bundle-size budget (e.g. fail if macOS app >400 MB today and a Phase-3 target set), import-linter rule that `xpst.ui`/`listeners`/`ai_responder` may not import Qt.

### Phase 2 — Product UI on the local web (≈4–8 weeks)
**Goal: the real UI, done light.**
1. **Web UI (static, no build toolchain):** compose & schedule, content library with status, analytics dashboards (per §2 contracts), connect wizard (reuse `xpst wizard` output + health checks), upload/delete menus incl. soft-vs-hard delete, inbox/drafts surface. Served by the FastAPI process; tokens never leave backend (§0 invariant).
2. **Unified notification/inbox engine:** poll-and-merge comments/DMs per §4.1, surfaced in the UI; per-platform reply/dismiss. (Lays the pipe for the AI responder.)
3. **Upload flow polish per §3,** including Instagram REELS container flow once IG auth exists; GitHub-Pages callback/HTTPS support for IG + TikTok webhooks (the existing `tysais.github.io/xPST` site is the callback host).
4. Ship `xpst ui` as the default launch; PySide6 app remains optional legacy (`xpst[desktop]`); update docs + first-run copy accordingly.

### Phase 3 — Desktop shell + optional AI responder + release (≈4–8 weeks)
**Goal: thin native shell and the complete optional feature.**
1. **Tauri 2 shell** wrapping the PyInstaller sidecar (target **~15 MB** desktop artifact, macOS first with rustup, Win/Linux in CI): native tray, notifications, autostart, updater. The Web UI becomes the shell’s content unchanged.
2. **AI auto-responder module (§4.3)** as the optional extra: OpenAI-compatible endpoint config, per-platform listeners (IG webhooks first, YT/X polling, TikTok optional), draft/inbox integration, dedupe + guardrails, memory via KB.
3. **Release packaging:** PyPI + per-OS installers; bundle-size check against Phase-1 target; performance/cold-start telemetry (local only); final documentation + connect-wizard polish for new platforms.

**Sequencing rationale:** nothing in Phases 1–2 depends on the shell choice, so the entire product direction is de-risked immediately; Tauri is last precisely because it is the only part needing Rust toolchains and can be replaced without touching the product.

---

## 6. Risks, open questions, and follow-ups

**Known risks**
- TikTok API delete gap and zero comment/DM API coverage are vendor facts — mitigated by web-session fallback and UI surfacing; do not design anything that depends on TikTok comment content on the app token.
- X free tier is gone for new signups → twikit-first stance is the inflation/shutdown-proof default; the optional v2 integration is a plug-in, not a pillar.
- Meta/Instagram long-lived token lifetime (~60 days) requires a refresh schedule in the existing token-refresh logic (pattern already proven for Google).
- Web UI = new browser security surface: bind loopback only, bcrypt auth (exists), CSRF-safe API (localhost + token header), no secrets in JS.

**Open questions for Tyler**
1. Confirm Meta app creation + IG account approval is OK to set up in **Phase 1** (his phone-only consent, one time).
2. Confirm bundle-size budget target for the Tauri shell (~15 MB macOS) and whether the PySide6 legacy app should remain a supported download (recommend: yes, flagged “legacy”, kept building).
3. Accept GitHub-Pages site as the webhook callback host for IG/TikTok (existing footprint, already HTTPS + verified).
4. AI responder defaults: auto-send off by default (draft-only is the safe default for launch).

**Where this came from (facts verified 2026-08-26):** developers.google.com/youtube/v3/determine_quota_cost; developers.tiktok.com (rate limits, Display API get-started, webhooks overview/events, Content Posting direct-post reference); developers.facebook.com (Instagram webhooks, Instagram messaging webhooks, IG media insights, Graph API rate-limiting); docs.x.com/x-api (rate limits, metrics, account-activity intro); neutralino.js.org; tech-insider.org/tauri-vs-electron-2026; digitalapplied.com/blog/desktop-apps-web-stack-tauri-electron-deno-wails-2026; chatwoot.com/deploy + developers.chatwoot.com/self-hosted; blotato.com (YouTube API pricing, TikTok API pricing); socialcrawl.dev (X API 2026, YouTube 2026); postproxy.dev (X API pricing 2026); ghcr/codebase measurements for the xPST bundle. All API endpoints above were cross-checked at the time of writing; platform terms change — re-verify the linked reference before implementation.
