# Messenger Setup

> **Auth method:** Facebook Messenger Platform — a static Page Access Token (long-lived, no refresh) plus an App Secret for webhook signature verification.
> **Official API:** Yes — the sanctioned Messenger Platform Graph API. No ban risk.
> **Time:** ~10 minutes (one Meta app + token generation).
> **Opt-in:** Messenger is **disabled by default**. Nothing runs until you set `accounts.messenger.enabled: true` and provide a Page Access Token.

Messenger is xPST's **auto-reply / chatbot** option (ManyChat-lite). Instead of posting video, it replies to incoming messages on your Facebook Page using keyword rules you define. It reuses the same platform-adapter + SessionManager architecture as every other xPST destination, and it's driven by a webhook that Meta calls when someone messages your Page.

Because it's an official API, there's no ban risk and no session hacking. The auth model is simpler than OAuth: a **static Page Access Token** (page tokens don't expire while the page remains valid — no refresh flow) plus an **App Secret** used to sign outbound calls and verify inbound webhooks.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Messenger Platform limits](#messenger-platform-limits)
3. [Step 1 — Create a Meta app and enable Messenger](#step-1--create-a-meta-app-and-enable-messenger)
4. [Step 2 — Generate a Page Access Token](#step-2--generate-a-page-access-token)
5. [Step 3 — Get your App ID and App Secret](#step-3--get-your-app-id-and-app-secret)
6. [Step 4 — Configure xPST](#step-4--configure-xpst)
7. [Step 5 — Set up the webhook](#step-5--set-up-the-webhook)
8. [Auto-reply rules (ManyChat-lite)](#auto-reply-rules-manychat-lite)
9. [Verify](#verify)
10. [Troubleshooting](#troubleshooting)

---

## Prerequisites

- A **Facebook Page** you administer (Messenger is page-scoped).
- A **Meta Developer account** (free) at <https://developers.facebook.com>.
- A way to expose the xPST dashboard to the internet (Meta must reach your webhook) — a reverse proxy, ngrok, or Cloudflare Tunnel to the dashboard port (default `8080`).

---

## Messenger Platform limits

| Limit | Value |
|-------|-------|
| Max text length | 640 characters |
| Messaging window | 24h standard (free-form replies); 7-day eligibility window at limited frequency |
| Token lifetime | Long-lived (no refresh needed while the page remains valid) |
| Recipient ID | Page-scoped PSID (per-page, not a global user ID) |
| Re-engagement | Requires message tags / templates (out of scope for v1 auto-reply) |

Docs: <https://developers.facebook.com/docs/messenger-platform>

---

## Step 1 — Create a Meta app and enable Messenger

1. Go to **<https://developers.facebook.com/apps>** and sign in.
2. **Create App** → type **Business** → name it (e.g. `xPST Messenger`).
3. On the dashboard, find **Messenger** under **Add Product** (or Products in the sidebar) and click **Set Up**.
4. In Messenger → **Settings**, connect your Facebook Page (under "Access Tokens" → "Add or Remove Pages").

In Development mode, only admins/testers of the app can message the page — which is fine for testing your own auto-replies.

---

## Step 2 — Generate a Page Access Token

1. In Messenger → **Settings** → **Access Tokens**, select your Page and click **Generate Token**.
2. Copy the token. It starts with `EAAG…` and is long-lived once generated for a page you administer.

> You can also derive a page token from a user token via the Graph API:
> `GET /v22.0/me/accounts?access_token={USER_TOKEN}` → returns `id`, `access_token`, and `tasks` (must include `MESSAGING`).

---

## Step 3 — Get your App ID and App Secret

1. Go to **App → Settings → Basic**.
2. Copy the **App ID** (numeric) and **App Secret**.

The App Secret is used for two things:
- **`appsecret_proof`** — an HMAC-SHA256 of the page token, appended to every outbound call to prevent token theft/replay.
- **`X-Hub-Signature-256`** — the HMAC signature Meta attaches to webhook POSTs; xPST verifies it before trusting any inbound body.

---

## Step 4 — Configure xPST

The fastest path is the guided wizard:

```bash
xpst auth messenger
#   Page Access Token (starts EAAG...): <paste>
#   App Secret (optional, for webhook signatures): <paste>
#   Webhook verify token (any string, optional): <pick one>
#   Page ID (numeric, optional): <paste>
#   App ID (numeric, optional): <paste>
```

This enables the account and stores the token + secret **encrypted** in the credential store. Alternatively, edit `~/.xpst/config.yaml`:

```yaml
accounts:
  messenger:
    enabled: true
    page_id: "1234567890"
    app_id: "1234567890"
    app_secret: "YOUR_APP_SECRET"
    verify_token: "some-random-string-you-chose"
    webhook_path: "/webhook/messenger"
    auto_reply: true
    reply_rules:
      "pricing": "Our pricing starts at $X — want a link?"
      "hours": "We're open 9–5 Mon–Fri."
      "*": "Thanks for reaching out! A human will reply soon."
```

Then validate:

```bash
xpst config validate
```

### What gets stored

| Location | Contents |
|----------|----------|
| `~/.xpst/credentials/` | Encrypted `messenger_page_token` + `messenger_app_secret` (primary) |
| `~/.xpst/config.yaml` → `accounts.messenger` | `enabled`, `page_id`, `app_id`, `verify_token`, `webhook_path`, `auto_reply`, `reply_rules` (token/secret optional fallback) |

> **Secret handling:** the token and app secret live encrypted in the credential store. You can also set them via environment variables: `XPST_MESSENGER_PAGE_ACCESS_TOKEN`, `XPST_MESSENGER_APP_SECRET`, `XPST_MESSENGER_VERIFY_TOKEN`, `XPST_MESSENGER_PAGE_ID`, `XPST_MESSENGER_APP_ID`, `XPST_MESSENGER_AUTO_REPLY`, `XPST_MESSENGER_REPLY_RULES` (JSON).

---

## Step 5 — Set up the webhook

xPST exposes two webhook endpoints on the dashboard (default path `/webhook/messenger`, configurable via `webhook_path`):

- **`GET`** — Meta's subscription handshake. xPST verifies `hub.verify_token` and echoes `hub.challenge`.
- **`POST`** — inbound message events. xPST verifies `X-Hub-Signature-256` (HMAC-SHA256 with your App Secret) before dispatching, then replies per your rules.

1. Start the dashboard: `xpst dashboard` (binds `127.0.0.1:8080` by default).
2. Expose it to the internet (reverse proxy / ngrok / Cloudflare Tunnel) so Meta can reach `https://your-host/webhook/messenger`.
3. In Meta → Messenger → **Settings** → **Webhooks**, click **Add Callback URL**:
   - **Callback URL:** `https://your-host/webhook/messenger`
   - **Verify Token:** the `verify_token` you chose in Step 4.
4. Subscribe to the `messages` field (and `message_deliveries` if you want delivery receipts).

> xPST responds `200` immediately to every webhook so Meta doesn't retry; outbound replies are sent asynchronously.

---

## Auto-reply rules (ManyChat-lite)

Auto-reply is controlled by two config fields:

- **`auto_reply`** — master switch (default `false`).
- **`reply_rules`** — a keyword → reply map. Matching is case-insensitive substring; the **longest** matching keyword wins. The special key `*` is the catch-all default.

```yaml
accounts:
  messenger:
    auto_reply: true
    reply_rules:
      "pricing": "Our pricing starts at $X — want a link?"
      "pricing pro": "Pro plan details here."
      "*": "Thanks for writing! A human will reply soon."
```

- A message "tell me about pricing" → matches `pricing`.
- "pricing pro please" → matches `pricing pro` (longest wins).
- Anything else → the `*` catch-all.

You can also manage rules over MCP with `messenger_set_rules`, or send a one-off reply with `messenger_send`.

---

## Verify

```bash
# Auth status (shows a Messenger row)
xpst auth status

# Full health check (GET /v22.0/me with the page token)
xpst health

# Send a test reply to a PSID (from a webhook event)
xpst messenger send <PSID> "Hello from xPST!"
```

> The `xpst messenger send` command is available once the adapter is registered; the primary programmatic surface is the MCP `messenger_send` tool.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `MESSENGER_NOT_CONFIGURED` | Set `accounts.messenger.enabled: true` and a Page Access Token (Step 4), or run `xpst auth messenger`. |
| `MESSENGER_AUTH_EXPIRED` / `190` | The page token is invalid or the page was removed from the app. Re-generate it (Step 2). |
| Webhook GET returns 403 | `hub.verify_token` doesn't match `accounts.messenger.verify_token`. |
| Webhook POST returns 403 "Invalid signature" | `app_secret` mismatch — make sure the App Secret in xPST matches the app that owns the page token. |
| `MESSENGER_RATE_LIMITED` | You hit the Messenger API rate limit; back off and retry. |
| `MESSENGER_NO_RECIPIENT` | Direct `upload()` needs `accounts.messenger.page_id`; webhook auto-reply always has a sender PSID. |
| Replies not firing | `auto_reply` is `false`, or no `reply_rules` match and there's no `*` catch-all. |

See [troubleshooting.md](troubleshooting.md) for the credential-reset procedure and cross-platform token issues.
