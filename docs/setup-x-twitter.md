# X / Twitter Setup

> **Auth method:** Login-based via `twikit` — enter username, email, and password; xPST saves the resulting session cookies. No manual cookie export needed.
> **Time:** ~2 minutes.

X/Twitter has no fully open public write API for individuals (the official API tiers are gated and expensive). xPST therefore authenticates via `twikit`, a library that performs a normal browser-style login and persists the session cookies — the same approach the official web client uses. You enter your credentials once; xPST logs in, saves cookies to `~/.xpst/credentials/x_cookies.json`, and reuses them for every post thereafter.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Step 1 — Run `xpst connect x`](#step-1--run-xpst-connect-x)
3. [What happens during login](#what-happens-during-login)
4. [Verify](#verify)
5. [Cookie storage & refresh](#cookie-storage--refresh)
6. [Two-factor authentication](#two-factor-authentication)
7. [Troubleshooting](#troubleshooting)

---

## Prerequisites

- An **X/Twitter account** that can log in via the web.
- Your **username** (the `@handle`), the **email address** on the account, and the **password**.
- Optional: `twikit` installed (xPST will tell you if it's missing — `pip install twikit`).

> X uses email + password for login verification, so the email field is required even though you log in with a username.

---

## Step 1 — Run `xpst connect x`

```bash
xpst connect x
```

The wizard prompts:

```
X username (without @): _
X email address: _
X password: (input hidden)
```

Enter each value and press Enter. The username may be typed with or without the leading `@` — xPST strips it. The password is entered hidden (via `getpass`).

Then:

```
Connecting to X/Twitter...
✅ Connected as @yourhandle
```

xPST logs in, verifies the session by fetching the logged-in user, and reports your screen name. It also tightens the cookies file to `0600` (owner-only) and mirrors the cookies into the encrypted credential store.

---

## What happens during login

Under the hood, `xpst connect x`:

1. Creates a `twikit.Client`.
2. Calls `client.login(auth_info_1=<username>, auth_info_2=<email>, password=<password>, cookies_file=~/.xpst/credentials/x_cookies.json)`.
3. twikit performs the login request, receives session cookies, and writes them to the file itself.
4. xPST re-chmods the file to `0600`.
5. xPST copies the cookie JSON into the encrypted store as `x_cookies.enc`.
6. xPST verifies by calling `client.user()` and printing your `@screen_name`.

No browser opens and no cookie export from a browser extension is needed — twikit handles the login end-to-end.

---

## Verify

```bash
# Test the connection (no post)
xpst connect --test
#   ✅ X/Twitter: @yourhandle

# If cookies are present but stale, you'll see:
#   ⚠️  X/Twitter: Cookies present but may be expired

# Post a test clip
xpst post -v ~/Videos/clip.mp4 -c "Cross-posting with xPST 🐦" -p x
```

### What gets stored

| File | Contents | Permissions |
|------|----------|-------------|
| `~/.xpst/credentials/x_cookies.json` | twikit session cookies (auth_token, ct0, etc.) | `0600` |
| `~/.xpst/credentials/x_cookies.enc` | Encrypted copy (Fernet) | `0600` |

Your **password is never stored** — only the resulting session cookies. If you revoke the session from X's settings, the cookies stop working and you'll re-login with `xpst connect x`.

---

## Cookie storage & refresh

Session cookies on X expire (or get invalidated) after a period of time, especially if:
- You log out of X on the web,
- X flags the session for security review,
- You haven't posted in a long time.

When cookies are stale, posts will fail with auth errors. To refresh:

```bash
xpst connect x        # Re-enter username/email/password
```

This overwrites `x_cookies.json` and `x_cookies.enc` with a fresh session. You do **not** need to delete the old files first — xPST overwrites them.

> `xpst health` will warn you if your X session file is older than 30 days ("platforms commonly expire or challenge these; re-auth").

---

## Two-factor authentication

If your X account has 2FA enabled and twikit's login flow requires a code, `xpst connect x` will prompt for the 2FA/verification code. Enter the code from your authenticator app (or SMS) when asked.

If login fails with a "verification" or "challenge" error, X may be asking for an email confirmation code — check your inbox and retry. For repeated issues, temporarily disable 2FA during the initial connection, then re-enable it; the saved cookies will continue to work.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `twikit not installed` | `pip install twikit` (xPST declares it as an optional dependency for X). |
| `Invalid credentials` | Wrong username, email, or password. Remember X needs the **email on the account**, not just the username. |
| `Account appears to be suspended` | The account is locked or suspended on X's side — nothing xPST can fix. |
| `Rate limited` during login | X throttled repeated login attempts. Wait ~15 minutes and retry. |
| `Cookies present but may be expired` (from `--test`) | Re-run `xpst connect x` to refresh the session. |
| Posts fail with `401 Unauthorized` | Cookies are stale or revoked. Re-run `xpst connect x`. |
| Login keeps failing despite correct credentials | X sometimes triggers a login challenge. Log in to X on the web once to "clear" the challenge state, then retry `xpst connect x`. |

See [troubleshooting.md](troubleshooting.md) for the full cookie-refresh and credential-reset procedures.
