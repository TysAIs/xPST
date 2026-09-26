# xPST Dashboard (Web API)

> The dashboard is a lightweight FastAPI/uvicorn server that exposes health,
> metrics, and state over HTTP. It is loopback-only by default. Read-only
> routes are protected by Basic auth when dashboard credentials are
> configured; mutating routes always require the xPST API token (see
> [Authentication](#authentication)). No external dependencies are required
> beyond the core install.

## Starting the Dashboard

```bash
# Default: http://127.0.0.1:8080
xpst dashboard

# Custom port / bind address
xpst dashboard --port 9000 --host 127.0.0.1
```

The server runs in the foreground; press `Ctrl+C` to stop it.

## Authentication

There are two layers, and they behave differently on purpose.

**Read-only routes** (`GET /health`, `/metrics`, `/state`, `/api/summary`,
`/api/videos`, `/api/onboarding`, …) keep the historical behaviour: if
`monitoring.dashboard_username` and `monitoring.dashboard_password_hash`
(bcrypt) are set in `~/.xpst/config.yaml`, everything except `/health`,
`/metrics`, `/bio` and `/oauth/callback` requires HTTP Basic auth; otherwise
they are open on loopback so the web UI is never locked out.

Set a dashboard password:

```bash
xpst config set monitoring.dashboard_password mypassword
```

(The value is hashed with bcrypt and stored as `dashboard_password_hash`.)

**Mutating routes** (`POST /api/post`, `POST /api/connect/{platform}`,
`POST /api/onboarding`, `POST /api/onboarding/complete`, `POST /api/preflight`,
`POST /bio/edit`) always require a credential, whether or not a dashboard
password is configured. Loopback is not an authorisation boundary: any process
on the machine — and any page open in a browser — can reach
`127.0.0.1:<port>`, so an unauthenticated write is refused with `401`.

### The API token

The token is generated on first run and stored in the encrypted credential
store under `~/.xpst/credentials/` (the same place as the platform OAuth
tokens; never in `config.yaml`, and no default value ships with the project).
Print it with:

```bash
xpst auth api-token          # print the token
xpst auth api-token --json   # {"config_dir": ..., "api_token": ...}
xpst auth api-token --rotate # replace it (the old token stops working)
```

Send it as either header:

```bash
TOKEN=$(xpst auth api-token --json | python -c 'import json,sys;print(json.load(sys.stdin)["api_token"])')

curl -sS -X POST http://127.0.0.1:8080/api/post \
  -H "X-API-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"media_paths": ["/path/clip.mp4"], "caption": "hi", "platforms": ["youtube"]}'

curl -sS -X POST http://127.0.0.1:8080/api/onboarding \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}'
```

Two further tokens are accepted but never written to disk:

| Variable | Who sets it | Purpose |
|----------|-------------|---------|
| `XPST_API_TOKEN` | you / your scripts / CI | operator override for CLI, MCP bridges and agents; no need to read the store |
| `XPST_UI_TOKEN` | the desktop shell (`xPST.app`) | per-launch token it hands its own webview; regenerated on every boot |

### How each client authenticates

| Client | Mechanism |
|--------|-----------|
| Desktop app (Tauri/Svelte shell) | The shell mints a per-launch token, passes it to the engine as `XPST_UI_TOKEN`, and opens the webview at `http://127.0.0.1:<port>/#xpst_token=<token>`. The UI reads the fragment, sends `X-API-Token` on writes, and strips the fragment from the URL immediately. |
| `xpst ui` (local browser UI) | Same fragment hand-off, with a token minted for that run and opened in your default browser. Nothing is ever embedded in the served HTML. |
| CLI / scripts / agents | `xpst auth api-token` (or `XPST_API_TOKEN`), sent as `Authorization: Bearer` or `X-API-Token`. |
| MCP server | Not affected: MCP tools call the engine in-process and never traverse HTTP. |
| Link-in-bio editor | Basic auth when configured; otherwise `?token=<api-token>` on `/bio/edit` (a plain HTML form cannot send a header). `xpst bio` prints that editor URL for you. |

Read-only calls need no token in any of these clients.

### Public-by-design routes

`POST /oauth/callback` (OAuth deep-link redirect from the browser) and the
optional Messenger webhook (`/webhook/*`) stay reachable without a token: a
browser redirect cannot attach a header, and Meta calls the webhook directly.
Both validate their own payloads (body cap + scheme allow-list; hub verify
token + HMAC signature).

## Endpoints

| Method & Path | Auth | Description |
|---------------|------|-------------|
| `GET /health` | — | Aggregated platform health check: one entry per configured platform with `ok`, `detail`, and latency. |
| `GET /metrics` | — | Prometheus text-format metrics (posting counters, upload durations, queue depths, health status). |
| `GET /state` | Basic | Current xPST state summary: version, per-platform status, queued and completed post counts, dead-letter queue size. |
| `GET /api/*` | Basic when configured | Web-UI JSON API (summary, videos, onboarding state, media, library, activity, schedules, providers, settings, capabilities). Read-only. |
| `POST /api/post` | API token | Plan (`dry_run: true`) or run a post through the real engine path. Accepts `content_type` (`video`/`image`/`carousel`/`text`/`thread`); a type the destination cannot publish is refused with `409` before anything is uploaded. |
| `POST /api/connect/{platform}` | API token | Inspect / enable / verify one destination platform. |
| `POST /api/onboarding`, `POST /api/onboarding/complete` | API token | Persist the first-run choices and the "wizard finished" flag. |
| `POST /api/preflight` | API token | Local, no-network post preflight. Accepts `content_type` and reports the same `content` verdict the CLI and MCP return. |
| `GET /api/capabilities` | Basic when configured | The canonical capability contract (one source: `xpst.content`): per destination, what it declares vs what it can publish, plus the publishing route per content type. |
| `GET /bio` | — | Public link-in-bio page (meant to be shared). |
| `GET/POST /bio/edit` | Basic or `?token=` | Admin editor for the link-in-bio page. |

### One readiness verdict (`/api/health-status` and `/api/onboarding`)

The Home Readiness panel and the onboarding wizard must not answer the same
question differently, so both endpoints embed the SAME `readiness` document,
built by `xpst.readiness.build_readiness_report(config, live_status=<canonical
probe>)`:

- `roles` — one entry per ENABLED provider role (`platform`, `role`,
  `role_label`, `state`, `ready`, `session_valid`, `live_checked`, `error`).
  A row is identified by `(platform, role)`; a screen that prints only
  `platform` renders three indistinguishable rows for a three-role platform.
- `blockers` — the `roles` that are not ready (what a panel should list).
- `verdict` — `{status, label, detail}`: the pill text and the copy. The UI
  renders these strings; it never re-derives a verdict of its own.
- `pending` — `true` when no live probe has answered yet. `ready` is then
  `false` and `roles` is empty: a stored credential is never published as a
  ready destination.

`live_status` is the canonical probe output (the same mapping
`/api/health-status.auth`, `xpst auth status` and `xpst doctor` render). With
no live answer the states are the config-only ones and `live_checked` is
`null`/`false` on every role, so "a credential file exists" never reads as
"verified". `GET /api/health-status.status`/`platforms` is the *recorded*
engine-health block only — it is not flipped by the live probe, so its pill
always matches its own rows.

### `/health` example

```json
{
  "status": "healthy",
  "platforms": {
    "youtube": {"status": "ok", "detail": "connected"},
    "instagram": {"status": "ok", "detail": "session valid"},
    "x": {"status": "error", "detail": "cookies expired"}
  },
  "total_processed": 42
}
```

`status` is `"healthy"` when every platform's `status` is `"ok"`, otherwise
`"degraded"`. On an internal failure it returns `{"status": "error", "detail": "..."}`.

### `/state` example (Basic auth required)

`/state` returns the aggregate summary computed from `state.json`:

```json
{
  "total_posts": 42,
  "total_processed": 40,
  "platform_counts": {"youtube": 12, "instagram": 15, "x": 9, "tiktok": 4},
  "platform_health": {"youtube": "ok", "instagram": "ok", "x": "needs_reauth"},
  "last_check": "2026-08-18T15:04:11",
  "posts_this_week": 6,
  "best_platform": "youtube",
  "total_platform_posts": 40
}
```

### `/metrics` (Prometheus)

```
xpst_posts_total{platform="youtube"} 42
xpst_upload_seconds_bucket{le="30.0"} 1
xpst_health_up{platform="youtube"} 1
```

## Durable drafts and the stale-plan gate

The compose screen keeps its work in the engine, not in the page: a draft survives
navigating away, closing the window, or restarting xPST. Drafts are local-only
(`<config_dir>/drafts.json`, mode `0600`) and hold content only — media paths, the caption,
destination names, and the preflight plan. No credential material is ever copied into one.

| Method & Path | Auth | Description |
|---------------|------|-------------|
| `GET /api/drafts` | Basic when configured | Stored drafts, newest first, each revalidated against this machine right now. |
| `POST /api/drafts` | API token | Create or update a draft (`draft_id`, `media_paths`, `caption`, `platforms`). Returns the stored draft and its verdict. An unknown `draft_id` creates a fresh draft and reports `recreated: true` rather than losing what was typed. |
| `GET /api/drafts/{draft_id}` | Basic when configured | One draft plus a fresh verdict — the revalidation performed when a screen resumes. |
| `DELETE /api/drafts/{draft_id}` | API token | Discard a draft. |

A draft records **what it was validated against**: the local facts the verdict depended on
(media existence/size/mtime, per-destination enabled flag and local auth readiness) plus a
fingerprint over them. Revalidating recomputes those facts and diffs them, so `stale` always
arrives with machine-readable `reasons` (`MEDIA_MISSING`, `MEDIA_CHANGED`, `DESTINATION_NOT_READY`,
`DESTINATION_DISABLED`, `AUTH_CHANGED`, `CONTENT_CHANGED`, …).

A plan whose destination, auth, media, or content state changed is **refused on the post path**:
`POST /api/preflight` and `POST /api/post` with a `dry_run` record the plan, and a later
`POST /api/post` bound to that `draft_id` returns `409` with `stale: true` and `stale_reasons`
unless the caller passes `confirm_stale: true`. Re-confirmation re-stamps the draft and is
recorded (`reconfirmations`) so the acceptance is auditable. A post that actually runs closes the
draft out with `status: "posted"` and the engine's `video_id`.

## Messenger Webhook (opt-in)

When `accounts.messenger.enabled: true`, the dashboard additionally mounts
the Messenger webhook:

| Method & Path | Description |
|---------------|-------------|
| `GET /webhook/messenger` | Meta handshake: verifies `hub.verify_token`, echoes `hub.challenge`. Fails closed (403) when no `verify_token` is configured — an unconfigured install cannot be subscribed by anyone. |
| `POST /webhook/messenger` | Incoming message events. **Every** request must carry `X-Hub-Signature-256` (HMAC-SHA256 of the raw body using your App Secret): Meta always signs its deliveries, so an unsigned or unverifiable request is refused with `403` rather than accepted unverified. |

Point your Facebook Page's webhook URL at
`https://<your-host>:<port>/webhook/messenger`. See
[setup-messenger.md](setup-messenger.md).

## Analytics Payload

The dashboard's analytics layer (`src/xpst/dashboard/analytics.py`) collects
per-post engagement from YouTube, Instagram, X, and TikTok (TikTok via the source-side downloader metadata path) and caches
snapshots in `~/.xpst/analytics.db`. The desktop app (`xpst app`) and the MCP
server (`xpst_analytics`, `xpst_cross_post_analytics`) share this data.

## Related

- [TUTORIAL_APP.md](TUTORIAL_APP.md) — the desktop app (Tauri shell)
- [TUTORIAL_CLI.md](TUTORIAL_CLI.md) — the CLI surface
- [TUTORIAL_MCP.md](TUTORIAL_MCP.md) — the MCP surface
- [api.md](api.md) — Python API reference (engine, use-cases, providers)
