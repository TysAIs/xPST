# Facebook Page Setup

> **Auth method:** Facebook Login for Business (BYO Meta app) → a **Page** access token.
> **Scope:** Page-scoped publishing only. Facebook's Graph API publishes as a **Page**;
> personal profiles have no publishing API at all, so xPST never posts as a profile.
> **Current status:** implemented and unit-tested; **not authenticated** in the current
> live environment (no Meta app is configured here). The steps below are the real
> connection path, not a claim that a Page is already connected.

xPST ships a Facebook destination in `src/xpst/platforms/facebook.py`. Connecting is
`xpst auth facebook`; the flow discovers the Pages your user token administers via
`GET /me/accounts`, verifies the chosen Page's token, and stores it encrypted. Feed
video is what this wave declares; Reels / photo / text are the follow-up that extends
the same connector.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Page API limits](#page-api-limits)
3. [Step 1 — Create a Meta app with Facebook Login for Business](#step-1--create-a-meta-app-with-facebook-login-for-business)
4. [Step 2 — Generate a user access token](#step-2--generate-a-user-access-token)
5. [Step 3 — Run `xpst auth facebook`](#step-3--run-xpst-auth-facebook)
6. [Step 4 — Verify](#step-4--verify)
7. [Agent / non-interactive use](#agent--non-interactive-use)
8. [What gets stored](#what-gets-stored)
9. [Token lifetime](#token-lifetime)
10. [Troubleshooting](#troubleshooting)

---

## Prerequisites

- A **Facebook Page** you administer (Admin or Editor role). A personal profile is
  not sufficient — there is no publishing API for profiles.
- A **Meta Developer account** (free) at <https://developers.facebook.com>.
- Your own Meta app. xPST is BYO-app: it does not ship a shared app, so your Pages
  are never shared with anyone else.

---

## Page API limits

| Limit | Value |
|-------|-------|
| Max caption length | 63,206 characters |
| Video upload | `POST /{page_id}/videos` (resumable/chunked upload for large files) |
| Page access token | Derived from a user token; inherits the user token's lifetime |
| Daily posting | Enforced by xPST's own quota (`rate_limits.facebook`, default 5/day) |

Docs: <https://developers.facebook.com/docs/pages-api> ·
<https://developers.facebook.com/docs/pages/access-tokens>

---

## Step 1 — Create a Meta app with Facebook Login for Business

1. Go to **<https://developers.facebook.com/apps>** and sign in.
2. **Create App** → use case **Other** → type **Business** → name it (e.g. `xPST Pages`).
3. In **Add Product**, add **Facebook Login for Business** → **Set Up**.
4. Under **Facebook Login for Business → Settings**, add the redirect URI you will use
   (`https://localhost/` is the xPST default) if you plan to use the dialog/code path.
5. Note the **App ID** and **App Secret** (Settings → Basic). Keep the secret out of the
   repository — xPST stores it encrypted.

**Standard Access is enough** for your own Pages; no App Review is required to publish
to Pages you administer.

---

## Step 2 — Generate a user access token

The shortest path (no redirect handling) is the Graph API Explorer:

1. Open **<https://developers.facebook.com/tools/explorer/>**.
2. Select your app (top right), then **Get User Access Token**.
3. Grant these permissions:
   - `pages_show_list` — required, to list the Pages you administer
   - `pages_read_engagement`
   - `pages_manage_posts` — required, to publish
4. Generate the token and copy it.

Alternatively, run the **Facebook Login for Business** dialog and paste the redirect URL
you land on: xPST extracts the `code` parameter and exchanges it for a token (this needs
the App ID, App Secret and the exact registered redirect URI).

---

## Step 3 — Run `xpst auth facebook`

```bash
xpst auth facebook
```

The flow asks for:

| Prompt | Notes |
|--------|-------|
| Meta App ID | Optional, but required to exchange the token for a long-lived one |
| Meta App Secret | Optional, hidden input |
| Login configuration ID | Optional; only for apps with a saved Login configuration |
| User Access Token **or** redirect URL | Paste either one |

Then it prints the Pages you administer, picks one (auto-selects when you only have one,
otherwise asks for the Page ID), verifies the Page token against `GET /{page_id}`, and
stores everything encrypted.

---

## Step 4 — Verify

```bash
xpst auth status --json          # facebook: roles.video_destination
xpst health                      # "Facebook Page" row + Page id
```

Expected on a successful connect: `facebook.roles.video_destination.state == "ready"`,
`authenticated: true`, and the health row naming your Page. If the Page is missing or the
token is wrong, the command fails with an explicit code instead of pretending to work.

---

## Agent / non-interactive use

`xpst auth facebook` never prompts when stdin is not a TTY. Set the inputs as environment
variables and read the structured report from stdout:

```bash
export XPST_FACEBOOK_USER_TOKEN=...      # long-lived user token (or use the code path)
export XPST_FACEBOOK_APP_ID=...          # optional: enables the long-lived exchange
export XPST_FACEBOOK_APP_SECRET=...      # optional
export XPST_FACEBOOK_PAGE_ID=...         # optional: choose the Page (required if you admin several)
export XPST_FACEBOOK_OAUTH_CODE=...      # optional: authorization code / redirect URL
export XPST_FACEBOOK_LOGIN_CONFIG_ID=... # optional
export XPST_FACEBOOK_REDIRECT_URI=...    # optional, default https://localhost/

xpst auth facebook --json
```

The JSON report contains `success`, `page`, `pages` (secret-free listing), `user`,
`auth_mode`, `error`, `hint` and a per-step `steps` trace — and never a token.

---

## What gets stored

| Location | Contents |
|----------|----------|
| `~/.xpst/credentials/` (encrypted CredentialStore) | `facebook_page_id`, `facebook_page_token`, `facebook_user_token`, and `facebook_app_id` / `facebook_app_secret` when supplied |
| `~/.xpst/config.yaml` → `accounts.facebook` | `enabled`, `page_id`, `page_name`, plus write-through copies of the above for convenience |

Credentials are encrypted at rest with the same Fernet file storage (0600) used for every
other platform; nothing is written in plaintext. `xpst disconnect facebook` removes the
stored credentials and disables the account.

---

## Token lifetime

A **Page access token** inherits the lifetime of the user token it was derived from. xPST
exchanges the user token for a **long-lived** (~60 day) token when the App ID and App
Secret are supplied, so the stored Page token is long-lived too. There is no refresh
endpoint for Page tokens: when it eventually expires, re-run `xpst auth facebook`. The
`xpst auth status` badge says `needs_reauth` rather than showing a stale green.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `FACEBOOK_PAGE_REQUIRED: this token administers no Facebook Pages` | Your user token has no Pages, or `pages_show_list` was not granted. Create a Page you administer, or re-generate the token with the permission. Personal profiles cannot publish. |
| `FACEBOOK_PAGE_NOT_FOUND: no administered Page with id …` | The Page ID you passed is not among the Pages that token administers; the error lists the ones that are. |
| `FACEBOOK_USER_TOKEN_INVALID: … (code 190)` | The user token expired or was revoked. Generate a fresh one (Step 2). |
| `FACEBOOK_PAGE_TOKEN_INVALID` / `(#200) Permissions error` | The user must hold an Admin/Editor role on that Page, and the app needs `pages_manage_posts`. |
| `FACEBOOK_PAGE_MISMATCH` | The stored token belongs to a different Page — re-run `xpst auth facebook` and pick the right Page. |
| Publishing succeeds but the post is not visible | Facebook Pages can require review of newly created Pages; check the Page directly, and confirm the Page (not a profile) is the target in `xpst health`. |

See [troubleshooting.md](troubleshooting.md) for credential resets and cross-platform
token issues.
