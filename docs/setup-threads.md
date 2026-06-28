# Threads Setup

> **Auth method:** Meta Threads API — a long-lived access token (60 days, refreshable) tied to your Threads user ID.
> **Official API:** Yes — the same sanctioned path as scheduling tools. No ban risk.
> **Time:** ~10 minutes (one Meta OAuth flow).

Threads (the text-and-media network by Meta) has an official **Threads API** that xPST uses to post videos and text. It uses Meta's container-publish model: create a media container, (optionally upload), then publish. Because it's an official API, there's no ban risk and no unofficial session hacking.

Unlike YouTube/Instagram/X which are wired into the interactive `xpst connect` wizard, Threads is configured via your `~/.xpst/config.yaml` file with a long-lived token and your Threads user ID. This guide shows both ends: getting the token from Meta, and pointing xPST at it.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Threads API limits](#threads-api-limits)
3. [Step 1 — Create a Meta app and enable Threads](#step-1--create-a-meta-app-and-enable-threads)
4. [Step 2 — Get your Threads user ID](#step-2--get-your-threads-user-id)
5. [Step 3 — Generate a long-lived Threads access token](#step-3--generate-a-long-lived-threads-access-token)
6. [Step 4 — Configure xPST](#step-4--configure-xpst)
7. [Verify](#verify)
8. [Token refresh & expiry](#token-refresh--expiry)
9. [Troubleshooting](#troubleshooting)

---

## Prerequisites

- A **Threads account** (via the Threads mobile app; sign in with your Instagram credentials).
- A **Meta Developer account** (free) at <https://developers.facebook.com>.
- The Threads account linked to a public/accessible profile.

---

## Threads API limits

| Limit | Value |
|-------|-------|
| Max posts per 24h | 250 |
| Max video duration | 300 seconds (5 min) |
| Max video size | 1 GB |
| Max text/caption length | 500 characters |
| Token lifetime | 60 days (refreshable) |
| Media upload | URL-based (media must be reachable via a public URL) or a two-step container upload |

> The 250-post daily limit is enforced server-side by Meta. xPST tracks quota locally and will warn as you approach it.

Docs: <https://developers.facebook.com/docs/threads>

---

## Step 1 — Create a Meta app and enable Threads

1. Go to **<https://developers.facebook.com/apps>** and sign in.
2. **Create App** → type **Business** → name it (e.g. `xPST Threads`).
3. On the dashboard, find **Threads API** under **Add Product** (or Products in the sidebar) and click **Set Up**.
4. In the Threads product settings, add your Threads account as a **tester** (Roles → Threads Testers) and accept the invite from the Threads app (Settings → Accounts → Invitations).

In Development mode, only test accounts can be used — which is fine for posting to your own Threads.

---

## Step 2 — Get your Threads user ID

The Threads user ID is numeric and distinct from your username.

**Easiest route — Threads API Explorer:**

1. Go to **<https://developers.facebook.com/tools/threads_api_explorer/>** (or the Graph API Explorer with the Threads product selected).
2. Select your app.
3. Generate a token with the `threads_basic` scope.
4. Run a `GET` against `me` or `me/threads_profile` — the response includes your Threads user `id` and `username`.

The ID looks like `9000123456789012`. Save it.

---

## Step 3 — Generate a long-lived Threads access token

1. In the Explorer, generate a user token with these scopes:
   - `threads_basic`
   - `threads_content_publish` (required to publish posts)
2. Exchange the short-lived token for a **long-lived** Threads token (valid 60 days):

   ```
   GET https://graph.threads.net/v1.0/refresh_access_token
       ?grant_type=th_refresh_token
       &access_token={short_lived_token}
   ```

   > The Threads API uses the `graph.threads.net` host (note: distinct from `graph.facebook.com`). The long-lived token response includes an `expires_in` (~5,154,000 seconds = 60 days).

3. Copy the resulting `access_token`.

---

## Step 4 — Configure xPST

Edit `~/.xpst/config.yaml` and fill in the Threads section:

```yaml
accounts:
  threads:
    enabled: true
    graph_access_token: "YOUR_LONG_LIVED_THREADS_TOKEN"
    threads_user_id: "9000123456789012"
```

Then reload/validate:

```bash
xpst config validate
```

xPST reads `threads.graph_access_token` and `threads.threads_user_id` at runtime. The Threads uploader lazily caches the token, verifies connectivity on the first health check, and refreshes the token via the Threads refresh endpoint when needed.

> **Secret handling:** Although you can put the token directly in `config.yaml`, the recommended pattern is to keep `config.yaml` paths/flags only and store the token in the encrypted credential store. You can also set it via environment variable: `XPST_ACCOUNTS_THREADS__GRAPH_ACCESS_TOKEN`. (xPST will use the value from config if present.)

---

## Verify

```bash
# Test all connections, including Threads
xpst connect --test
#   ✅ Threads: connected (Graph API)

# Full health check
xpst health

# Post a test clip (note: Threads media uploads are URL-based;
# for local files xPST handles the container flow)
xpst post -v ~/Videos/clip.mp4 -c "Cross-posting to Threads 🧵" -p threads
```

### What gets stored

| Location | Contents |
|----------|----------|
| `~/.xpst/config.yaml` → `accounts.threads` | `enabled`, `threads_user_id`, (token if you chose config storage) |
| `~/.xpst/credentials/` | Encrypted copies when mirrored via the credential store |

---

## Token refresh & expiry

The long-lived Threads token expires after **60 days**. xPST can refresh it automatically using the Threads refresh endpoint *as long as the token is still valid* (you must refresh **before** it fully expires — once expired, you must re-do the OAuth flow).

- xPST's Threads uploader calls `refresh_access_token` with `grant_type=th_refresh_token` to extend the token.
- If refresh fails (token already lapsed), re-do Step 3 and update `config.yaml`.

Check remaining validity:

```bash
xpst health
```

> Set a calendar reminder ~5 days before the 60-day mark. If `xpst health` shows Threads failing with `190` (token expired), run Steps 3–4 again.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `THREADS_NOT_CONFIGURED: Set graph_access_token and threads_user_id` | Edit `~/.xpst/config.yaml` (Step 4) and run `xpst config validate`. |
| `190` / token-expired errors | Re-generate a long-lived token (Step 3) and update config. |
| `(#10) Application does not have permission` | Threads tester invite not accepted (Step 1, item 4), or `threads_content_publish` scope missing on the token. |
| Upload fails with "media URL not accessible" | Threads requires media reachable via a **public URL**. For local files, xPST uses the two-step container upload; ensure FFmpeg is installed so the file is re-encoded to spec (H.264, ≤300s, ≤1GB). |
| `Rate limit` / 250 posts exceeded | You've hit the 24-hour cap. It resets server-side; wait or reduce post frequency. |
| `400` invalid container | Caption > 500 chars, or video > 300s / > 1GB. Trim or re-encode. |

See [troubleshooting.md](troubleshooting.md) for the credential-reset procedure and cross-platform token issues.
