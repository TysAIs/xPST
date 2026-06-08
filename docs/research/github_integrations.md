# xPST GitHub Open Source Integrations Research

> **Date:** June 7, 2026
> **Purpose:** Identify best open-source tools and libraries xPST can integrate or learn from across 10 key categories.
> **Criteria:** MIT/Apache/BSD license, active maintenance, Python-first or Python-compatible, clear integration path.

---

## 1. Social Media Scheduling

### 🏆 Best: BrightBean Studio
- **URL:** https://github.com/brightbeanxyz/brightbean-studio
- **Stars:** 1.7k
- **License:** AGPL-3.0 ⚠️ (copyleft — can integrate but modifications must be shared)
- **Language:** Python (Django)
- **Last Updated:** Actively maintained (updated hours ago)
- **What it does:** Full-featured self-hosted social media management platform. Schedule, publish, and manage content across 12+ platforms (Facebook, Instagram, LinkedIn, TikTok, YouTube, Pinterest, Threads, Bluesky, Mastodon, Google Business). Includes unified social inbox, approval workflows, analytics, media library, and client portal.
- **How xPST could use it:**
  - Study its multi-platform publishing engine and OAuth flows for each platform
  - Reference its approval workflow and scheduling queue architecture
  - Its REST API + MCP server pattern is directly relevant to xPST's agent architecture
  - Can deploy alongside xPST or use its scheduling logic as a reference
- **Integration difficulty:** Medium (Django-based, well-documented, but AGPL requires careful license handling)

### Honorable Mention: Free-AI-Social-Media-Scheduler
- **URL:** https://github.com/Anil-matcha/Free-AI-Social-Media-Scheduler
- **Stars:** 2.1k
- **License:** MIT ✅
- **Language:** JavaScript (Next.js)
- **What it does:** Self-hostable scheduler with built-in AI content generation. Currently supports YouTube and TikTok video scheduling with Stripe-powered credits system.
- **How xPST could use it:** Study its video-first scheduling pipeline and MuAPI integration for AI content generation
- **Integration difficulty:** Hard (Next.js/JavaScript, different tech stack from Python xPST)

---

## 2. Analytics Dashboards

### 🏆 Best: Daily Social Report (Livedashboard)
- **URL:** https://github.com/dizzyx/daily-social-report
- **Stars:** 0 (new)
- **License:** MIT ✅
- **Language:** TypeScript (Next.js 15, Recharts)
- **Last Updated:** April 2026
- **What it does:** Universal social media analytics dashboard with automated daily reporting across X, YouTube, LinkedIn, Instagram, and TikTok. Features: KPI cards, time-series charts, per-platform deep dives, branded PDF reports, automated Slack/Discord delivery, demo mode with mock data. Pluggable adapter interface for each platform.
- **How xPST could use it:**
  - Study its adapter interface pattern (`PlatformAdapter`) — directly applicable to xPST's multi-platform architecture
  - Reference its aggregation logic for cross-platform metrics
  - Its brand.config.ts pattern for white-labeling is useful
  - The cron-based daily report generation pattern
- **Integration difficulty:** Medium (TypeScript/Next.js, but architecture patterns are transferable)

### Honorable Mention: Plotly Dash
- **URL:** https://github.com/plotly/dash
- **Stars:** 24.2k
- **License:** MIT ✅
- **Language:** Python
- **What it does:** The most popular Python framework for building data dashboards. 50+ chart types, reactive callbacks, no JavaScript required.
- **How xPST could use it:** Build xPST's analytics dashboard using Dash — it's Python-native, battle-tested, and ideal for data visualization
- **Integration difficulty:** Easy (Python, well-documented, `pip install dash`)

---

## 3. Content Calendar

### 🏆 Best: Build Custom (using BrightBean Studio as reference)
No dedicated open-source content calendar tool with significant adoption was found. However:

**Reference Architecture from BrightBean Studio:**
- Visual calendar with recurring weekly posting slots per account
- Named posting queues
- Kanban idea board for content planning
- Content composer with per-platform overrides and version history
- Templates, categories, and tags

