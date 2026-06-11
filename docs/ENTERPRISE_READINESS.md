# Enterprise Readiness

> **SUPERSEDED — historical snapshot (2026-06-10).**
> Canonical status lives in [ISA.md](../ISA.md) (criteria) and [docs/XPST-NORTH-STAR.md](XPST-NORTH-STAR.md) (current-vs-ideal). Numbers below are stale (e.g. the test count and the readiness score) and must not be cited. This file is kept for historical context only.

xPST is designed to remain free, open source, local-first, and user-controlled while still being packaged like a professional desktop app, CLI, and MCP server. This document defines the release bar for that goal.

## Current Status

| Area | Status | Notes |
|---|---:|---|
| Core tests | Passing | 866 passing, 6 skipped on Windows after the provider/MCP/diagnostics/clean-install/repo-assets/release-evidence/security-doc/license/readiness UX/post-preview/clean-profile/macOS public-gate pass. |
| Lint | Passing | `ruff check src tests` passes. Qt/QML bridge names are explicitly ignored where camelCase is required by QML. |
| Vulnerability audit | Required for release | Run `pip-audit` in CI and before public release; do not rely on stale local results. |
| Python package | Passing | Source distribution and wheel build successfully. |
| Clean install smoke | Passing | Built wheel and source distribution install into fresh virtual environments and run version, providers, update metadata, readiness, and diagnostics commands. |
| Release metadata | Passing | Checksums, PyPI metadata, release notes, SBOM, release evidence, artifact attestations, and release legal documents are generated. |
| Docker build smoke | CI-gated | CI builds `xpst:ci` and runs `xpst version --json` in the container. Local Docker was not available on this Windows workstation. |
| Windows executable | Mostly passing | Direct PyInstaller build produced `dist/xPST.exe`; packaged launch smoke stayed alive locally with an isolated clean profile; public release still needs signing. |
| Desktop QML smoke test | Passing | QML loads with `QT_QPA_PLATFORM=offscreen`. |
| Type checking | Passing | `mypy src/xpst` passes with a pragmatic baseline for dynamic third-party/Qt integrations. |
| Platform compliance | Mixed | YouTube uses the official API. Instagram, X, and TikTok rely on user-owned cookies/private or downloader flows and must be documented as user-risk integrations. |

## Release Gate

Every public release should pass these checks:

```bash
python -m pytest
ruff check src tests
mypy src/xpst
python scripts/verify_qml_pages.py
pip-audit
python scripts/build_package.py
python scripts/clean_install_smoke.py --dist dist --artifact both
python scripts/release_artifacts.py --dist dist --output-dir release --skip-checks
xpst version --json
xpst diagnostics --json
xpst health --json
docker build -t xpst:ci .
docker run --rm xpst:ci version --json
```

Platform-specific artifacts should also be built and smoke-tested:

```bash
xpst build --json
```

## Security And Supply Chain

- Use PyPI Trusted Publishing from GitHub Actions instead of long-lived PyPI API tokens.
- Generate GitHub artifact attestations for every Python, Windows, and macOS release bundle.
- Generate and attach a CycloneDX SBOM for every GitHub Release.
- Generate and attach `RELEASE_EVIDENCE.json` for every release candidate.
- Attach SHA-256 checksums for wheels, source distributions, and desktop installers/executables.
- Generate release metadata with `python scripts/release_artifacts.py --dist dist --output-dir release`; it supports Python packages and desktop-only artifact folders.
- Run `pip-audit` in CI and fail releases on known fixable vulnerabilities.
- Keep credentials local: OS keychain first, encrypted `.enc` file fallback only when keychain is unavailable.
- Never bundle user cookies, OAuth tokens, `client_secrets.json`, session files, or `~/.xpst` state into release artifacts.

## Licensing

xPST is dual licensed as `MIT OR Apache-2.0`. The desktop dependency PySide6/Qt is available under open-source and commercial licensing options; distributing a bundled desktop app means release notes should include Qt/PySide6 notices and LGPL compliance information.

The repository should keep:

- `LICENSE`
- `NOTICES.md`
- `LICENSING_REPORT.md`
- `docs/CODESIGNING.md`
- Dependency license inventory for direct runtime dependencies, plus a generated transitive report for each release environment
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

**8.9/10 for engineering readiness**

The main remaining gaps are operational rather than code-breaking:

- Successful tag-run proof of the CI release workflow with Trusted Publishing and artifact attestations
- Windows/macOS code signing
- Fresh external platform credential tests by the account owner
- Gradually tightening the mypy baseline
- Clearer UI polish pass on the desktop app
