# LinkedIn Setup

xPST posts videos to LinkedIn using the official **LinkedIn API v2** with OAuth 2.0 authentication. Videos are uploaded as article updates via the `/v2/posts` endpoint with a registered media asset.

## Prerequisites

- A **LinkedIn account** with access to [LinkedIn Developers](https://www.linkedin.com/developers/)
- Your LinkedIn account must have permission to post (Personal profile or Company Page)

## Step 1 — Create a LinkedIn App

1. Go to **[linkedin.com/developers](https://www.linkedin.com/developers/)** → **Create app**
2. Fill in:
   - **App name:** xPST (or any name you prefer)
   - **LinkedIn Page:** Select your company page, or create one if needed
   - **Privacy policy URL:** Your website or a placeholder
   - **App logo:** Optional
3. Click **Create app**

## Step 2 — Configure OAuth 2.0

1. In your app dashboard, go to the **Auth** tab
2. Under **Authorized redirect URLs**, add:
   ```
   http://localhost:8765/callback
   ```
3. Under **OAuth 2.0 scopes**, add these permissions:
   - `w_member_social` — Post on behalf of the user
   - `r_organization_social` — Read organization posts (optional, for Company Page posting)
   - `w_organization_social` — Post as organization (optional, for Company Page posting)
4. Note your **Client ID** and **Client Secret**

## Step 3 — Get Your Access Token

### Option A: Use xPST's built-in flow (recommended)

```bash
xpst auth linkedin
```

xPST will open a browser for the OAuth consent flow and automatically save the token.

### Option B: Manual token retrieval

1. Go to the **OAuth 2.0 tools** section in your app dashboard
2. Use the **Token Generator** to generate a 60-day access token
3. Copy the token

## Step 4 — Get Your LinkedIn User ID

1. Use the token to call the LinkedIn API:
   ```bash
   curl -H "Authorization: Bearer YOUR_TOKEN" \
     https://api.linkedin.com/v2/userinfo
   ```
2. Note the `sub` field — this is your LinkedIn user ID

## Step 5 — Configure xPST

Add your LinkedIn credentials to `~/.xpst/config.yaml`:

```yaml
linkedin:
  enabled: true
  access_token: "YOUR_ACCESS_TOKEN"
  linkedin_user_id: "YOUR_USER_ID"
```

Or use environment variables:

```bash
export XPST_LINKEDIN_ACCESS_TOKEN="your_token"
export XPST_LINKEDIN_LINKEDIN_USER_ID="your_user_id"
```

## Step 6 — Verify the Connection

```bash
xpst health --json | jq '.platforms.linkedin'
```

You should see:

```json
{
  "authenticated": true,
  "session_valid": true,
  "error": null
}
```

## Token Refresh

LinkedIn access tokens expire after **60 days**. When your token expires:

1. Run `xpst auth linkedin` again to get a fresh token, **or**
2. Generate a new token in the LinkedIn Developers portal and update your config

xPST will report `session_valid: false` in health checks when the token expires.

## Limits

| Resource | Limit |
|----------|-------|
| Posts per day | ~150 (server-enforced) |
| Max video size | 200 MB recommended (1 GB max) |
| Max caption length | 3,000 characters |
| Token lifetime | 60 days |

## Company Page Posting

To post as a Company Page instead of your personal profile:

1. Ensure your LinkedIn app has `w_organization_social` scope
2. Use the **organization URN** (e.g., `urn:li:organization:12345`) as the `linkedin_user_id`
3. You must be an admin of the Company Page

## Troubleshooting

### "401 Unauthorized"
- Your access token has expired. Run `xpst auth linkedin` to refresh it.

### "403 Forbidden — Throttled"
- You've hit the daily post limit (~150). Wait 24 hours.

### "Video too large"
- LinkedIn recommends videos under 200 MB. Use `xpst post -v video.mp4 -p linkedin` and xPST will encode to a compliant profile automatically.

### Token not saving
- Check that `~/.xpst/credentials/` exists and has `0600` permissions:
  ```bash
  ls -la ~/.xpst/credentials/
  ```

See [LinkedIn Marketing API docs](https://learn.microsoft.com/en-us/linkedin/marketing/) for the full API reference.