**Recommendation:** xPST should build its own content calendar module. Key components:
- Use **FullCalendar** (https://github.com/fullcalendar/fullcalendar, 18k stars, MIT) for the calendar UI
- Use **Plotly Dash** or **FastAPI + React** for the backend
- Study BrightBean's queue/slot system for recurring scheduling

- **Integration difficulty:** Medium (build from scratch using proven UI libraries)

---

## 4. AI Caption Generation

### 🏆 Best: LangChain (as framework for AI caption pipeline)
- **URL:** https://github.com/langchain-ai/langchain
- **Stars:** 139k
- **License:** MIT ✅
- **Language:** Python
- **Last Updated:** Actively maintained (3,927 contributors)
- **What it does:** The leading agent engineering platform. Chains LLM calls with tools, memory, and retrieval. Supports all major LLM providers (OpenAI, Anthropic, Google, etc.).
- **How xPST could use it:**
  - Build a caption generation pipeline: user input → LangChain chain → platform-specific caption with hashtags, CTAs, and emojis
  - Use prompt templates for different platforms (Twitter's 280 chars vs Instagram's 2200 chars)
  - Integrate with RAG for brand voice consistency
  - Use LangGraph for multi-step content generation workflows (research → draft → optimize → schedule)
- **Integration difficulty:** Easy (Python, extensive docs, `pip install langchain`)

### Also Relevant: OpenAI/Anthropic APIs directly
No mature open-source caption generator with significant adoption exists on GitHub. Best approach: build a caption generation module using LangChain + LLM APIs with platform-specific prompt templates.

---

## 5. Hashtag Research

### 🏆 Best: Build Custom (using RapidAPI/free hashtag APIs)
No mature open-source hashtag research tool exists on GitHub (only 3 results, all 0 stars).

**Recommended Approach:**
- Use **RapidAPI hashtag endpoints** (e.g., Best Hashtags, Instagram Hashtag Generator APIs — many have free tiers)
- Build a Python module that:
  1. Takes a topic/keyword as input
  2. Queries hashtag APIs for related hashtags with popularity scores
  3. Filters by platform (Instagram, TikTok, Twitter/X)
  4. Returns optimized hashtag sets (mix of high/medium/low competition)
- Study the approach from `Gursev12/Insta-Growth-Helper` (0 stars, but has hashtag generator + best posting time logic)

**Key APIs to evaluate:**
- Hashtagy API (RapidAPI)
- Tagstagram API
- Instagram's native hashtag search (requires auth)

- **Integration difficulty:** Easy (simple API wrapper module)

---

## 6. Best Time to Post

### 🏆 Best: WhenToPost.online
- **URL:** https://github.com/stefanicjuraj/whentopost.online
- **Stars:** 7
- **License:** MIT ✅
- **Language:** TypeScript (Next.js)
- **Last Updated:** March 2025
- **What it does:** Determines optimal posting times based on the user's timezone and their audience's timezone distribution. Simple, focused tool.
- **How xPST could use it:**
  - Study its timezone-overlap algorithm
  - Port the core logic to Python
  - Extend with platform-specific engagement data (each platform has different peak hours)
  - Add historical analytics integration (use xPST's own data to personalize recommendations)

**Supplementary data sources:**
- Sprout Social, Later, and Hootsuite publish free "best time to post" research annually
- Buffer's open research on optimal posting times

- **Integration difficulty:** Easy (simple algorithm, port to Python)

---

## 7. Image/Video Editing

### 🏆 Best: opensource-clipping (AI Auto-Clipper)
- **URL:** https://github.com/NaufalRizqullah/opensource-clipping
- **Stars:** 18
- **License:** MIT ✅
- **Language:** Python
- **Last Updated:** May 2026 (actively maintained)
- **What it does:** Full AI content factory: transforms long-form videos into viral short-form highlights. Features include: Whisper transcription, Gemini AI content curation, face-tracking auto-framing, karaoke subtitles (Hormozi style), B-roll integration (Pexels), BGM ducking, auto-thumbnails, cross-platform metadata generation, and auto YouTube upload.
- **How xPST could use it:**
  - Use as the core video processing pipeline for xPST's video features
  - Its aspect ratio handling (9:16, 16:9, 1:1, 3:4, 4:5) covers all social platforms
  - Cross-platform metadata generation is directly useful
  - Auto-thumbnail feature with brand styling
  - Can be invoked as a CLI tool or integrated as a Python library
- **Integration difficulty:** Medium (Python, well-structured, requires FFmpeg + GPU for optimal performance)

### Honorable Mention: thumbnail_generator
- **URL:** https://github.com/Racks-Labs/thumbnail_generator
- **Stars:** 4 (new)
- **License:** Not specified (check before integrating)
- **Language:** Python
- **What it does:** Automatic thumbnail generator: video → AI scene generation (Gemini) → branded text overlay (Pillow + Inter font). A24-inspired cinematic aesthetic.
- **How xPST could use it:** Generate branded thumbnails for scheduled video content automatically
- **Integration difficulty:** Easy (CLI tool, `uv tool install`)

### Honorable Mention: pysnippet/thumbnails
- **URL:** https://github.com/pysnippet/thumbnails
- **Stars:** 17
- **License:** Apache 2.0 ✅
- **Language:** Python
- **What it does:** Fast video thumbnail generator optimized for web players. Generates WebVTT and JSON thumbnail sprite sheets.
- **How xPST could use it:** Generate preview thumbnails for video content in the xPST dashboard
- **Integration difficulty:** Easy (`pip install thumbnails`, clean Python API)

---

## 8. Social Media API Wrappers

### 🏆 Best: SocialReaper
- **URL:** https://github.com/ScriptSmith/socialreaper
- **Stars:** 652
- **License:** MIT ✅
- **Language:** Python
- **Last Updated:** June 2020 (⚠️ no longer maintained)
- **What it does:** Unified Python library for scraping data from Facebook, Twitter, Reddit, YouTube, Pinterest, and Tumblr APIs. Provides consistent interface across platforms with CSV export.
- **How xPST could use it:**
  - Study its unified interface pattern for multi-platform data collection
  - Use as reference for xPST's own API wrapper module
  - Note: Twitter/Facebook APIs have changed significantly since 2020 — use as architectural reference only

**Better Alternative: Build custom API wrappers using platform-specific libraries:**
| Platform | Recommended Library | License |
|----------|-------------------|---------|
| Twitter/X | `tweepy` (4.5k stars) | MIT |
| Instagram | `instagrapi` (3.5k stars) | MIT |
| YouTube | `google-api-python-client` | Apache 2.0 |
| TikTok | `TikTokApi` (2.5k stars) | MIT |
| LinkedIn | `python-linkedin-v2` or direct API | Various |
| Facebook | `facebook-sdk` | Apache 2.0 |
| Reddit | `praw` (3.5k stars) | BSD-2 |
| Bluesky | `atproto` (1.5k stars) | MIT |

- **Integration difficulty:** Medium (multiple libraries, each with own auth flow)

---

## 9. Video Format Conversion

### 🏆 Best: ffmpeg-python
- **URL:** https://github.com/kkroening/ffmpeg-python
- **Stars:** 11k
- **License:** Apache 2.0 ✅
- **Language:** Python
- **Last Updated:** Active (83.5k+ dependents)
- **What it does:** Pythonic FFmpeg wrapper with complex filter graph support. Translates convoluted FFmpeg CLI syntax into readable, maintainable Python code. Supports arbitrarily large directed-acyclic signal graphs.
- **How xPST could use it:**
  - Core video processing: format conversion, trimming, merging, overlay, watermarking
  - Aspect ratio conversion for different platforms (9:16 TikTok → 1:1 Instagram)
  - Audio extraction for transcription pipelines
  - Video compression optimized per platform
  - Watermark/logo overlay on video content
- **Integration difficulty:** Easy (`pip install ffmpeg-python`, clean API, extensive examples)

### Also Useful: yt-dlp
- **URL:** https://github.com/yt-dlp/yt-dlp
- **Stars:** 100k+
- **License:** Unlicense ✅
- **Language:** Python
- **What it does:** Feature-rich audio/video downloader supporting thousands of websites. Post-processing, format selection, metadata embedding.
- **How xPST could use it:** Download reference content, competitor analysis, content repurposing from existing videos
- **Integration difficulty:** Easy (`pip install yt-dlp`, CLI or Python API)

---

## 10. OAuth Management

### 🏆 Best: Authlib
- **URL:** https://github.com/authlib/authlib
- **Stars:** 5.3k
- **License:** BSD ✅
- **Language:** Python
- **Last Updated:** May 2026 (actively maintained, 52.4k dependents)
- **What it does:** The ultimate Python OAuth/OpenID Connect library. Supports OAuth 1.0/2.0, OpenID Connect, JWS/JWE/JWK/JWT. Integrates with Requests, HTTPX, Flask, Django, Starlette, and FastAPI. Can act as both OAuth client and provider.
- **How xPST could use it:**
  - Manage OAuth flows for all social platforms (Twitter, Instagram, Facebook, LinkedIn, TikTok, YouTube)
  - Token storage, refresh, and revocation
  - PKCE support for mobile/SPA flows
  - FastAPI integration is directly relevant to xPST's backend
  - Can also act as an OAuth provider if xPST offers its own API
- **Integration difficulty:** Easy (`pip install authlib`, excellent docs, FastAPI integration built-in)

### Honorable Mention: Django-Allauth
- **URL:** https://github.com/pennersr/django-allauth
- **Stars:** 10.3k
- **License:** MIT ✅
- **Language:** Python (Django)
- **What it does:** Comprehensive authentication/registration with 3rd party social account authentication. Supports 100+ providers. Rate limiting, account enumeration protection, SAML 2.0.
- **How xPST could use it:** If xPST uses Django, this is the gold standard for social auth
- **Integration difficulty:** Easy (if using Django) / Hard (if using FastAPI)

### Honorable Mention: Python Social Auth
- **URL:** https://github.com/python-social-auth/social-app-django
- **Stars:** 2.1k
- **License:** BSD-3-Clause ✅
- **Language:** Python
- **What it does:** Easy setup social authentication/registration. Supports Django, Flask, and other frameworks. 100+ provider backends.
- **How xPST could use it:** Alternative to django-allauth with broader framework support
- **Integration difficulty:** Easy-Medium

---

## Summary Matrix

| # | Category | Best Project | Stars | License | Language | Difficulty |
|---|----------|-------------|-------|---------|----------|-----------|
| 1 | Social Media Scheduling | BrightBean Studio | 1.7k | AGPL-3.0 | Python | Medium |
| 2 | Analytics Dashboards | Plotly Dash | 24.2k | MIT | Python | Easy |
| 3 | Content Calendar | Build Custom (FullCalendar) | 18k | MIT | JS/Python | Medium |
| 4 | AI Caption Generation | LangChain | 139k | MIT | Python | Easy |
| 5 | Hashtag Research | Build Custom (RapidAPI) | — | — | Python | Easy |
| 6 | Best Time to Post | WhenToPost.online | 7 | MIT | TS→Python | Easy |
| 7 | Image/Video Editing | opensource-clipping | 18 | MIT | Python | Medium |
| 8 | Social Media API Wrappers | Build Custom (per-platform) | — | Various | Python | Medium |
| 9 | Video Format Conversion | ffmpeg-python | 11k | Apache 2.0 | Python | Easy |
| 10 | OAuth Management | Authlib | 5.3k | BSD | Python | Easy |

---

## Top Integration Priorities

### Phase 1 — Foundation (Easy, High Impact)
1. **Authlib** — OAuth management for all social platforms
2. **ffmpeg-python** — Video processing backbone
3. **LangChain** — AI caption/content generation pipeline
4. **Plotly Dash** — Analytics dashboard framework

### Phase 2 — Core Features (Medium Effort)
5. **BrightBean Studio** — Study scheduling architecture and platform integrations
6. **opensource-clipping** — Video editing and auto-clipper pipeline
7. **Platform API wrappers** — tweepy, instagrapi, praw, TikTokApi, etc.
8. **Content Calendar** — Build with FullCalendar + custom backend

### Phase 3 — Differentiators (Build Custom)
9. **Hashtag Research** — Custom module with RapidAPI integration
10. **Best Time to Post** — Custom algorithm with historical data learning

---

## Key Takeaways

1. **Python ecosystem is strong** — Authlib, ffmpeg-python, LangChain, Plotly Dash are all Python-native and production-ready
2. **No complete social media scheduler exists in Python** — BrightBean Studio (Django/AGPL) is closest but requires license consideration
3. **Analytics dashboards** — Plotly Dash is the clear winner for Python-based analytics
4. **AI content generation** — LangChain is the right abstraction layer; no mature dedicated caption generator exists
5. **Video processing** — opensource-clipping + ffmpeg-python covers the full pipeline
6. **OAuth is solved** — Authlib handles all social platform OAuth flows
7. **Hashtag research and best-time-to-post** — Must be custom-built; no mature OSS options exist

---

*Research conducted June 7, 2026. All star counts and last-updated dates reflect the time of research.*
