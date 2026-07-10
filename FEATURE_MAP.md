# xPST Feature Map & Open-Source Redundancy

## Platform Auth — Primary & Redundancy

### YouTube
- **Primary**: `google-api-python-client` + `google-auth-oauthlib` (OAuth2 desktop app flow)
- **Redundancy (Open Source)**: `yt-dlp` cookie-based extraction + `yt-dlp` upload via cookies.txt
  
### Instagram
- **Primary**: Instagram Graph API (official, ban-safe, read-only metrics), instagrapi (session-based for posting)
- **Redundancy (Open Source)**: Instagram Private API (another unofficial session-based lib), or direct Cookie-based via yt-dlp source extraction
  
### X/Twitter
- **Primary**: twikit (cookie-based, unofficial), X API v2 (OAuth 1.0a, official, 17 posts/day free tier)
- **Redundancy (Open Source)**: Twitter-API (alternative Python wrapper), raw HTTP with bearer token
  
### TikTok
- **Primary**: yt-dlp (source download, cookie-based), TikTok Content Posting API (official OAuth for destination)
- **Redundancy (Open Source)**: TikTokAPI (unofficial Python wrapper — even more fragile than yt-dlp)
  
### Threads
- **Primary**: Meta Graph API (`graph.threads.net`, v21.0+, official, dev mode)
- **Redundancy (Open Source)**: **None.** Threads has no official open-source lib. Only official Graph API.
  
### LinkedIn
- **Primary**: LinkedIn V2 API (`/rest/posts`, OAuth2 access token)
- **Redundancy (Open Source)**: `python-linkedin` (unmaintained, V1 API), raw `httpx` requests

## Feature Completeness Map

### Core
| Feature | Status | Notes |
|---------|--------|-------|
| Cross-Post Engine | ✅ WORKING | 6 platforms, circuit breakers, crash recovery, anti-bot, quota |
| Bidirectional posting | ✅ WORKING | All sources → all destinations |
| Manual posting (file) | ✅ WORKING | Direct file upload |
| Content dedup | ✅ WORKING | Content hash + cross-post tracking |
| Rate limiting | ✅ WORKING | Exponential backoff 60s→3600s |
| Webhook notifications | ✅ WORKING | Success/failure webhooks |
| Graceful shutdown | ✅ WORKING | Pidfile lock, checkpoint save |

### Sources
| Feature | Status | Notes |
|---------|--------|-------|
| YouTube source | ✅ WORKING | yt-dlp download, multi-format fallback |
| Instagram source | ✅ WORKING | instagrapi, carousel support |
| TikTok source | ✅ WORKING | yt-dlp async download, slideshows |
| Local source | ✅ WORKING | File/directory scan, carousel grouping |

### Posting Destinations
| Feature | Status | Notes |
|---------|--------|-------|
| YouTube upload | ✅ WORKING | google-api-python-client, OAuth2 |
| Instagram upload | ⚠️ PARTIAL | Graph API metrics OK, resumable upload untested |
| X/Twitter upload | ✅ WORKING | twikit + API v2 dual path |
| TikTok upload | ⚠️ PARTIAL | Wizard added, Content Posting API — needs real token |
| Threads upload | ⚠️ PARTIAL | Auth fixes applied — needs Meta Dashboard + App invite |
| LinkedIn upload | ⚠️ PARTIAL | Token verification added — needs real token |

### Auth & Connection
| Feature | Status | Notes |
|---------|--------|-------|
| YouTube connect | ✅ WORKING | OAuth2 browser flow |
| Instagram connect | ⚠️ PARTIAL | Graph API + instagrapi fallback |
| X/Twitter connect | ✅ WORKING | Cookie collection + API v2 |
| TikTok connect | ✅ WORKING | Source cookies + destination wizard |
| Threads connect | ⚠️ PARTIAL | Wizard done — needs token from Meta Dashboard |
| LinkedIn connect | ⚠️ PARTIAL | Verification added — needs real token |

### Infrastructure
| Feature | Status | Notes |
|---------|--------|-------|
| CLI (34 commands) | ✅ WORKING | Click-based, all commands wired |
| Config system | ✅ WORKING | YAML + env vars + Pydantic |
| Credential store | ✅ WORKING | Fernet+scrypt encrypted |
| Session manager | ✅ WORKING | Shared across platforms |
| Config migration (v1→v4) | ✅ WORKING | Incremental, bcrypt migration |
| State manager | ✅ WORKING | SQLite + checkpoint |
| Diagnostics | ✅ WORKING | Redacted ZIP |
| Readiness checks | ✅ WORKING | System + platform health |
| Scheduler | ✅ WORKING | Polling + async variant |
| Schedule manager | ✅ WORKING | JSON-based, recurring |
| Update system | ✅ WORKING | PyPI + smoke test + rollback |
| Plugin system | ✅ WORKING | Directory discovery, sandbox opt-in |

### Advanced Features
| Feature | Status | Notes |
|---------|--------|-------|
| MCP Server | ✅ WORKING | 23 tools, security guardrails |
| Dashboard (API) | ⚠️ PARTIAL | 3 endpoints, needs graphical UI |
| Analytics | ⚠️ PARTIAL | YouTube/IG/X real data; Threads/LinkedIn scraping |
| Analytics persistence | ✅ WORKING | SQLite store |
| Cross-post analytics | ✅ WORKING | DB grouped per cross-post |
| Anti-bot protection | ✅ WORKING | Time-of-day, daily limits, caption variation |
| Circuit breaker | ✅ WORKING | Per-platform, 5 failures then open |
| Desktop app | ⚠️ SCAFFOLDING | PySide6 GUI exists (8 pages) but untested |
| Knowledge system | ⚠️ SCAFFOLDING | Whisper→embed→query pipeline, deps heavy |
| i18n | ⚠️ PARTIAL | English only, no translations shipped |
| Video processing | ✅ WORKING | FFmpeg encoding per platform |
| Content hashing | ✅ WORKING | SHA-256 dedup |

## Bugs Found & Fixed (This Session)
1. ✅ Threads tuple crash: `_get_access_token()` → `(token, user_id)` tuple vs string
2. ✅ Threads token refresh: wrong endpoint `graph.threads.net` → `graph.facebook.com/v21.0`
3. ✅ Credential store: platforms not falling back to config.yaml values
4. ✅ X cookie expiry: stale cookies falsely reported as connected
5. ✅ X uploader: no cookie validation before posting
6. ✅ YouTube source: references TikTok's cookie config instead of its own
7. ✅ Scheduler: `time.sleep()` blocks asyncio event loop — added `run_async()`

## Still Broken / Not Tested
1. ❌ Facebook login checkpoint — manual phone approval needed
2. ❌ Instagram login via React form — CUA background typing doesn't trigger onChange
3. ❌ Threads API token — needs Meta Developer Dashboard access + app invite acceptance
4. ❌ Instagram resumable upload — `rupload.facebook.com` endpoint untested
5. ❌ Threads/LinkedIn analytics — yt-dlp scraping unreliable, will return empty for users
6. ❌ Desktop app — PySide6 GUI, no real-world testing done
7. ❌ Knowledge system — heavy deps (torch, whisper), embedded query pipeline untested
