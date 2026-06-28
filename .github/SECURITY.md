# Security Policy

xPST takes security seriously. The full vulnerability-reporting policy and
credential-storage model live in the root [`SECURITY.md`](../SECURITY.md).

## Reporting a Vulnerability

- **Do not** open a public GitHub issue for security problems.
- Review the supported versions and disclosure timeline in
  [`SECURITY.md`](../SECURITY.md).
- Report vulnerabilities privately per the contact instructions in that file.

## Credential Storage

xPST never writes secrets in cleartext. Credentials are stored in the OS
keychain (macOS Keychain / Windows Credential Locker / Linux Secret Service)
with an encrypted Fernet file fallback. See
[`SECURITY.md`](../SECURITY.md) and
[`src/xpst/utils/credentials.py`](../src/xpst/utils/credentials.py) for the
full model.
