# Instagram Setup

> **Recommended method: Meta Graph API** — official, ToS-compliant, and ban-proof.
> xPST defaults to the Graph API. The unofficial `instagrapi` private-API client is available as an explicit fallback **only** and carries a real risk of getting your account banned.

Instagram is the platform most likely to ban automated accounts. xPST therefore uses Instagram's **official Graph API** by default, which is the same sanctioned path that scheduling tools (Buffer, Later, Meta Business Suite) use. This guide walks through the full setup.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Step 1 — Convert to a Creator or Business account](#step-1--convert-to-a-creator-or-business-account)
3. [Step 2 — Create a Meta Developer app](#step-2--create-a-meta-developer-app)
4. [Step 3 — Add the Instagram Graph API product](#step-3--add-the-instagram-graph-api-product)
5. [Step 4 — Get your IG user ID](#step-4--get-your-ig-user-id)
6. [Step 5 — Generate a long-lived access token](#step-5--generate-a-long-lived-access-token)
7. [Step 6 — Run `xpst connect instagram`](#step-6--run-xpst-connect-instagram)
8. [Verify](#verify)
9. [Token refresh & expiry](#token-refresh--expiry)
10. [Fallback: instagrapi session auth (NOT recommended)](#fallback-instagrapi-session-auth-not-recommended)
11. [Troubleshooting](#troubleshooting)

---

## Prerequisites

- An **Instagram Creator or Business account** (personal accounts cannot use the Graph API). Converting is free and reversible.
- A **Facebook Page** connected to that Instagram account.
- A **Meta Developer account** (free) and a Meta app in **Development** or **Live** mode.

> Example: a Creator account like `@your.account` works perfectly with this flow.

---

## Step 1 — Convert to a Creator or Business account

If your account is already a Creator/Business account, skip to [Step 2](#step-2--create-a-meta-developer-app).

1. Open the Instagram app → **Settings and privacy** → **Account type and tools**.
2. Tap **Switch to professional account**.
3. Choose **Creator** (recommended for individuals) or **Business**.
4. Complete the prompts (category, contact info). This is free and you keep all your followers/content.

A Creator/Business account can be linked to a Facebook Page, which is required for the Graph API.

---

## Step 2 — Create a Meta Developer app

1. Go to **<https://developers.facebook.com/apps>** and sign in with the Facebook account that owns your Page.
2. Click **Create App**.
3. App type: **Business** (gives access to Instagram Graph API).
4. Fill in **App name** (e.g. "xPST Poster"), contact email, and accept the terms.
5. You'll land on the app dashboard. Note your **App ID** and **App Secret** (Settings → Basic) — you won't usually need these directly, but keep them handy.

---

## Step 3 — Add the Instagram Graph API product

1. On your app dashboard, scroll to **Add Product** (or **Products** in the left sidebar).
2. Find **Instagram Graph API** and click **Set Up**.
3. In the product's **Basic Settings**, add the test Instagram account:
   - Go to **Roles** → **Instagram Testers** → **Add Instagram Testers**.
   - Enter your Instagram username (e.g. `your.account`) and submit.
4. **Accept the tester invite** from inside the Instagram app: Settings → **Apps and websites** → **Invitations from businesses** → accept.

While the app is in **Development** mode, only test users can be targeted. That's fine for personal use. To go Live, you'll need App Review for the relevant permissions — but Development mode is enough to post to your own account.

---

## Step 4 — Get your IG user ID

The IG user ID is a numeric identifier (different from your username).

**Using the Graph API Explorer (easiest):**

1. Go to **<https://developers.facebook.com/tools/explorer/>**.
2. Select your app in the top-right dropdown.
3. Click **Generate Access Token** and check these permissions:
   - `instagram_basic`
   - `instagram_content_publish`
   - `pages_show_list`
   - `pages_read_engagement`
4. In the query field, run a `GET` against `me/accounts` to list your Pages and find the Page connected to Instagram.
5. Then query `/{page-id}?fields=instagram_business_account` to get the **IG user ID** (the `instagram_business_account.id` field).

The value looks like `17841401234567890`. Save it — you'll paste it into xPST.

---

## Step 5 — Generate a long-lived access token

Short-lived tokens expire in ~1 hour. For an unattended cross-posting tool you want a **long-lived** token (60 days, refreshable).

1. In the Graph API Explorer, generate a **User token** with the scopes from Step 4.
2. Convert it to a long-lived token with this call (replace placeholders):

   ```
   GET https://graph.facebook.com/v21.0/oauth/access_token
       ?grant_type=fb_exchange_token
       &client_id={app_id}
       &client_secret={app_secret}
       &fb_exchange_token={short_lived_token}
   ```

3. The response contains your `access_token` (valid 60 days) and `expires_in`.

> **Scopes you need:** `instagram_basic` and `instagram_content_publish` are the minimum for posting Reels/media. `pages_show_list` and `pages_read_engagement` are needed to resolve the Page → IG account mapping. The xPST wizard verifies your token against `graph.facebook.com/v21.0/{ig_user_id}` and will tell you if a scope is missing.

---

## Step 6 — Run `xpst connect instagram`

```bash
xpst connect instagram
```

The wizard prints:

```
Recommended: Use the official Meta Graph API (ban-safe, ToS-compliant).
Not recommended: instagrapi session auth (risks account bans).

Use Graph API (recommended)? [Y/n]:
```

Press **Enter** (or `y`) to choose the Graph API. The wizard then asks:

1. **Instagram user ID (numbers):** — paste the IG user ID from Step 4.
2. **Long-lived access token:** — paste the token from Step 5 (input is hidden).

xPST immediately verifies the token against the Graph API:

```
Verifying token...
✅ Connected as @your.account (1234 followers, 56 posts)
✅ Instagram Graph API configured (ban-safe)!
```

If verification fails, the wizard prints the HTTP status and a hint about which scopes are missing.

---

## Verify

```bash
# Test the connection without uploading anything
xpst connect --test
#   ✅ Instagram: @your.account (Graph API)

# Or post a test Reel
xpst post -v ~/Videos/test.mp4 -c "xPST test reel 🎬" -p instagram
```

### What gets stored

- `config.yaml` → `instagram.auth_mode: "graph_api"`, plus the IG user ID and token (the token is also mirrored in the encrypted credential store).
- `~/.xpst/credentials/instagram_graph_token.enc` — encrypted token (Fernet).
- `~/.xpst/credentials/instagram_graph_user_id.enc` — encrypted user ID.

All files are `0600` (owner-only). See [getting-started.md](getting-started.md#where-your-credentials-live) for the full security model.

---

## Token refresh & expiry

Long-lived Instagram tokens expire after **60 days**. xPST will start receiving `190` / token-expired errors when this happens. To refresh:

1. Re-run `xpst connect instagram` and paste a freshly generated long-lived token, **or**
2. Exchange a still-valid (non-expired) token for a new 60-day one before it lapses:

   ```
   GET https://graph.facebook.com/v21.0/oauth/access_token
       ?grant_type=ig_exchange_token
       &client_secret={app_secret}
       &access_token={current_long_lived_token}
   ```

> Tip: set a calendar reminder ~5 days before expiry. You can check remaining validity with `xpst health`.

---

## Fallback: instagrapi session auth (NOT recommended)

> ⚠️ **Warning: `instagrapi` uses Instagram's private, undocumented API. Instagram actively detects and blocks automated clients, and this method can get your account shadow-banned or permanently disabled.** Use this only if you genuinely cannot set up the Graph API.

If you choose `n` at the "Use Graph API?" prompt, the wizard prints a ban warning and asks you to confirm. Only then does it proceed with `instagrapi`:

```bash
xpst connect instagram
# Use Graph API (recommended)? [Y/n]: n
# ⚠️  instagrapi uses Instagram's private API and can get your account BANNED.
# Continue with instagrapi anyway? [y/N]: y
```

The flow then:

1. Prompts for your Instagram **username** and **password** (hidden input).
2. Logs in via `instagrapi.Client.login()`.
3. Handles **2FA** — if Instagram requires a verification code, the wizard prompts for it (enter the code from Google Authenticator / Authy / SMS).
4. Handles **login challenges** (unusual-login security codes).
5. Saves a session file to `~/.xpst/credentials/instagram_session.json` (`0600`) so you don't re-enter credentials, and mirrors it into the encrypted store.

```bash
# If instagrapi isn't installed
pip install instagrapi
```

**Why this is risky:** private-API clients reverse-engineer Instagram's mobile endpoints. Even with stable device fingerprints (xPST persists a `device_id` per account to reduce friction), Instagram routinely flags and bans accounts that post via these endpoints. The Graph API exists *specifically* to avoid this. Prefer it whenever possible.

---

## Troubleshooting

| Symptom | Cause / Fix |
|---------|-------------|
| `Token verification failed: 400` | Token malformed or expired. Regenerate a long-lived token (Step 5). |
| `Token verification failed: 403` | Missing scopes. Ensure the token has `instagram_basic` + `instagram_content_publish`. |
| `(#10) Application does not have permission` | App not in scope, or test user invite not accepted (Step 3, item 4). |
| `No IG user ID` returned | Your Instagram account isn't linked to a Facebook Page, or isn't a Creator/Business account (Step 1). |
| Posts fail with media errors | Reels via Graph API require a **publicly accessible media URL** or a two-step container upload. For local files, xPST handles the container flow; ensure the video is ≤1080px height and H.264. |
| `403` / sudden `challenge_required` in instagrapi mode | Stop immediately. This is the ban signal. Switch to Graph API. |

See [troubleshooting.md](troubleshooting.md) for the full error catalogue, including how to reset Instagram credentials entirely.
