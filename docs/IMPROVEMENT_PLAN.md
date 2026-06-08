# XPST — Deep Research & Improvement Plan

## Questions Asked & Answered

### 1. Are we using the most effective programming language?

**Answer: Python is the correct choice. Here's why:**

| Language | Pros | Cons | Verdict |
|----------|------|------|---------|
| **Python** ✅ | Best ecosystem (twikit, instagrapi, google-api-python-client, yt-dlp). Fast development. Rich CLI frameworks (Click). Huge community. | Slower startup (~200ms). Requires Python installed. | **Best for this use case** |
| Go | Single binary. Fast startup (~5ms). Good concurrency. | No native Instagram/X libraries. Would need to rewrite or call Python. Limited async ecosystem for social media. | Not worth rewriting |
| Rust | Fastest execution. Memory safe. Single binary. | Massive development overhead. No social media API libraries. Would take 10x longer to build. | Overkill |
| Node.js | Good async. npm ecosystem. | Worse CLI tooling. No instagrapi equivalent. Callback hell. | Worse than Python |

**Key insight:** The bottleneck is network I/O (API calls), not CPU. Python's async capabilities are more than sufficient. The ecosystem advantage (instagrapi, twikit, yt-dlp) is insurmountable — rewriting these in Go/Rust would take months.

**Recommendation:** Stay with Python. Focus optimization efforts on network efficiency, not language choice.

---

### 2. Why doesn't it have persistent login sessions?

**Current state:** We store credentials in JSON files but don't implement proper session persistence.

**Problems identified:**

| Platform | Current Approach | Problem | Solution |
|----------|-----------------|---------|----------|
| **YouTube** | OAuth2 token in JSON file | Token expires after 1 hour. Refresh token expires after 7 days (Testing mode). | Implement automatic token refresh. Publish OAuth app to Production. |
| **Instagram** | sessionid from browser cookie | Session expires frequently (days/weeks). No refresh mechanism. | Use instagrapi's `dump_settings()`/`load_settings()` for full session persistence. |
| **X/Twitter** | Cookies from browser export | Cookies expire. No refresh mechanism. | Use twikit's `save_cookies()`/`load_cookies()` with automatic re-login. |

**Solution — Session Manager:**

```python
class SessionManager:
    """Persistent session management with automatic refresh"""
    
    def __init__(self, config_dir: str):
        self.sessions_dir = Path(config_dir) / "sessions"
        self.sessions_dir.mkdir(exist_ok=True)
    
    async def get_youtube_service(self):
        """Get YouTube service with automatic token refresh"""
        # Load token
        # If expired, refresh automatically
        # Save refreshed token
        # Return service
    
    async def get_instagram_client(self):
        """Get Instagram client with session persistence"""
        # Load full session settings (not just sessionid)
        # If session invalid, re-login with stored credentials
        # Save session settings
        # Return client
    
    async def get_x_client(self):
        """Get X client with cookie persistence"""
        # Load cookies
        # If expired, re-login with stored credentials
        # Save cookies
        # Return client
```

**Key changes needed:**
1. Store full Instagram session settings (device info, UUIDs, etc.)
2. Implement automatic token refresh for YouTube
3. Add re-login capability for X and Instagram
4. Add session health checks before each operation

---

### 3. How can we make revalidation easy?

**Current state:** Users must manually re-authenticate when sessions expire.

**Solution — Interactive Auth Manager:**

```bash
# Check auth status for all platforms
xpst auth status

# Re-authenticate specific platform
xpst auth youtube
xpst auth x
xpst auth instagram

# Auto-refresh all sessions
xpst auth refresh
```

**Implementation:**
1. **YouTube:** Use `flow.run_local_server()` with automatic browser opening
2. **Instagram:** Support both sessionid and username/password login
3. **X:** Support both cookies and username/password login

---

### 4. How can we make it safe for anyone to run?

**Security concerns identified:**

| Concern | Risk | Mitigation |
|---------|------|------------|
| **Credential storage** | JSON files can be read by any process | Use OS keychain (keyring library) |
| **Session files** | Contain sensitive auth tokens | Encrypt at rest |
| **Config file** | May contain usernames | Separate credentials from config |
| **Logs** | May leak sensitive info | Sanitize logs, never log credentials |
| **Network** | API calls over HTTPS | Enforce HTTPS, certificate validation |

**Solution — Keyring Integration:**

```python
import keyring

# Store credentials in OS keychain
keyring.set_password("xpst", "youtube_token", token_json)
keyring.set_password("xpst", "instagram_sessionid", sessionid)
keyring.set_password("xpst", "x_cookies", cookies_json)

# Retrieve credentials
token = keyring.get_password("xpst", "youtube_token")
```

