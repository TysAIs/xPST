# xPST — Configuration & Integration Audit
**Audit date:** 2026-08-26 (MDT) · **Host:** this Mac (itxji) · **Config home:** `/Users/itxji/.xpst/`
**Method:** read of all config/credential/state files + live CLI checks (`python -m xpst` from `/Users/itxji/xPST/.venv`) + live network checks (TikTok OAuth token endpoint, tysais.github.io, github.com). Every verdict cites file content or command output below.
**CLI note:** `/Users/itxji/.local/bin/xpst` is **not** the xPST CLI — it is a Hermes bot wrapper (`exec …/hermes -p xpst "$@"` → the `xpst` *bot profile*). The real CLI is `/Users/itxji/xPST/.venv/bin/python -m xpst` (v1.0.0, py3.11.15, yt-dlp 2026.3.17, instagrapi 2.8.16, twikit 2.3.3).

---

## 1. Config inventory — `/Users/itxji/.xpst/config.yaml` (155 lines, version: 4)

### accounts.*
| Key | Value | Plain-English purpose | Verdict |
|---|---|---|---|
| `accounts.tiktok.username` | `tys.ais` | TikTok handle used as the cross-posting **source** (download) account | WORKING (readiness: `tiktok_username: true`; health source ok) |
| `accounts.tiktok.cookies_from_browser` / `cookies_file` | true / `credentials/tiktok_cookies.txt` | Use CDP-exported Netscape cookie jar for yt-dlp downloads (TCC workaround) | WORKING (jar exists, 43 lines, valid to ~2027; health: `cookies_available: true`) |
| `accounts.tiktok.client_key` / `client_secret` | `[REDACTED-CLIENT-KEY]` / `[REDACTED-TIKTOK-SECRET]` | TikTok **Content Posting API** app (uploader) credentials | WORKING credentials — live client-credentials token test against `open.tiktokapis.com/v2/oauth/token` **SUCCEEDED** (`access_token clt.2.3JSDMS-…, expires_in 7200`). Skill note that the app "rejects the client_key in Draft" is now **STALE** — the app has been approved. |
| `accounts.tiktok.access_token` / `refresh_token` | `''` / `''` | Stored user-OAuth tokens for the uploader | **BROKEN/INCOMPLETE** — empty, so uploader reports `TIKTOK_NOT_CONFIGURED` in `xpst health`. Only the client-credentials flow works; the user-OAuth `xpst auth tiktok` step (needs Tyler's phone) was never completed. |
| `accounts.tiktok.sandbox` | false | TikTok sandbox mode toggle | NOT-USED (prod app) |
| `accounts.youtube.enabled` | true | Destination on | WORKING |
| `accounts.youtube.client_secrets` | `credentials/youtube_client_secrets.json` | Google OAuth client (production app `xpst-prod-87871`, client `88230346195-…`) | WORKING — health live: `authenticated: true, session_valid: true`, channel **“Ty's AI's”** `[REDACTED-CHANNEL-ID]` |
| `accounts.youtube.token_file` | `credentials/youtube_token.json` | OAuth token (has long-lived refresh token; app is In Production → no weekly expiry) | WORKING — 17:02 log: “YouTube credentials refreshed successfully”; health valid |
| `accounts.youtube.channel_id` | `[REDACTED-CHANNEL-ID]` | Destination channel (Ty's AI's / JaceDigital brand) | WORKING; **replaced** the earlier `UCRF2kQGQ6DJiWR4BbY4SPqA` “gumbo racing” channel (see §5) |
| `accounts.youtube.username` | `''` | Unused field | NOT-USED |
| `accounts.x.enabled` / `cookies_file` | true / `x_cookies.json` (+ `.enc`) | X destination via twikit cookie/session auth | WORKING — health live: `session_valid: true`, username `tys_ais`, user_id `2059868840004403200` |
| `accounts.x.username` | `tys_ais` | X handle | WORKING (matches live) |
| `accounts.x.auth_mode` | `cookies` | twikit **unofficial** API (vs official API v2) | WORKING, but logs `Using unofficial API for x — may violate platform ToS` every post; ToS/robustness caveat |
| `accounts.instagram.enabled` / `session_file` | true / `instagram_session.json` (+ `.enc`) | IG destination | **BROKEN** — `xpst health`: `authenticated: false, session_valid: false, error: “Instagram session expired or invalid… Re-run: xpst connect instagram”`. Live 403 `LoginRequired` from `i.instagram.com/api/v1/users/80131363736/info_stream/`. |
| `accounts.instagram.username` | `''` | Unused | NOT-USED |
| `accounts.instagram.auth_mode` | `graph_api` | Declares **Meta Graph API** mode — but `config.instagram.graph_ig_user_id` is not set and no IG Graph token is stored in the CredentialStore (stored = session only) | **BROKEN/INCOMPLETE** — declared but never provisioned; the only stored IG credential is the dead instagrapi session (§2) |
| `accounts.instagram.device_id` | null | instagrapi device fingerprint | NOT-USED (session auth only) |
| `accounts.threads.*` | disabled, all tokens `''` | Threads destination | NOT-USED (deliberately disabled; not listed by `xpst quota`) |
| `accounts.messenger.*` | disabled, `auto_reply: false`, all tokens `''` | FB Messenger auto-reply (opt-in adapter) | NOT-USED (opt-in, never enabled) |
| `accounts.local.path` | `''` | Local-folder content source | NOT-USED (readiness: `local_path_exists: false`) |

### video.* / video.encoding.*
| Key | Verdict |
|---|---|
| `download_dir` → `/Users/itxji/.xpst/downloads` | WORKING dir; contains only one old 130 KB test clip (§3), `cleanup_after_post: false` |
| `encoding.youtube` (passthrough true) | WORKING-if-used — no re-encode path for YT |
| `encoding.instagram` (720p/CRF23/3.5M/main/L3.0/GOP72/30fps) | Realistic IG Short limits; can't be exercised until IG auth fixed — **BLOCKED-ON-IG** |
| `encoding.x` (1080p/10M/12M/high/L4.0/GOP90/30fps) | WORKING — X posts encoded at these settings on 08-25 test |

### reliability / monitoring / schedule / rate_limits / anti_bot / circuit_breaker
| Key | Purpose | Verdict |
|---|---|---|
| `reliability.*` (retries 3, backoff 2, CB threshold 5 / reset 3600) | Retry/circuit-breaker tuning | WORKING defaults; `health → circuit_breakers: {}` = never tripped |
| `monitoring.log_level/file/rotation` | Logging | WORKING (`logs/xpst.log` rotating) |
| `monitoring.healthcheck_port` = 8080, `enable_metrics` true | Dashboard/health server | **NOT-USED** — `lsof :8080` → nothing listening; no dashboard/daemon running |
| `monitoring.dashboard_username/hash` (both `''`) | Dashboard auth | NOT-USED (open if dashboard launched) |
| `schedule.*` (900 s check, 48 h catchup, 3×/day) | `xpst watch` pacing | **NOT-USED** — no watch daemon/scheduler running (§4) |
| `rate_limits.*` all 5 | Per-platform daily cap | **PARTIALLY-BROKEN** — runtime `xpst quota` reports IG 25 and X 10 (not 5); youtube 5 honored. Config is not authoritative; mismatched with `quotas.json` too (§3/§4) |
| `notifications.*` (enabled false, discord/telegram empty) | Discord/Telegram alerts | NOT-USED (off; no endpoints) |
| `bio.*` (empty) | Link-in-bio page | NOT-USED (needs dashboard) |
| `shortcuts.*` | Desktop-app keys | NOT-USED (desktop not run) |
| `first_run_complete` | **false** | **STALE** — auth is largely done but the flag / first-run workflow never completed (wizard_state.json absent, §4) → FIX |
| `provider_mode: official` | Platform API mode | LEGACY default marker |
| `version: 4` | Config schema | CURRENT (auto-migrated) |
| `video_processing.*` (250 MB / CRF23 / medium / auto_convert) | Re-encode defaults | WORKING-if-used (sane defaults) |
| `sources.*` — **all four empty** (`tiktok.username`, `youtube.channel_id`, `x.user_id`, `instagram.username`) | Redundant source-descriptor block | **LEGACY/DUPLICATE** — superseded by `accounts.*`; readiness reads `accounts.tiktok.username`, not `sources.*` → REMOVE |
| `anti_bot.*` (enabled, 2–10 s delay, 0.3 jitter) | Human-like pacing | WORKING-if-used; benign; KEEP |
| `circuit_breaker.*` (threshold 5 / recovery 300 / half-open 3) | CB tuning | **DUPLICATE** — same concept already in `reliability.circuit_breaker_*`; two schemas govern one behavior → FIX (pick one) |

---

## 2. Credentials & tokens — `/Users/itxji/.xpst/credentials/`

Storage mode confirmed by CLI: **“File Storage (fallback)”** — Fernet/scrypt `.enc` files + fallback key files. NOT keychain (`XPST_USE_KEYRING=1` is the opt-in). `xpst auth status` reports stored credentials: `instagram_session, x_cookies, youtube_token`.

| File | Provider | Format | Validity / notes |
|---|---|---|---|
| `youtube_client_secrets.json` | Google (YouTube) | OAuth client JSON — production project `xpst-prod-87871`, client `88230346195-…`, app type `installed` | **VALID**; matches token file client_id |
| `youtube_token.json` | Google (YouTube) | OAuth token JSON: access token, **refresh_token present** (long-lived — app In Production), scopes `youtube.upload + readonly + force-ssl`, expiry `2026-08-27T00:02Z` | **VALID/PASS** (`xpst health` session_valid true; 17:02 refresh OK; refresh token enables auto-refresh) |
| `youtube_token.enc` | Google | Encrypted store mirror (14:35 mtime) | VALID (runtime mirror) |
| `x_cookies.json` | X/Twitter | twikit cookie set: `auth_token`, `twid=u%3D2059868840004403200`, `ct0`, guest/cf tokens | **VALID/PASS** (health session_valid true; user id matches) |
| `x_cookies.enc` | X/Twitter | Encrypted store mirror | VALID |
| `instagram_session.json` | Instagram | instagrapi session: `sessionid` user `80131363736`, `csrftoken`, `rur`, `mid`, `datr` | **INVALID/FAIL** — 403 `LoginRequired`; session revoked/expired (written 08-24); needs `xpst connect instagram` with Tyler's password (one-time) |
| `instagram_session.enc` | Instagram | Encrypted store mirror (17:26) | Same dead session — INVALID |
| `tiktok_cookies.txt` | TikTok (source only) | Netscape jar, 43 lines, session cookies (`tt_csrf`, `passport_csrf`, `multi_sids` shows **2 accounts**: `7429416641148945451` + `7600920081380213815`), expiries ~2027 | **VALID for source downloads** (health ok); **irrelevant to uploader** |
| `tiktok_dev_account.txt` | TikTok Developer Portal | Plaintext metadata: `EMAIL: [REDACTED-EMAIL]`, `PASSWORD: [REDACTED]` | VALID aid (not read by xPST runtime); **plaintext credential note** |
| `.fallback_salt` (16 B) / `.fallback_secret` (32 B) | — | Fernet/scrypt fallback key material | **WORKING** (backing the encrypted store) |
| **Config-inline tokens** | TikTok | `client_secret` in plaintext inside config.yaml | VALID (needed for token requests); mirrors nothing else |

### Live validity checks (run 2026-08-26)
| Provider / component | Command / test | Result |
|---|---|---|
| YouTube | `xpst health` | **PASS** — authenticated, session valid, channel “Ty's AI's” `[REDACTED-CHANNEL-ID]` |
| X | `xpst health` | **PASS** — authenticated, session valid, `tys_ais` / `2059868840004403200` |
| Instagram | `xpst health` | **FAIL** — session expired/invalid (403 LoginRequired); `xpst auth status` misleadingly reports `authenticated: true` (file-exists check, not a real probe) |
| TikTok source | `xpst health` | PASS — cookies available, yt-dlp present |
| TikTok uploader | `xpst health` | **FAIL** — `TIKTOK_NOT_CONFIGURED: Set access_token (and client_key/client_secret/refresh_token…)` |
| TikTok API reachability | `curl -X POST open.tiktokapis.com/v2/oauth/token` (client_credentials) | **PASS** — returned valid `clt.*` access_token ⇒ app approved; **only the user-OAuth token capture is missing** |
| Threads/Messenger | `xpst health` | n/a — intentionally disabled |
| `xpst readiness` | — | ready:true, blocking:none (file-existence checks only — does **not** catch the invalid IG session or missing TikTok token) |

**Honest per-provider status:** YouTube ✅ · X ✅ · TikTok source ✅ / uploader ⚠️ (creds valid, flow incomplete) · Instagram ❌ · Threads/Messenger n/a (off).

---

## 3. State / quotas / analytics / sessions / plugins / backups

| Item | Contents | Orphans / stale / dup / dead refs |
|---|---|---|
| `state.json` (588 B, 08-25 20:27) | v2; `posted_videos: {}` (test posts deleted); `total_processed: 3`; platform_health all `ok` with last_success youtube/x `2026-08-26T02:27` | **Optimistic health** — instagram/tiktok marked `ok` though IG session is invalid and TikTok uploader unconfigured (no last_success ever). `total_videos_tracked: 0` yet `total_processed: 3` |
| `state.json.forensic` | Byte-identical to state.json (diff = identical, same mtime 20:27:36) | Benign forensic snapshot; no divergence |
| `.state.lock` | 0-byte lock | Stale/benign (watchdog rewrites at startup; no writer process) |
| `quotas.json` | youtube used 1, x used 2, ig/tiktok 0 — all `daily_limit: 5`, some last_reset present | **STALE + misaligned**: runtime `xpst quota` reports IG `daily_limit 25`, X `10`, and *remaining = limit (not limit−used)* — youtube used 1/5 → “remaining 5”. File is not the runtime source of truth; matches the false `QUOTA_EXHAUSTED` warnings and 248 MCP `guardrail_block` refusals (§4) |
| `analytics.db` | `cross_post_groups` 2 · `metric_snapshots` 30 · `follower_snapshots` 0→1 | **Dead refs:** 1 real row (`test-video-2026-08-26-01daa27c` → youtube `hApVcihAD-Y` + x `2092438721056604567`, the deleted test post) and **1 pytest artifact row** (`test-8da2503a`, source `/private/var/…/pytest-of-itxji/pytest-90/…`, post_id `post123`, url `https://example.com/post123`) that should be purged. metric_snapshots are all **youtube-only** (2 batches 08-26 02:30/02:35); zero metrics ever stored for X/IG/TikTok (IG/TikTok never posted) |
| `sessions/` | **EMPTY** dir | Orphan — runtime sessions moved to `credentials/*.enc`; dir unused |
| `plugins/` | **EMPTY**; `xpst plugins list` → `{"plugins": [], "count": 0}` | Feature unused, no orphans |
| `backups/` | 10 `config.yaml.backup_*` (08-20→08-26 13:16) + 5 `state_*.json` (08-25 20:27) | Historical drift evidence (§5): LinkedIn keys removed; TikTok client_key/secret churned; `accounts.youtube.channel_id` changed; `anti_bot`/`circuit_breaker`/`video_processing`/`sources`/`version` added by migration. **2×/min same-day config saves (13:09→13:16) = churn from config edits on 08-26** — 5 redundant snapshots in 7 min. 4/5 state backups still carry the deleted test post. No dead refs beyond that |
| `diagnostics/` | 1 bundle `xpst-diagnostics-20260825T111235Z.zip` (diagnostics.json 38 KB + README) | One-off, healthy |
| `downloads/` | `xpst-pipeline-test.mp4` (130 KB, 08-19) | **Orphan test artifact** (downloads otherwise empty; `cleanup_after_post: false`) |
| `logs/` | `xpst.log` (46 KB) + `mcp_audit.jsonl` (622 entries) | Log: 22 error/failed lines incl. the (fixed) `object dict can't be used in 'await'` YT bug and **recurrent false `QUOTA_EXHAUSTED`** warnings. mcp_audit: 374 success / **248 `guardrail_block` failures** (08-16), recent activity up to 08-26 |
| `thumbnails/`, `translations/` | both **EMPTY** | Features never used (thumbnailing, i18n) |
| `xpst.pid` | `{"pid": 19942, "started_at":"2026-08-25T05:16:27"}` | **STALE** — pid 19942 is gone from `ps`; no xPST daemon is running (only a live test-suite pytest + the Hermes bot gateway). Nothing on port 8080 either |
| `/Users/itxji/xPST/tiktokH5LJ7htLuZUf7F8WChFfCvLuMRklJEns.txt` (repo ROOT) | `tiktok-developers-site-verification=H5LJ7…` | **REQUIRED, KEEP** — TikTok URL-prefix ownership proof for `tysais.github.io/xPST/` (site verified HTTP 200) |

---

## 4. Optional features Tyler may have enabled

| Feature | What it is | Does it work? | Recommendation |
|---|---|---|---|
| **Bots** (Hermes `xpst` profile) | AI operator bot owning the xPST domain; gateway running via LaunchAgent `ai.hermes.gateway-xpst.plist` (live proc 43629/43626); own daily `xpst-mission-tick` (06:00, last_status ok) | ✅ **Works** (gateway alive, ticker heartbeat 08-26 17:06, job ok) — but **model drift**: profile config is `provider: nous, model: stealth/ox-alpha` (cloud, fallback local DS4 `10.0.0.4:8888`) while its SOUL.md asserts “You run on the free local fleet engine: Ornith-1.5-35B-A3B-NVFP4 on aragorn :8889” → unintended cloud spend / contradiction | **KEEP** (it's the domain owner) — **FIX**: update SOUL/MEMORY, and reconcile the duplicate mission ticks (default-profile `xpst-mission-tick` 11:00 Mon–Fri deliver `bot-chat:xpst` **plus** the bot's own 06:00 tick = two overlapping jobs) |
| **Fleet crons** (default profile) | `fleet-night-watch bacd39a44502` (every 120 m) → `telegram:8252299635` (live chat, ok); dead chat `0638436d9d83` no longer used (only 8252299635 in usage_audit) — **already retargeted**; legacy `fleet-night-watch 17a8c2cf5f52` (auto-spawner) disabled; `fleet-morning-brief 3d9138f782a7` disabled with `drift_skip` error (model ornith→deepseek drift); `gcp-publish-watcher` paused; `tiktok-oauth-completion 8c230d0f30ae` **enabled every 30 m** | Active watch ✅; tiktok-oauth-completion keeps firing but its goal (approved app) is effectively done — only the final token-capture OAuth step remains, which **no cron can do** (needs Tyler's phone/consent) | **KEEP** night-watch & huddle; **FIX**: delete/archive the two disabled legacy jobs + disabled morning-brief (or re-pin model); **tiktok-oauth-completion → complete the one-time OAuth then disable** (don't let it run every 30 min indefinitely) |
| **Wizard / first-run** | `xpst wizard` (PR #47) with resumable state file `~/.xpst/wizard_state.json` + per-platform click-by-click guides | **Never run to completion** — `wizard_state.json` absent AND `first_run_complete: false` in config despite auth mostly complete | **FIX/RUN ONCE** with Tyler's phone, then KEEP — it is exactly the vehicle for the two missing OAuth steps (IG + TikTok) and would clear the stale flag |
| **Quotas** | `quotas.json` + runtime guardrails | ⚠️ **Broken lineage**: config `rate_limits` all=5, `quotas.json` all=5, **runtime** reports IG 25 / X 10; `remaining = limit` (not limit−used); repeated false `QUOTA_EXHAUSTED … 0 uploads remaining today` log warnings AND 248 MCP `guardrail_block` refusals on 08-16; duplicate `circuit_breaker` section | **FIX** — single source of truth + correct remaining math + verify the guardrail check (it has blocked the MCP tools) — then KEEP (it protects YouTube's real 10,000 unit / 6-upload headroom which the 08-25 test respected) |
| **Notifications** | Discord/Telegram alerting (off) | n/a — `enabled: false`, webhook_url & bot_token empty | **KEEP disabled** (fleet already reports via `telegram:8252299635`); wire xPST's own Telegram later if wanted |
| **Plugins** | Plugin system (`xpst plugins`, MCP `xpst_plugins*`) | System present, zero plugins installed (`plugins/` empty, count 0) | **KEEP** (harmless, ready); no action |
| **Dashboard/desktop/health** | `xpst dashboard` (FastAPI :8080), desktop app, metrics | Not running — nothing on :8080; `monitoring.healthcheck_port` unbound; dashboard auth unset | **KEEP** code, no runner configured; start dashboard on demand if Tyler wants the bio/analytics UI |
| **Scheduling** (`xpst watch`/`run`) | Auto cross-posting loop (`schedule.check_interval 900`) | **Not running** — no daemon, no launchd for the app, no crontab rows; the engine only runs on manual invocation or MCP/bot-triggered jobs | **FIX/OPERATIONAL GAP** — if auto-posting is intended, this is the single biggest missing piece; otherwise document that posting is manual/bot-triggered |

---

## 5. References to removed devices, old accounts, dead URLs

1. **LinkedIn — removed feature, refs remain.** Code removed (commit c398780), config keys gone (backups 08-20/08-24 still carry `accounts.linkedin.*` + `rate_limits.linkedin`; current config has none). Still referenced by: xpst bot `SOUL.md` (“…Threads, and **LinkedIn**”), bot `memories/OPERATING.md` (“…**LinkedIn** … ALL false … next to fix”), and the default-profile `xpst-mission-tick` prompt (“…tiktok/**linkedin** likely need Tyler”). → **Stale text only; REMOVE refs, do not re-add the platform.**
2. **Old YouTube channel “gumbo racing” `UCRF2kQGQ6DJiWR4BbY4SPqA`.** Replaced by “Ty's AI's” `[REDACTED-CHANNEL-ID]` (config + live health). No occurrence left anywhere under `~/.xpst` or repo docs/README/configs (grep = 0) — but **still stale inside** bot `memories/OPERATING.md` (“channel 'gumbo racing.'”) and the xpst-cross-posting skill body. → Update bot memory/skill.
3. **Dead process / port refs.** `xpst.pid` → pid 19942 (not running); port 8080 healthcheck configured but unbound. → Remove/ignore pid; treat health server as on-demand.
4. **Dead/test artifact refs.** `downloads/xpst-pipeline-test.mp4` (08-19 test file); analytics `cross_post_groups` pytest row (`https://example.com/post123`, `post_id 'post123'`); 5 redundant same-minute config backups (08-26 13:09–13:16); 4 state backups holding a deleted test post. → Purge/archive.
5. **Empty/legacy dirs & config.** `sessions/`, `thumbnails/`, `translations/`, `plugins/` empty; `sources.*` block all-empty duplicate of `accounts.*`; `accounts.youtube.username`/`accounts.instagram.username` unused. → REMOVE.
6. **URLs checked live (not dead):** `https://tysais.github.io/xPST/` → HTTP 200; `https://github.com/TysAIs/xPST` → HTTP 200; git remote origin = `https://github.com/TysAIs/xPST.git` (HTTPS, reachable). TikTok developer-site verification file in repo root is **required and live** — keep.
7. **Credential cross-refs:** `tiktok_dev_account.txt` contains plaintext TikTok dev-portal password (`[REDACTED]`) and the live email `[REDACTED-EMAIL]`; `youtube_token.json` duplicates the client_id/secret (normal Google pattern). No dead emails/keys found in config; no 404 URLs found.

---

## Top-line summary (condensed)

- **Working:** YouTube (production OAuth, long-lived), X (cookie auth), TikTok **source** (cookie jar), TikTok uploader **app & credentials** (token test passes — only the user-OAuth capture is missing), analytics (YouTube live stats), the xpst Hermes bot + night-watch reporting to the live Telegram chat.
- **Broken / blocking:** Instagram (stale 403 session; graph_api mode declared but never provisioned) · TikTok **uploader** (access_token empty; `TIKTOK_NOT_CONFIGURED`) · quota guardrail & reporting (config/quotas.json/runtime disagree; false `QUOTA_EXHAUSTED`; 248 MCP `guardrail_block`s) · `xpst auth status` false-positive for IG (file-exists check) · no xPST daemon/scheduler running (pid stale, :8080 dead) · `first_run_complete:false` + wizard never completed.
- **Cleanup:** pytest row in analytics.db, orphan test mp4, dead LinkedIn/product references in bot memory & SOUL & cron prompts, stale SOUL engine claim (Ornith-on-aragorn vs actual nous/stealth-ox-alpha), duplicate mission ticks & disabled legacy fleet crons, empty legacy dirs (`sessions/`, `thumbnails/`, `translations/`, `plugins/`), redundant `sources.*` and duplicate `circuit_breaker` config.
