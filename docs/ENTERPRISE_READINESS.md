# Enterprise Readiness

xPST is designed to remain free, open source, local-first, and user-controlled while still being packaged like a professional desktop app, CLI, and MCP server. This document defines the release bar for that goal.

## Current Status

| Area | Status | Notes |
|---|---:|---|
| Core tests | Passing | 793 tests collected; macOS validation passed and Windows local validation passed with 787 passing, 6 skipped. |
| Lint | Passing | `ruff check src tests` passes. Qt/QML bridge names are explicitly ignored where camelCase is required by QML. |
| Vulnerability audit | Passing | `pip-audit` reports no known vulnerabilities in the local environment. |
| Python package | Passing | Source distribution and wheel build successfully. |
| Windows executable | Mostly passing | Direct PyInstaller build produced `dist/xPST.exe`; the `xpst build --json` wrapper can run longer than the current tool timeout and should be exercised in CI with a larger timeout. |
| Desktop QML smoke test | Passing | QML loads with `QT_QPA_PLATFORM=offscreen`. |
| Type checking | Passing | `mypy src/xpst` passes with a pragmatic baseline for dynamic third-party/Qt integrations. |
| Platform compliance | Mixed | YouTube uses the official API. Instagram, X, and TikTok rely on user-owned cookies/private or downloader flows and must be documented as user-risk integrations. |

## Release Gate

Every public release should pass these checks:

```bash
python -m pytest
ruff check src tests
mypy src/xpst
pip-audit
python -m build
QT_QPA_PLATFORM=offscreen python scripts/verify_qml_pages.py
xpst version --json
xpst health --json
```

Platform-specific artifacts should also be built and smoke-tested:

```bash
xpst build --json
```

## Security And Supply Chain

- Use PyPI Trusted Publishing from GitHub Actions instead of long-lived PyPI API tokens.
- Generate and attach a CycloneDX SBOM for every GitHub Release.
- Attach SHA-256 checksums for wheels, source distributions, and desktop installers/executables.
- Run `pip-audit` in CI and fail releases on known fixable vulnerabilities.
- Keep credentials local: OS keychain first, local file fallback only when keychain is unavailable.
- Never bundle user cookies, OAuth tokens, `client_secrets.json`, session files, or `~/.xpst` state into release artifacts.

## Licensing

xPST is dual licensed as `MIT OR Apache-2.0`. The desktop dependency PySide6/Qt is available under open-source and commercial licensing options; distributing a bundled desktop app means release notes should include Qt/PySide6 notices and LGPL compliance information.

The repository should keep:

- `LICENSE`
- `NOTICES.md`
- `LICENSING_REPORT.md`
- `docs/CODESIGNING.md`
- Dependency license inventory for direct runtime dependencies
- Release artifact notices for PyInstaller desktop bundles

## Platform Risk Posture

xPST should not claim that every platform integration is officially approved.

| Platform | Integration | Risk posture |
|---|---|---|
| YouTube | Official YouTube Data API v3 | Lowest platform risk, subject to Google/YouTube API policies and quotas. |
| Instagram | instagrapi/private API/session cookies | Higher risk; user must understand account and Terms risk. |
| X/Twitter | twikit/session cookies | Higher risk; X documentation strongly prefers official API use and prohibits scraping/browser automation for developer apps. |
| TikTok | yt-dlp/browser cookies for source/download workflows | Medium to high risk depending on usage; official TikTok developer APIs should be preferred for publish workflows when available. |

## Distribution Checklist

- Publish to PyPI with Trusted Publishing.
- Create a GitHub Release from a signed tag.
- Attach wheel, sdist, Windows executable, checksums, SBOM, and release notes.
- Code-sign Windows and macOS artifacts before broad distribution.
- Notarize macOS artifacts when distributing outside local development.
- Run a clean Windows install test from the built executable.
- Run a clean `pip install xpst` test after PyPI publication.
- Document which integrations are official and which use user-owned cookies/private flows.

## Enterprise Score

After the Windows pass and release-readiness cleanup, xPST is a strong beta-to-release candidate:

**9.1/10 for engineering readiness**

The main remaining gaps are operational rather than code-breaking:

- GitHub-hosted CI availability depends on repository/account billing and runner limits
- Trusted Publishing configured and exercised during the tagged release flow
- Optional artifact attestations for release assets
- Windows/macOS code signing
- Fresh external platform credential tests by the account owner
- macOS notarization for broad public distribution