**Benefits:**
- macOS: Keychain (encrypted, requires Touch ID/password)
- Windows: Credential Locker (encrypted, tied to user account)
- Linux: Secret Service (GNOME Keyring, KWallet)
- Never stored in plain text on disk

---

### 5. What are the API rate limits?

**Research findings:**

| Platform | Endpoint | Rate Limit | Impact |
|----------|----------|------------|--------|
| **YouTube** | `videos.insert` | 1,600 quota units/upload | **Max 6 uploads/day** (10,000 unit daily quota) |
| **YouTube** | `channels.list` | 1 unit/read | Unlimited health checks |
| **Instagram** | Reels publish | **25 posts/24 hours** (hard limit) | Can't exceed even with multiple accounts |
| **Instagram** | API calls | 200 requests/hour | Health checks and uploads |
| **X/Twitter** | Media upload (Free) | **17 requests/24 hours** | Very limited! |
| **X/Twitter** | Media upload (Pro) | 500 requests/15 minutes | Much better |
| **X/Twitter** | Tweet create (Free) | 50 tweets/24 hours | Moderate |

**Critical finding:** X/Twitter Free tier only allows 17 media uploads per 24 hours. This means:
- Can't backfill many videos
- Need intelligent queuing
- Should track quota usage

**Solution — Quota Manager:**

```python
class QuotaManager:
    """Track and enforce API rate limits"""
    
    def __init__(self):
        self.quotas = {
            "youtube": {"daily_uploads": 6, "used": 0, "reset": midnight},
            "instagram": {"daily_uploads": 25, "used": 0, "reset": midnight},
            "x": {"daily_uploads": 17, "used": 0, "reset": midnight},
        }
    
    def can_upload(self, platform: str) -> bool:
        """Check if we can upload to a platform"""
        quota = self.quotas[platform]
        return quota["used"] < quota["daily_uploads"]
    
    def record_upload(self, platform: str):
        """Record an upload against the quota"""
        self.quotas[platform]["used"] += 1
```

---

### 6. How can we make it run smoother?

**Performance bottlenecks identified:**

| Bottleneck | Current | Optimization |
|------------|---------|--------------|
| **FFmpeg encoding** | Sequential, ~30-60s per video | Parallel encoding with multiprocessing |
| **Platform uploads** | Sequential | Parallel uploads with asyncio.gather() |
| **yt-dlp extraction** | Sequential | Cache metadata, batch extraction |
| **State saves** | After each video | Batch saves, write-ahead log |

**Solution — Parallel Processing:**

```python
async def process_video(video, platforms):
    """Process video with parallel encoding and uploads"""
    
    # Step 1: Encode for all platforms in parallel
    encode_tasks = [
        encode_for_platform(video, platform)
        for platform in platforms
    ]
    encoded_paths = await asyncio.gather(*encode_tasks)
    
    # Step 2: Upload to all platforms in parallel
    upload_tasks = [
        upload_to_platform(encoded_path, platform)
        for encoded_path, platform in zip(encoded_paths, platforms)
    ]
    results = await asyncio.gather(*upload_tasks, return_exceptions=True)
    
    return results
```

**Expected improvement:** 3x faster for 3 platforms (encoding parallelized, uploads parallelized).

---

### 7. What monitoring/alerting should we add?

**Current state:** Basic logging to file.

**Proposed monitoring stack:**

| Component | Purpose | Implementation |
|-----------|---------|----------------|
| **Structured logging** | Machine-readable logs | Already implemented (structlog) |
| **Metrics** | Track upload counts, failures, latency | Prometheus client (already in deps) |
| **Health endpoint** | External monitoring | HTTP server on configurable port |
| **Webhook notifications** | Alert on failures | Discord/Telegram/Slack webhooks |
| **Dashboard** | Visual status | Simple HTML dashboard |

**Solution — Webhook Notifier:**

```python
class WebhookNotifier:
    """Send notifications on events"""
    
    async def notify_success(self, platform, video_url):
        """Notify on successful upload"""
        await self._send(f"✅ Posted to {platform}: {video_url}")
    
    async def notify_failure(self, platform, error):
        """Notify on failure"""
        await self._send(f"❌ Failed to post to {platform}: {error}")
    
    async def notify_quota_warning(self, platform, remaining):
        """Notify when quota is running low"""
        await self._send(f"⚠️ {platform} quota: {remaining} remaining")
```

---

### 8. Cross-platform distribution

**Current state:** pip install only.

**Distribution channels:**

