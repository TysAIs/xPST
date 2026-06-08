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

xPST stores credentials in the OS keychain when available. If the keychain is unavailable, xPST falls back to local JSON files in the user configuration directory. Those fallback files are not encrypted by xPST and rely on operating-system filesystem permissions.

## User Responsibility

Users are responsible for:

- Keeping local account files private
- Reviewing platform Terms of Service before enabling integrations
- Avoiding spam, platform manipulation, and unauthorized scraping
- Removing credentials with `xpst auth logout` or by deleting local xPST credential files when needed

