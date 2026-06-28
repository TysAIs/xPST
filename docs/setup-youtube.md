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
8. [Verify](#verify)
9. [Token storage & refresh](#token-storage--refresh)
10. [Troubleshooting](#troubleshooting)

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

> While the consent screen is in **Testing** mode, only the accounts you add as test users can authorize. For personal use that's exactly what you want — you don't need to publish the app or go through verification.

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

The saved token includes a **refresh token**, so once you've authorized once you should never need to do the browser dance again. xPST:

- Detects when the access token is expired.
- Uses the refresh token to get a new access token automatically.
- Re-saves the refreshed token to `youtube_token.json` (`0600`).

You only need to re-run `xpst connect youtube` if:
- You revoke xPST's access in your Google account settings, or
- You delete `youtube_token.json` and `youtube_token.enc`, or
- You change the Google account / channel.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `access_denied` during OAuth | Your Google account isn't on the consent screen's **Test users** list (Step 3, item 5). Add it and retry. |
| `redirect_uri_mismatch` | You created a **Web application** credential instead of **Desktop app**. Recreate it as Desktop app (Step 4). |
| `File not found` for client_secrets | The file isn't at `~/.xpst/credentials/youtube_client_secrets.json`. Check the exact name. |
| `API not enabled` error | You skipped Step 2 — enable YouTube Data API v3 in the Cloud Console. |
| `quotaExceeded` | You hit the daily YouTube API quota (10,000 units). It resets at midnight Pacific time. Uploads cost ~1600 units each. |
| Port `8085` already in use | Another process holds port 8085 (xPST's OAuth callback port). Close it or check `lsof -i :8085`. |
| Token works but upload fails with `400` | The video may exceed Shorts limits (>3 min, wrong aspect ratio) or be in an unsupported codec. xPST re-encodes to YouTube's spec with FFmpeg — ensure FFmpeg is installed (`ffmpeg -version`). |

See [troubleshooting.md](troubleshooting.md) for cross-platform issues and the credential reset procedure.
