# xPST First-Run Connection Guide

One-time, per-platform human approval steps. The in-app wizard (`xpst wizard`) shows the same instructions interactively.

## YouTube Shorts

Publishes your videos as YouTube Shorts.

1. Create (or select) a Google Cloud project: https://console.cloud.google.com/projectcreate (pick an existing project: https://console.cloud.google.com/projectselector2/YOUR_PROJECT_ID)
2. Open the OAuth consent screen (Google Auth Platform → Audience): https://console.cloud.google.com/auth/audience
3. IMPORTANT — click 'Publish App' to move the app to 'In production'. Apps left in 'Testing' issue refresh tokens that EXPIRE AFTER 7 DAYS, so you would have to re-authorize xPST every week. Note: the xPST project's own Google Cloud app is already published to production, so its tokens are long-lived — if you create your own project, publish it to production too (see docs/youtube-oauth-production.md).
4. Create the OAuth client (Google Auth Platform → Clients → 'Create Client'): https://console.cloud.google.com/auth/clients (legacy UI: https://console.cloud.google.com/apis/credentials → 'Create Credentials' → 'OAuth client ID')
5. Application type: 'Desktop app'. Name it e.g. 'xPST Desktop'. YouTube scopes are sensitive, so Google may show an 'unverified app' warning during consent — choose 'Advanced' → 'Continue' (fine for personal, single-user use).
6. Download the client JSON and save it as ~/.xpst/credentials/youtube_client_secrets.json
7. Back in xPST, re-run `xpst connect youtube` — your browser opens for the one-time Google approval. Sign in and click 'Allow'.

More details: https://github.com/TysAIs/xPST/blob/main/docs/youtube-oauth-production.md


## Instagram Reels

Cross-posts your videos as Instagram Reels.

1. Instagram posting uses the official Meta Graph API. You need a Meta developer app: open https://developers.facebook.com/apps
2. Click 'Create App' → type 'Business'.
3. Add the 'Instagram Graph API' product to the app.
4. Connect your Instagram Business account to a Facebook Page (required by Meta). In Instagram: Settings → Business tools.
5. Generate a long-lived access token (Graph API Explorer → generate token with instagram_basic + instagram_content_publish permissions, then exchange for a long-lived token).
6. Paste the token when the wizard asks. It also needs your numeric IG user ID (it can look this up automatically).


## X / Twitter

Posts your videos to X/Twitter.

1. Option A (official API): open https://developer.x.com and create a Project + App. Free tier allows ~17 posts/day.
2. Generate the Bearer Token and paste it into the wizard — it verifies immediately against the X API.
3. Option B (cookies): log into x.com in your browser, export cookies (auth_token + ct0), and choose the cookies option in the wizard.


## TikTok

Watches a TikTok account as the video source.

1. For SOURCE mode (watching an account), you only need yt-dlp installed — no app required. The wizard checks this for you (`uv tool install yt-dlp` if missing).
2. For UPLOADING to TikTok (Content Posting API): open https://developers.tiktok.com/apps and click 'Manage apps' → 'Create app'.
3. Add the 'Content Posting API' product. Set a redirect URI (e.g. http://localhost:8080/callback). Note the client key and client secret.
4. Submit the app for audit; while pending you can still post to private/self-only audience from the same account.


## Threads

Posts to Threads via the official Threads API.

1. Open https://developers.facebook.com/apps
2. 'Create App' → use case 'Threads API access'.
3. Connect your Threads profile and generate an access token.
4. Paste the token and your numeric Threads user ID into the wizard.


## Facebook Page

Publishes as a Facebook Page (Pages only — personal profiles have no publishing API).

1. Facebook publishing is Page-scoped and needs your own Meta app: open https://developers.facebook.com/apps
2. 'Create App' → type 'Business', then add the 'Facebook Login for Business' product.
3. Grant the app pages_show_list, pages_read_engagement and pages_manage_posts (Standard Access is enough for your own Pages).
4. In the Graph API Explorer pick that app, select the permissions above and generate a User Access Token.
5. Run `xpst auth facebook` and paste the token (plus the App ID/Secret so the token is exchanged for a long-lived one).
6. xPST lists your Pages via GET /me/accounts, verifies the chosen Page token and stores it encrypted. No Page → a clear error, never a personal-profile post.

More details: https://github.com/TysAIs/xPST/blob/main/docs/setup-facebook.md


## Messenger (optional)

Optional: reply to viewers via Messenger webhook.

1. This is optional — press Enter to skip unless you need it.
2. In your Meta app, add the Messenger product.
3. Generate a Page Access Token and paste it into the wizard.


## Bluesky

Posts text, images and multi-image posts through the open AT Protocol — no app review, no fee, no developer console.

1. You need a Bluesky account (any handle, including a custom domain).
2. Open https://bsky.app/settings/app-passwords
3. Click 'Add App Password', name it 'xPST' and copy the value.
4. Run `xpst auth bluesky` and paste your handle and the app password — it is stored encrypted, never in config.yaml.
5. Video is not supported: Bluesky serves video from a separate service and xPST does not claim it. Text and images publish normally.

More details: https://docs.bsky.app/

