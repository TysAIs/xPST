# X/Twitter Authentication Guide

How to set up X/Twitter authentication for xPST using browser cookies.

---

## Overview

xPST uses **[twikit](https://github.com/david-lev/twikit)** to upload videos to X/Twitter. twikit authenticates using browser cookies — no API keys or developer accounts needed. This is the same approach used by the official X website.

---

## Method 1: Browser Cookie Export (Recommended)

### Step 1: Log into X

1. Open your browser (Chrome, Firefox, Safari, or Edge)
2. Go to [https://x.com](https://x.com)
3. Log in with your credentials
4. Make sure you're fully logged in (you can see your timeline)

### Step 2: Export Cookies

Install a cookie editor extension:

- **Chrome:** [EditThisCookie](https://chrome.google.com/webstore/detail/editthiscookie/fngmhnnpilhplaeedifhccceomclgfbg) or [Cookie-Editor](https://chrome.google.com/webstore/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm)
- **Firefox:** [Cookie-Editor](https://addons.mozilla.org/en-US/firefox/addon/cookie-editor/)
- **Safari:** Developer tools → Storage → Cookies

**Steps:**

1. Navigate to `x.com` (make sure you're on the X domain)
2. Click the cookie editor extension icon
3. Click **"Export"** → **"Export as JSON"**
4. Copy the JSON to clipboard

### Step 3: Save the Cookies File

Create the credentials directory and save:

```bash
mkdir -p ~/.xpst/credentials
```

Paste the cookies into `~/.xpst/credentials/x_cookies.json`:

```bash
# Open in your editor
nano ~/.xpst/credentials/x_cookies.json
```

The file should look like this (array of cookie objects):

```json
[
  {
    "domain": ".x.com",
    "expirationDate": 1735689600,
    "hostOnly": false,
    "httpOnly": true,
    "name": "auth_token",
    "path": "/",
    "secure": true,
    "value": "abc123def456..."
  },
  {
    "domain": ".x.com",
    "name": "ct0",
    "path": "/",
    "secure": true,
    "value": "xyz789..."
  }
]
```

> **Important:** The cookies must include at least `auth_token` and `ct0` cookies from the `.x.com` domain.

### Step 4: Store in Keychain

```bash
xpst auth x
```

This imports the cookies from the file into your OS keychain for secure storage.

### Step 5: Verify

```bash
xpst health
```

You should see:

```
✅ X/Twitter: Connected
   Username: @yourusername
```

---

## Method 2: CLI Authentication

Use the `xpst auth x` command for guided setup:

```bash
xpst auth x
```

This will:

1. Check if cookies already exist in the keychain
2. If a cookies file exists at `~/.xpst/credentials/x_cookies.json`, import it into the keychain
3. Display instructions for obtaining cookies if none are found

---

## Method 3: Desktop App

If using the xPST desktop app:

1. Launch: `xpst app`
2. Go to the **Connect** page
3. Click **"Connect X/Twitter"**
4. Use the **"Paste Cookies"** dialog to paste your exported JSON
5. Click **"Save"**

---

## Cookie Format Reference

twikit expects cookies in a specific format. The minimum required cookies are:

| Cookie Name | Domain | Purpose |
|------------|--------|---------|
| `auth_token` | `.x.com` | Authentication token (long-lived) |
| `ct0` | `.x.com` | CSRF token (session-scoped) |

### Full Cookie Object Structure

```json
{
  "domain": ".x.com",
  "expirationDate": 1735689600,
  "hostOnly": false,
  "httpOnly": true,
  "name": "auth_token",
  "path": "/",
  "sameSite": "no_restriction",
  "secure": true,
  "session": false,
  "storeId": "0",
  "value": "abc123..."
}
```

### Simplified Format (Also Accepted)

twikit also accepts a simplified format:

```json
{
  "auth_token": "abc123...",
  "ct0": "xyz789..."
}
```

Or as a JSON file:

```json
{
  "auth_token": "abc123...",
  "ct0": "xyz789..."
}
```

---

## Troubleshooting

### "Session expired" or "Authentication failed"

**Cause:** Cookies have expired (typically after 2-4 weeks of inactivity).

**Fix:**

1. Log into x.com in your browser
2. Re-export cookies (see Method 1 above)
3. Overwrite `~/.xpst/credentials/x_cookies.json`
4. Re-import: `xpst auth x`

### "Cookie file not found"

**Cause:** The cookies file doesn't exist at the configured path.

**Fix:**

```bash
# Check what path is configured
xpst config show

# Create the credentials directory
mkdir -p ~/.xpst/credentials

# Place your cookies file there
# The default path is: ~/.xpst/credentials/x_cookies.json
```

### "Invalid cookie format"

**Cause:** The JSON file doesn't contain the required cookies.

**Fix:** Make sure the file is valid JSON and contains at least `auth_token` and `ct0`. The file should be either:
- An array of cookie objects (full export format)
- A flat object with cookie names as keys

### "Rate limited" or "Too many requests"

**Cause:** You've hit X's rate limits.

**Fix:**

```bash
# Check your rate limits
xpst status --json

# Reduce daily upload limit
xpst config set rate_limits.x 3
```

### Uploads fail but health check passes

**Cause:** Cookies may be partially valid (enough for reading, not for posting).

**Fix:**

1. Clear the cached cookies: `xpst auth x`
2. Re-export fresh cookies from your browser
3. Make sure you're actively logged in to x.com

### "ct0" cookie missing

**Cause:** The `ct0` CSRF cookie is required for write operations.

**Fix:**

1. Make sure you're logged into x.com (not just visiting)
2. Refresh the page before exporting cookies
3. Export cookies specifically from the `x.com` domain

---

## Security Notes

- **Cookies are stored in your OS keychain** (macOS Keychain, Linux Secret Service, Windows Credential Manager)
- **The cookies file is optional** after initial import — you can delete `~/.xpst/credentials/x_cookies.json` after running `xpst auth x`
- **Never commit cookies to version control** — the `.gitignore` should exclude `~/.xpst/credentials/`
- **Cookies contain session tokens** — treat them like passwords

---

## Cookie Expiration

X auth tokens (`auth_token`) typically last:
- **2-4 weeks** of inactivity
- **Months** if you remain active on X
- **Immediately** if you log out of x.com in your browser

### Signs of Expired Cookies

- Health check shows `Session may be expired` for X
- Uploads fail with `401 Unauthorized`
- Post attempts return `Session expired` errors

### Preventing Expiration

- Stay logged into x.com in your browser
- Run `xpst health` periodically to catch expiration early
- Set up notifications to alert on auth failures

---

## See Also

- [Install Guide](INSTALL.md) — Full setup instructions
- [Agent Guide](AGENT_GUIDE.md) — CLI and Python API
- [MCP Tools Reference](MCP_TOOLS.md) — AI integration
