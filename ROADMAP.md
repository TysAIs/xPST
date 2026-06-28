# xPST Production Readiness Roadmap

> Generated 2026-06-19 from full audit (Cult/UI research, anti-ban research, knowledge base audit, CLI/MCP audit, UI/UX QA)

## Current State: v0.1.0-rc — Phases 1-4 COMPLETE, Phase 5 IN PROGRESS

- ✅ 1322 tests passing, 0 failures, 0 warnings
- ✅ 28 CLI commands verified production-ready
- ✅ 16 MCP tools verified production-ready
- ✅ Privacy scrub clean (zero personal data)
- ✅ Brand assets, README, 3 tutorials written
- ✅ ComposePage, DetailPanel created
- ✅ ConnectPage platform guides added
- ✅ Analytics fabricated data removed
- ✅ Phase 1: Anti-ban complete (UA rotation, TLS hardening, proxy, caption variation, account warming, device-ID)
- ✅ Phase 2: Official API paths complete (Instagram Graph API, X API v2, quota estimator)
- ✅ Phase 3: QML animation polish (AnimatedNumber, LoadingSkeleton, micro-interactions, page polish)
- ✅ Phase 4: KB refocus (extract modes, deterministic fallback, source provenance, search-only path, LanceDB fix)
- 🟡 Phase 5: Final polish (SECURITY.md, CONTRIBUTING.md, tutorials done; onboarding/error-handling/CI-CD partial)

---

## Phase 1: Anti-Ban Critical Fixes (SAFETY — do first)

### 1.1 Fix dead UA rotation
- `anti_bot.get_user_agent()` exists but is never called by uploaders
- Instagram hardcodes its own mobile UA in `platforms/instagram.py`
- X/YouTube clients don't use it either
- **Fix**: Call `anti_bot.get_user_agent()` in each platform uploader's client setup

### 1.2 Add TLS fingerprint hardening
- Python's httpx/urllib3 has a distinct JA3 that platforms flag instantly
- This is the #1 silent killer for instagrapi/twikit
- **Fix**: Add `curl_cffi` as optional dependency, route HTTP through it for IG/X
- **Alternative**: Document proxy requirement prominently

### 1.3 Add proxy support
- instagrapi's own docs call this the #1 ban cause
- Config has no proxy field
- **Fix**: Add `proxy` field to config, pass to instagrapi/twikit clients
- Support HTTP, SOCKS5, and residential proxy URLs

### 1.4 Improve caption variation
- Current suffix lists are mostly empty strings
- YouTube always appends identical `#Shorts`
- **Fix**: Add meaningful per-platform caption variations (hashtags, emojis, CTAs)

### 1.5 Add account warming / progressive ramp
- New sessions jump straight to upload velocity
- **Fix**: Track account age, gradually increase daily caps over first 2 weeks

### 1.6 Add per-account device-ID persistence for Instagram
- instagrapi supports `client.set_device()` but xPST doesn't use it
- **Fix**: Generate and persist a stable device ID per Instagram account

---

## Phase 2: Official API Paths (FREE + SAFE)

### 2.1 Add Instagram Graph API path
- Current: instagrapi (unofficial, ban-prone, ToS-violating)
- Add: Meta Graph API for business/creator accounts (free, official, ban-safe)
- Requires: Instagram Business account + linked Facebook Page
- **Fix**: Add `instagram.auth_mode` config option: "graph_api" | "session"
- Implement Graph API uploader as preferred mode, instagrapi as fallback with risk warning

### 2.2 Add X API v2 free tier path
- Current: twikit cookie-based (ToS-violating, rate-limit crackdown)
- Add: X API v2 free tier (17 posts/day, ban-safe, official)
- xPST already caps X at 10/day — well within free tier
- **Fix**: Add `x.auth_mode` config option: "api_v2" | "cookies"
- Implement API v2 uploader as preferred mode, twikit as fallback

### 2.3 Add quota usage estimator
- YouTube: 10,000 units/day, each upload = 1,600 units → ~6 uploads/day
- X: 17 posts/day, 1,500/month
- **Fix**: Show "X of N uploads remaining today" in dashboard and CLI

