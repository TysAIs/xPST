# Bring your own developer app (BYO)

xPST ships **no shared, multi-tenant social app** — and it never asks you to
wait for an app review. Instead, each supported platform can run on **an app
you own**: you create a developer app at the provider, paste its App ID and
App Secret into xPST once, and xPST uses *your* app for OAuth and API calls.

Why this matters: Meta's **Standard Access** is defined as app use by people
who have a role on the app. You created the app, so you are its owner —
publishing to your own Instagram account, Threads profile, or Facebook Page
needs **no App Review and no Business Verification**. The review queue that
blocks every multi-tenant third-party tool simply does not apply to you.

TikTok works the same way mechanically (you register your own client key),
with one honest caveat: unaudited TikTok apps are limited to **SELF_ONLY**
(private) direct posts until TikTok audits the app. xPST never claims public
reach it does not have.

## What xPST stores, and what it never shows

* The App **Secret** is written only to the encrypted credential store under
  your xPST config dir (`~/.xpst/credentials/*.enc` by default, Fernet at
  rest). It is never synced, never committed, never placed in a build.
* The App **ID** is mirrored into `config.yaml` because an app id is not a
  secret — it is how the UI shows you *which* app is in play.
* Every surface (UI, CLI, MCP, HTTP) shows the credential **masked**: at most
  a four-character id tail (`…3456`). The secret is never echoed back, never
  logged, and never appears in an API response.

## Setting an app up

Pick whichever surface you like — they all write the same encrypted store.

### In the desktop app

Open **Connect** → pick Instagram, Threads, or TikTok → the *Bring your own
developer app* panel explains what the credential unlocks, links the
provider's app-creation page, and stores what you paste. When a Meta app is
stored, the **Sign in** button for Instagram and Threads lights up with it.

### From a terminal

```bash
xpst byo status            # what has an app, what is missing (masked)
xpst byo set instagram     # prompts; the secret input is hidden
xpst byo set tiktok --app-id awcl9k…   # secret via env, see below
xpst byo clear threads     # remove a stored pair
```

For agents and CI, pass the id with `--app-id` and the secret through the
platform env var so it never lands in a process listing:

| Platform | ID env var | Secret env var |
|----------|------------|----------------|
| Instagram | `XPST_INSTAGRAM_APP_ID` | `XPST_INSTAGRAM_APP_SECRET` |
| Threads | `XPST_THREADS_APP_ID` | `XPST_THREADS_APP_SECRET` |
| TikTok | `XPST_TIKTOK_CLIENT_KEY` | `XPST_TIKTOK_CLIENT_SECRET` |

### From an AI agent (MCP)

Call `xpst_byo_app` with `platform`, `app_id`, `app_secret` (or `clear:
true`). The reply is masked by construction, so the tool payload is safe for
an agent to log. Omit the fields for a status read.

## Per-platform notes

**One Meta app covers Instagram, Threads and Messenger.** Enter it once;
all three product surfaces see the same stored pair. Register the redirect
URI `http://localhost:8888/callback` on the app (Meta accepts loopback
redirects for apps in Development mode). After pasting the credential,
click **Sign in** — xPST opens the provider's own consent page, exchanges
the code with *your* app's id/secret, extends it to a long-lived token, and
stores the resulting account token encrypted. See
[setup-instagram.md](setup-instagram.md) / [setup-threads.md](setup-threads.md)
for the scopes each product needs.

**TikTok:** create an app at <https://developers.tiktok.com/manage/apps>,
add the Content Posting API, and register the redirect
`http://localhost:8085/callback`. Unaudited apps post `SELF_ONLY` — a
private post you can check before resubmitting for audit.

**Facebook Pages** keep their own connect flow (`xpst auth facebook`), which
already takes a BYO app id/secret; the BYO status line just shows whether one
is stored.

**YouTube** needs a Google *Desktop* OAuth client
(`credentials/youtube_client_secrets.json`) — the same idea, different shape,
documented in [setup-youtube.md](setup-youtube.md).
