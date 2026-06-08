# Security Policy

## Credential Storage

xPST takes credential security seriously. All authentication tokens, session cookies, and API keys are stored using a layered security approach:

### Primary: OS Keychain (Recommended)

When the `keyring` package is installed (default), credentials are stored in the operating system's native secure storage:

- **macOS**: Keychain (encrypted, requires Touch ID or password to access)
- **Windows**: Credential Locker (encrypted, tied to user account)
- **Linux**: Secret Service (GNOME Keyring, KWallet)

This provides:
- Encryption at rest
- OS-level access control
- No plain-text secrets on disk
- Integration with system security policies

### Fallback: Encrypted File Storage

If the OS keychain is unavailable (e.g., headless server, Docker container), xPST falls back to file-based storage in `~/.xpst/credentials/`. These files are JSON-encoded but **not encrypted** — the OS filesystem permissions provide the security layer.

### What Is Stored

| Credential | Key | Storage |
|---|---|---|
| YouTube OAuth Token | `youtube_token` | Keychain / file |
| X/Twitter Cookies | `x_cookies` | Keychain / file |
| Instagram Session | `instagram_session` | Keychain / file |

### What Is NOT Stored

- Passwords are **never** stored by xPST
- API keys are read from environment variables or config files
- OAuth client secrets (`client_secrets.json`) must be provided by the user

## .gitignore Protection

The repository `.gitignore` is configured to prevent accidental commits of:

- All credential files (`*.json` patterns for tokens, cookies, sessions)
- Environment files (`.env`, `.env.*`)
- State files containing user-specific data
- Session directories
- The entire `~/.xpst/` runtime directory

## Reporting a Vulnerability

If you discover a security vulnerability in xPST, please report it responsibly:

1. **Do NOT** open a public GitHub issue
2. Email the maintainers privately (see README.md for contact info)
3. Include a description of the vulnerability and steps to reproduce
4. Allow reasonable time for a fix before public disclosure

## Security Best Practices for Users

1. **Use the OS keychain** — install `keyring` for automatic secure storage
2. **Rotate credentials regularly** — re-authenticate platforms periodically
3. **Use environment variables** for API keys in production/deployment
4. **Review `.gitignore`** before pushing to ensure no secrets are tracked
5. **Run `xpst auth status`** to verify credential storage health
6. **Never share** your `~/.xpst/` directory or credential files

## Dependencies

xPST's security depends on:

- [`keyring`](https://pypi.org/project/keyring/) — OS keychain integration
- [`google-auth`](https://pypi.org/project/google-auth/) — OAuth2 token management
- [`twikit`](https://pypi.org/project/twikit/) — X/Twitter session management
- [`instagrapi`](https://pypi.org/project/instagrapi/) — Instagram session management

## Platform Terms of Service Compliance

### ⚠️ Important Notice

xPST uses unofficial APIs and third-party libraries to interact with social media platforms. Users are solely responsible for ensuring their use complies with platform Terms of Service.

### Unofficial APIs Used

| Platform | Library | Status | Risk Level |
|----------|---------|--------|------------|
| Instagram | instagrapi | Unofficial API | High |
| X/Twitter | twikit | Unofficial API | High |
| YouTube | google-api-python-client | Official API | Low |
| TikTok | yt-dlp | Unofficial/Scraping | Medium |

### Platform-Specific Risks

#### Instagram (via instagrapi)
- Uses unofficial private API endpoints
- May violate Instagram's Terms of Service
- Risk of account suspension or ban
- Instagram actively detects and blocks unofficial API usage

#### X/Twitter (via twikit)
- Uses unofficial API endpoints
- May violate X/Twitter's Terms of Service
- Risk of account suspension or ban
- X/Twitter has been increasingly restrictive with API access

#### TikTok (via yt-dlp)
- Uses video scraping/downloading techniques
- May violate TikTok's Terms of Service
- Risk of IP-based rate limiting or blocking
- TikTok actively works to prevent unauthorized downloading

#### YouTube (via google-api-python-client)
- Uses official, Google-approved API
- Complies with YouTube's Terms of Service
- Subject to API quota limits
- Lowest risk of account issues

### Recommendations

1. **Use Official APIs When Possible**
   - YouTube: Already using official API ✅
   - Consider official APIs for other platforms if available

2. **Respect Rate Limits**
   - Use xPST's built-in rate limiting features
   - Avoid excessive posting frequency
   - Monitor platform notifications

3. **Account Separation**
   - Consider using dedicated accounts for automation
   - Don't use your primary personal accounts
   - Be prepared for potential account restrictions

4. **Stay Informed**
   - Monitor platform Terms of Service changes
   - Follow library communities for updates
   - Be aware of platform enforcement actions

5. **Legal Compliance**
   - Ensure compliance with local laws regarding automation
   - Respect copyright and intellectual property rights
   - Don't use for spam, harassment, or abusive purposes

### Disclaimer

THE DEVELOPERS OF xPST ARE NOT RESPONSIBLE FOR:
- Account suspensions, bans, or restrictions
- Loss of data or content
- Legal consequences of ToS violations
- Any damages resulting from platform enforcement actions

USE THIS SOFTWARE AT YOUR OWN RISK AND IN COMPLIANCE WITH ALL APPLICABLE TERMS OF SERVICE AND LAWS.