---

## Phase 3: QML Animation Polish (UX)

### 3.1 Port Cult/UI animation patterns to native QML
- Standardize StackView transitions to consistent Animator-based opacity+slide (~220ms, Easing.OutCubic)
- Add SpringAnimation to sidebar selection indicator (spring: 1.5, damping: 0.4, mass: 0.5)
- Staggered list entrance on ContentPage/AnalyticsPage cards (60-80ms per-item delay)
- Spring-driven AnimatedNumber QML component for analytics counts
- Loading skeleton (animated gradient shimmer) replacing BusyIndicator

### 3.2 Add hover/press micro-interactions
- `whileHover: {scale: 1.02}` → QML `Behavior on scale` + MouseArea containsMouse
- `whileTap: {scale: 0.98}` → QML `Behavior on scale` + MouseArea containsPressed
- Apply to all buttons, cards, and interactive elements

### 3.3 Fix responsive layout below 1100px
- Sidebar should collapse to icon-only mode below 1100px
- Grid layouts should reduce columns below 900px
- Test at 800px, 1024px, 1280px, 1920px

---

## Phase 4: Knowledge Base Refocus

### 4.1 Wire auto-ingest into cross-posting engine
- After successful CrossPostVideo, auto-enqueue source into KB queue
- Resolve source_platform/source_post_id so nuggets carry performance history
- This is the differentiator — no competitor does this for video libraries

### 4.2 Reframe extraction from "teachable nuggets" to "repurposable clips + topics"
- Generalize extraction prompt: topic summaries, quotable hooks, clip-worthy moments
- Add title/topic/tag fields to Nugget model
- Keep nugget extraction as one mode

### 4.3 Complete MCP surface
- Expose kb_course, kb_doctor, kb_reembed via MCP (only 4 of 8 CLI commands exposed)
- Add kb_search_clips returning videos/sources, not just nuggets

### 4.4 Fix default-config out-of-box experience
- Default assumes local LLM at http://127.0.0.1:8000/v1
- Add deterministic fallback (sentence-segmentation + keyword extraction) when no LLM configured
- Add "search-only" path (no transcription) for creators with existing transcripts

### 4.5 Maintenance pass
- Fix LanceDB `table_names()` → `list_tables()` deprecation
- Add real queue worker (durable queue exists but nothing drains it)
- Add guard/notice when extraction LLM is unreachable

---

## Phase 5: Final Polish

### 5.1 First-run onboarding
- Detect first launch, show guided setup wizard
- Walk through: pick content folder → connect first platform → post first video

### 5.2 Error handling polish
- Surface all QML errors to user (no silent catch blocks)
- Add error recovery suggestions in error dialogs

### 5.3 Documentation
- Add screenshots to tutorials
- Add video walkthrough (optional)
- Add CONTRIBUTING.md updates with new architecture

### 5.4 Security audit
- Review all credential storage paths
- Verify encryption at rest
- Add security.txt

### 5.5 CI/CD
- GitHub Actions for tests on push
- Automated release builds (macOS, Linux, Windows)
- PyPI publishing pipeline

---

## Priority Order

1. 🔴 Phase 1 (Anti-ban) — Safety risk, do first
2. 🔴 Phase 2 (Official APIs) — Reduces ban risk, adds free paths
3. 🟡 Phase 3 (QML polish) — UX improvement, not blocking
4. 🟡 Phase 4 (KB refocus) — Differentiator, not blocking
5. 🟢 Phase 5 (Final polish) — Pre-release cleanup

---

## Research Sources

- Cult/UI: https://github.com/nolly-studio/cult-ui (MIT, safe, web-only)
- Anti-ban: fingerprint.com, niespodd/browser-fingerprinting, instagrapi GH discussions
- Free posting: YouTube Data API docs, Meta Graph API docs, X API v2 docs
- Encoding: dev.to/alfg, gehrcke.de, CapKit export guide
- KB competitors: Buffer, Hootsuite, Sprout, Lately, Jasper, Writer
- Vector DBs: LanceDB vs Chroma vs Qdrant comparison
