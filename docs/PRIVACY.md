# Privacy

xPST is local-first. There is no xPST cloud service, hosted account, telemetry endpoint, or subscription backend in this repository.

## What Stays Local

- Video files
- Captions and schedule data
- Upload history and state files
- Browser cookies and session files
- OAuth tokens
- Configuration files

## What Leaves The Machine

Data leaves the machine only when xPST talks to a platform or service that the user configures:

- YouTube API requests for YouTube uploads and metadata
- Instagram requests made through the configured Instagram session
- X/Twitter requests made through the configured X session
- TikTok/source requests made through downloader workflows
- Optional webhook notifications, such as Discord or Telegram

## Credential Storage

xPST's `CredentialStore` uses Fernet-encrypted `.enc` files by default. An OS
keychain can be selected explicitly with `XPST_USE_KEYRING=1` when the platform
keychain is available. If `cryptography` is unavailable, xPST refuses to write a
credential rather than falling back to plaintext. Platform-specific token,
cookie, and session files may still be written with owner-only permissions;
treat the whole `~/.xpst/` directory as sensitive.

## User Responsibility

Users are responsible for:

- Keeping local account files private
- Reviewing platform Terms of Service before enabling integrations
- Avoiding spam, platform manipulation, and unauthorized scraping
- Removing credentials with `xpst disconnect <platform> --yes` or by deleting local xPST credential files when needed
