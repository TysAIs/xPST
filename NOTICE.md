# NOTICE — Third-Party Software Licenses

xPST is dual-licensed under the **MIT License OR Apache License 2.0**, at your option.
See [LICENSE](LICENSE) for the full text of both licenses.

This file documents third-party software included with or required by xPST.

---

## Core Dependencies (installed by default)

All core dependencies use permissive, OSI-approved licenses compatible with
xPST's dual MIT/Apache-2.0 license:

| Package | License | Purpose |
|---------|---------|---------|
| click | BSD-3-Clause | CLI framework |
| pyyaml | MIT | Config file parsing |
| rich | MIT | Terminal formatting |
| yt-dlp | Unlicense | Video downloading |
| google-api-python-client | Apache-2.0 | YouTube Data API |
| google-auth-oauthlib | Apache-2.0 | Google OAuth |
| google-auth-httplib2 | Apache-2.0 | Google auth transport |
| twikit | MIT | X/Twitter API (community) |
| instagrapi | MIT | Instagram API (community) |
| structlog | MIT | Structured logging |
| prometheus-client | Apache-2.0 | Metrics endpoint |
| keyring | MIT | OS keychain integration |
| bcrypt | Apache-2.0 | Password hashing |
| cryptography | Apache-2.0 OR BSD-3-Clause | Encryption (Fernet) |
| fastapi | MIT | Dashboard API server |
| uvicorn | BSD-3-Clause | ASGI server |
| httpx | BSD-3-Clause | HTTP client |
| pydantic-settings | MIT | Config management |
| msgpack | Apache-2.0 | Serialization |

## Optional Dependencies (extras)

These are installed only when explicitly requested (`pip install xpst[extra]`):

### `desktop` extra — Desktop GUI

| Package | License | Notes |
|---------|---------|-------|
| PySide6 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | Qt bindings. **Users choose LGPL** at runtime — no copyleft obligation to xPST itself. The LGPL option allows linking without infecting xPST's MIT/Apache license. |

> **LGPL Compliance:** PySide6 is used as a library via the LGPL-3.0 option.
> xPST itself remains MIT/Apache-2.0 dual-licensed. Users who distribute xPST
> with PySide6 must comply with LGPL-3.0 requirements (provide object files,
> include LGPL notice). See: https://www.qt.io/licensing/

### `anti-ban` extra — TLS fingerprint hardening

| Package | License | Purpose |
|---------|---------|---------|
| curl_cffi | MIT | Browser-like TLS fingerprints |

### `mcp` extra — AI agent integration

| Package | License | Purpose |
|---------|---------|---------|
| mcp | MIT | Model Context Protocol server |

### `knowledge` extra — Transcript search

| Package | License | Purpose |
|---------|---------|---------|
| faster-whisper | MIT | Speech-to-text |
| fastembed | Apache-2.0 | Text embeddings |
| sqlite-vec | MIT | Vector storage |
| lancedb | Apache-2.0 | Legacy vector backend |

### `desktop` extra — Web-based desktop fallback

| Package | License | Purpose |
|---------|---------|---------|

### `dashboard` extra — Web dashboard

| Package | License | Purpose |
|---------|---------|---------|

### `windows` extra — Windows integration

| Package | License | Purpose |
|---------|---------|---------|
| pywin32 | BSD-3-Clause | Windows API bindings |
| winshell | MIT | Windows shell integration |

## Build & Development Dependencies (not distributed)

These are used only during development and are not included in the published
package:

| Package | License | Purpose |
|---------|---------|---------|
| pyinstaller | GPL-2.0 | Desktop binary packaging (**build-only** — not distributed to users) |
| pyinstaller-hooks-contrib | Apache-2.0 OR GPL-2.0 | PyInstaller hooks |
| pytest | MIT | Testing |
| ruff | MIT | Linting |
| mypy | MIT | Type checking |

> **GPL build tools:** pyinstaller is used only to build standalone binaries
> and is NOT included in the distributed package or listed as a runtime
> dependency. The GPL-2.0 license applies only to the build tool itself, not
> to xPST or its output binaries.

## Transitive Dependencies

Some transitive dependencies (pulled in automatically by core deps) use
permissive licenses:

| Package | License | Pulled in by |
|---------|---------|-------------|
| CairoSVG | LGPL-3.0-or-later | PySide6 (optional rendering) |
| chardet | LGPL-2.1-or-later | Various (charset detection) |
| docutils | BSD OR GPL OR Public Domain | Sphinx/documentation tools |

> These LGPL-licensed transitive dependencies are used as libraries and do not
> affect xPST's licensing. Users who redistribute xPST with these dependencies
> must include the LGPL notices.

---

## License Compatibility Summary

- **xPST core:** MIT OR Apache-2.0 (user's choice) — fully permissive
- **All runtime dependencies:** Permissive (MIT, Apache, BSD) or LGPL (user chooses LGPL option)
- **Build tools (GPL):** Not distributed — only used during development
- **No GPL dependencies** in the distributed package or default install

xPST can be used, modified, and distributed under either the MIT or Apache-2.0
license without any copyleft obligations.
