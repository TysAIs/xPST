# YouTube Setup

> **Auth method:** OAuth 2.0 via Google Cloud Console (Desktop app credentials).
> **One-time setup:** ~5 minutes. After that, xPST auto-refreshes your token.

YouTube uses Google's official OAuth 2.0 flow. You create a small "OAuth client" in Google Cloud Console once, download a `client_secrets.json` file, and xPST handles the rest — opening a browser for you to authorize, then saving and auto-refreshing the token forever after.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Step 1 — Create a Google Cloud project](#step-1--create-a-google-cloud-project)
3. [Step 2 — Enable the YouTube Data API v3](#step-2--enable-the-youtube-data-api-v3)
4. [Step 3 — Configure the OAuth consent screen](#step-3--configure-the-oauth-consent-screen)
5. [Step 4 — Create OAuth Desktop credentials](#step-4--create-oauth-desktop-credentials)
6. [Step 5 — Download client_secrets.json](#step-5--download-client_secretsjson)
7. [Step 6 — Run `xpst connect youtube`](#step-6--run-xpst-connect-youtube)
8. [Step 7 — Publish the app to production (one-time fix for constant re-auth)](#step-7--publish-the-app-to-production-one-time-fix-for-constant-re-auth)
9. [Verify](#verify)
10. [Token storage & refresh](#token-storage--refresh)
11. [Troubleshooting](#troubleshooting)

---

## Prerequisites

- A **Google account** with a YouTube channel (the account you'll upload to).
  - Example: the central account `your-email@gmail.com`.
- Access to **<https://console.cloud.google.com>** (free).

No billing is required — the YouTube Data API v3 has a generous free daily quota (10,000 units; each upload costs ~1600 units, so you can post ~6 videos/day for free).

---

## Step 1 — Create a Google Cloud project

1. Open **<https://console.cloud.google.com/>** and sign in with your Google account.
2. Click the project picker (top bar) → **New Project**.
3. Name it (e.g. `xpst-youtube`) and click **Create**.
4. Switch to the new project using the project picker.

---

## Step 2 — Enable the YouTube Data API v3

1. In the left sidebar: **APIs & Services** → **Library**.
2. Search for **YouTube Data API v3**.
3. Open it and click **Enable**.

> If you skip this step, the OAuth flow will fail with an "API not enabled" error during the `xpst connect youtube` step.

---

## Step 3 — Configure the OAuth consent screen

1. Go to **APIs & Services** → **OAuth consent screen**.
2. Choose **External** (unless you have a Google Workspace org) and click **Create**.
3. Fill in:
   - **App name:** `xPST`
   - **User support email:** your email
   - **Developer contact email:** your email
4. Save and continue through the Scopes page (you can add scopes later — xPST requests them at runtime).
5. On the **Test users** page, click **Add users** and add the Google account you'll upload with (e.g. `your-email@gmail.com`). This is required while the app is in **Testing** mode.
6. Click **Save and Continue**.

> While the consent screen is in **Testing** mode, only the accounts you add as test users can authorize. **But Testing mode has a hidden cost: Google expires the refresh token ~7 days after consent** (access tokens last ~1 hour and xPST refreshes them silently — the 7-day refresh-token expiry is what makes xPST ask you to log in again every week). **Fix: Step 7 publishes the app to production, which makes the refresh token long-lived. Do Step 7.**

---

## Step 4 — Create OAuth Desktop credentials

1. Go to **APIs & Services** → **Credentials**.
2. Click **+ Create Credentials** → **OAuth 2.0 Client ID**.
3. **Application type:** select **Desktop app** (this is important — not "Web application").
4. **Name:** `xPST` (any name is fine).
5. Click **Create**.

> Desktop-app credentials are used because xPST runs locally. They use a loopback redirect (`http://localhost:8085`) rather than a public redirect URI, which keeps the flow entirely on your machine.

---

## Step 5 — Download client_secrets.json

1. After creating the credential, you'll see it listed under **OAuth 2.0 Client IDs**.
2. Click the **download** icon (⬇) on the right of the row — this downloads a JSON file named `client_secret_<id>.json`.
3. **Rename and save it to:**

   ```text
   ~/.xpst/credentials/youtube_client_secrets.json
   ```

   Create the directory first if it doesn't exist:

   ```bash
   mkdir -p ~/.xpst/credentials
   mv ~/Downloads/client_secret_*.json ~/.xpst/credentials/youtube_client_secrets.json
   chmod 600 ~/.xpst/credentials/youtube_client_secrets.json
   ```

> The `client_secrets.json` file identifies your OAuth app — it is **not** a credential by itself, but you should still keep it private. xPST locks it to `0600`.

---

## Step 6 — Run `xpst connect youtube`

```bash
xpst connect youtube
```

The wizard:

1. Checks for `~/.xpst/credentials/youtube_client_secrets.json`. If it's missing, it prints the quick setup steps above and offers to open the Cloud Console in your browser.
2. Opens your default browser to a Google sign-in / consent page.
3. You sign in with the Google account that owns the target channel and click **Allow**.
4. Google redirects to `http://localhost:8085/` on your machine, xPST captures the authorization code, and exchanges it for tokens.
5. The wizard prints:

   ```
   ✅ YouTube connected and token saved!
   ```

xPST requests these scopes (read+upload):

- `https://www.googleapis.com/auth/youtube.upload`
- `https://www.googleapis.com/auth/youtube.readonly`
- `https://www.googleapis.com/auth/youtube.force-ssl`

---

## Step 7 — Publish the app to production (one-time fix for constant re-auth)

**Do this. It is the single most important step for "set it and forget it" auth.**

While the consent screen sits in **Testing** mode, Google **expires the refresh
token ~7 days after you consent** (the access token's ~1 hour lifetime is
handled automatically by xPST — it's the refresh-token expiry that forces a
new browser login every week). Publishing the app converts the grant to a
**long-lived refresh token**: you authorize once, and xPST refreshes silently
forever after. This is a Google app-setting change — **no xPST code change is
required** (the auto-refresh at `src/xpst/utils/sessions.py` is already there).

1. Go to **<https://console.cloud.google.com/apis/credentials/oauthclient>** (or **APIs & Services** → **OAuth consent screen** in the left sidebar), with your xPST project selected in the top bar.
2. On the consent screen page, click **Publish App** (top of the page). It will change from "In testing" to "In production".
3. Google may ask you to add the three xPST scopes to the consent screen (**Scopes** tab → **Add or remove scopes**):
   - `https://www.googleapis.com/auth/youtube.upload`
   - `https://www.googleapis.com/auth/youtube.readonly`
   - `https://www.googleapis.com/auth/youtube.force-ssl`

   YouTube scopes are classified as **sensitive**, so the published app will show an **"unverified app" warning screen** (and a 100-user cap) until you complete Google's app verification. **That warning does not stop a refresh token from being issued** — just click **Advanced** → **Continue** on the warning when you next authorize. The 100-user cap is irrelevant for personal use (you are 1 user).
   - *Optional, later:* if you want the warning gone, click **Prepare for Verification** on the consent screen page and answer the short questionnaire (scope justification + demo link). For a personal tool this is never required for auth to work.
4. **Re-authorize once** so the new grant is issued under production mode:
   ```bash
   xpst connect youtube
   ```
   Sign in with the channel's Google account, click **Allow**. (If you see the "unverified app" warning: **Advanced** → **Continue** → **Allow**.)
5. Verify:
   ```bash
   xpst auth status --json    # youtube.authenticated should be true
   ```

> **How to tell it worked:** in `~/.xpst/credentials/youtube_token.json` the
> `expiry` field keeps advancing (access tokens are refreshed hourly), and you
> no longer get a "YouTube credentials expired. Run: xpst auth youtube" error
> ~7 days later. If you still do, the app is still in Testing mode — repeat
> step 2.

---

## Verify

```bash
# Test the connection (no upload)
xpst connect --test
#   ✅ YouTube: My Channel Name

# Or check full health
xpst health

# Post a test Short
xpst post -v ~/Videos/short.mp4 -c "First xPST Short 🎬" -p youtube
```

### What gets stored

| File | Contents | Permissions |
|------|----------|-------------|
| `~/.xpst/credentials/youtube_client_secrets.json` | Your OAuth client ID (from Google) | `0600` |
| `~/.xpst/credentials/youtube_token.json` | Your user access + refresh token (auto-refreshed) | `0600` |
| `~/.xpst/credentials/youtube_token.enc` | Encrypted copy of the token (Fernet) | `0600` |

---

## Token storage & refresh

The saved token includes a **refresh token**. xPST:

- Detects when the access token is expired (access tokens last ~1 hour).
- Uses the refresh token to get a new access token automatically.
- Re-saves the refreshed token to `youtube_token.json` (`0600`).

**The refresh token itself only lives forever if your app is in production mode (Step 7).** In Testing mode, Google expires the refresh token ~7 days after consent, and xPST *cannot* silently recover from that — it will tell you to run `xpst connect youtube` again. So: publish the app once, and "authorize once, refresh forever" is real.

You only need to re-run `xpst connect youtube` if:
- Your app was still in Testing mode and the refresh token expired (~7 days), **or**
- You revoke xPST's access in your Google account settings, or
- You delete `youtube_token.json` and `youtube_token.enc`, or
- You change the Google account / channel.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| **"YouTube credentials expired" every ~7 days** | Your OAuth app is still in **Testing** mode — Google expires refresh tokens there. **Step 7: Publish App to production**, then re-run `xpst connect youtube` once. After that the refresh token is long-lived. |
| `access_denied` during OAuth | Your Google account isn't on the consent screen's **Test users** list (Step 3, item 5). Add it and retry. |
| `redirect_uri_mismatch` | You created a **Web application** credential instead of **Desktop app**. Recreate it as Desktop app (Step 4). |
| `File not found` for client_secrets | The file isn't at `~/.xpst/credentials/youtube_client_secrets.json`. Check the exact name. |
| `API not enabled` error | You skipped Step 2 — enable YouTube Data API v3 in the Cloud Console. |
| `quotaExceeded` | You hit the daily YouTube API quota (10,000 units). It resets at midnight Pacific time. Uploads cost ~1600 units each. |
| Port `8085` already in use | Another process holds port 8085 (xPST's OAuth callback port). Close it or check `lsof -i :8085`. |
| Token works but upload fails with `400` | The video may exceed Shorts limits (>3 min, wrong aspect ratio) or be in an unsupported codec. xPST re-encodes to YouTube's spec with FFmpeg — ensure FFmpeg is installed (`ffmpeg -version`). |

See [troubleshooting.md](troubleshooting.md) for cross-platform issues and the credential reset procedure.