| Channel | Pros | Cons | Priority |
|---------|------|------|----------|
| **PyPI** | Standard Python distribution | Requires Python installed | High |
| **Homebrew** | Easy macOS install | Requires formula maintenance | Medium |
| **Docker** | Consistent environment | Requires Docker installed | Medium |
| **GitHub Releases** | Direct download | Manual installation | Low |
| **Snap/Flatpak** | Linux distribution | Limited audience | Low |

**Solution — Multi-channel distribution:**

```yaml
# .github/workflows/release.yml
- name: Publish to PyPI
  run: twine upload dist/*

- name: Create GitHub Release
  uses: softprops/action-gh-release@v1
  with:
    files: dist/*

- name: Build Docker Image
  run: docker build -t xpst .
```

---

## Comprehensive Improvement Plan

### Phase 1: Security & Auth (Week 1)

| Task | Priority | Effort | Impact |
|------|----------|--------|--------|
| Implement keyring integration for credential storage | HIGH | 2 days | Security |
| Implement instagrapi session persistence (dump_settings/load_settings) | HIGH | 1 day | Reliability |
| Implement YouTube automatic token refresh | HIGH | 1 day | Reliability |
| Add session health checks before each operation | MEDIUM | 1 day | Reliability |
| Add `xpst auth status` command | MEDIUM | 0.5 day | UX |

### Phase 2: Performance & Reliability (Week 2)

| Task | Priority | Effort | Impact |
|------|----------|--------|--------|
| Implement parallel video encoding | HIGH | 1 day | Performance |
| Implement parallel platform uploads | HIGH | 1 day | Performance |
| Add quota tracking and enforcement | HIGH | 1 day | Reliability |
| Add dead letter queue for failed uploads | MEDIUM | 1 day | Reliability |
| Add retry with exponential backoff for all API calls | MEDIUM | 0.5 day | Reliability |

### Phase 3: Monitoring & Alerting (Week 3)

| Task | Priority | Effort | Impact |
|------|----------|--------|--------|
| Add webhook notifications (Discord/Telegram) | MEDIUM | 1 day | Observability |
| Add Prometheus metrics endpoint | LOW | 1 day | Observability |
| Add simple HTML dashboard | LOW | 1 day | Observability |
| Add structured JSON logging option | LOW | 0.5 day | Observability |

### Phase 4: Distribution & Documentation (Week 4)

| Task | Priority | Effort | Impact |
|------|----------|--------|--------|
| Publish to PyPI | HIGH | 0.5 day | Distribution |
| Create Homebrew formula | MEDIUM | 1 day | Distribution |
| Add comprehensive error messages | MEDIUM | 1 day | UX |
| Add video tutorial / setup guide | LOW | 1 day | Adoption |

---

## Technical Debt to Address

| Issue | Priority | Fix |
|-------|----------|-----|
| Python 3.10+ requirement | HIGH | Document clearly, consider 3.9 support |
| twikit dependency on Python 3.10+ | HIGH | Can't fix, document limitation |
| No Windows testing | MEDIUM | Add Windows CI/CD |
| No integration tests | MEDIUM | Add mock-based integration tests |
| Config validation could be stricter | LOW | Add more validation rules |

---

## Competitive Analysis

| Feature | XPST | Buffer | Hootsuite | Later |
|---------|-----------|--------|-----------|-------|
| **Price** | Free | $6/mo | $99/mo | $25/mo |
| **Self-hosted** | ✅ | ❌ | ❌ | ❌ |
| **Open source** | ✅ | ❌ | ❌ | ❌ |
| **TikTok source** | ✅ | ❌ | ❌ | ❌ |
| **Video encoding** | ✅ | ❌ | ❌ | ❌ |
| **CLI** | ✅ | ❌ | ❌ | ❌ |
| **API** | Planned | ✅ | ✅ | ✅ |
| **Analytics** | Planned | ✅ | ✅ | ✅ |
| **Web UI** | Planned | ✅ | ✅ | ✅ |

**XPST's unique advantages:**
1. Free and open source
2. Self-hosted (data stays local)
3. TikTok-first workflow
4. Platform-optimized video encoding
5. CLI-first (developer friendly)
6. No subscriptions

---

## Summary

**Python is the right choice** — the ecosystem advantage is insurmountable.

**Key improvements needed:**
1. **Security:** Keyring integration for credential storage
2. **Reliability:** Persistent sessions with automatic refresh
3. **Performance:** Parallel encoding and uploads
4. **Observability:** Webhook notifications and metrics
5. **Distribution:** PyPI + Homebrew + Docker

**Timeline:** 4 weeks to production-grade.

**Unique value:** Only free, open-source, self-hosted tool with TikTok-first workflow and platform-optimized encoding.