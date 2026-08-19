# xPST Dashboard (Web API)

> The dashboard is a lightweight FastAPI/uvicorn server that exposes health,
> metrics, and state over HTTP. It is loopback-only by default and protected
> by Basic auth when dashboard credentials are configured. No external
> dependencies are required beyond the core install.

## Starting the Dashboard

```bash
# Default: http://127.0.0.1:8080
xpst dashboard

# Custom port / bind address
xpst dashboard --port 9000 --host 127.0.0.1
```

The server runs in the foreground; press `Ctrl+C` to stop it.

## Authentication

If `monitoring.dashboard_username` and `monitoring.dashboard_password_hash`
(bcrypt) are set in `~/.xpst/config.yaml`, all endpoints other than `/health`
and `/metrics` require HTTP Basic auth. `/health` and `/metrics` are open so
load balancers and uptime monitors can probe them.

Set a dashboard password:

```bash
xpst config set monitoring.dashboard_password mypassword
```

(The value is hashed with bcrypt and stored as `dashboard_password_hash`.)

## Endpoints

| Method & Path | Auth | Description |
|---------------|------|-------------|
| `GET /health` | — | Aggregated platform health check: one entry per configured platform with `ok`, `detail`, and latency. |
| `GET /metrics` | — | Prometheus text-format metrics (posting counters, upload durations, queue depths, health status). |
| `GET /state` | Basic | Current xPST state summary: version, per-platform status, queued and completed post counts, dead-letter queue size. |

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

## Messenger Webhook (opt-in)

When `accounts.messenger.enabled: true`, the dashboard additionally mounts
the Messenger webhook:

| Method & Path | Description |
|---------------|-------------|
| `GET /webhook/messenger` | Meta handshake: verifies `hub.verify_token`, echoes `hub.challenge`. |
| `POST /webhook/messenger` | Incoming message events. Verified with `X-Hub-Signature-256` (HMAC-SHA256 of the raw body using your App Secret + App Secret as key). |

Point your Facebook Page's webhook URL at
`https://<your-host>:<port>/webhook/messenger`. See
[setup-messenger.md](setup-messenger.md).

## Analytics Payload

The dashboard's analytics layer (`src/xpst/dashboard/analytics.py`) collects
per-post engagement from YouTube, Instagram, X, and TikTok APIs and caches
snapshots in `~/.xpst/analytics.db`. The desktop app (`xpst app`) and the MCP
server (`xpst_analytics`, `xpst_cross_post_analytics`) share this data.

## Related

- [TUTORIAL_APP.md](TUTORIAL_APP.md) — the native PySide6/QML desktop app
- [TUTORIAL_CLI.md](TUTORIAL_CLI.md) — the CLI surface
- [TUTORIAL_MCP.md](TUTORIAL_MCP.md) — the MCP surface
- [api.md](api.md) — Python API reference (engine, use-cases, providers)
