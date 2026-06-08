# GitHub Launch Checklist

Use this checklist before making xPST public on GitHub.

## Pre-Launch Verification

- [ ] All tests pass (`pytest tests/ -v`)
- [ ] Lint clean (`ruff check src tests`)
- [ ] Type checks pass (`mypy src/xpst`)
- [ ] Vulnerability audit passes (`pip-audit`)
- [ ] README complete with badges, disclaimer, installation, commands
- [ ] LICENSE correct (`MIT OR Apache-2.0`)
- [ ] CONTRIBUTING.md present with guidelines
- [ ] CHANGELOG.md present with version history
- [ ] SECURITY.md present with vulnerability reporting policy
- [ ] NOTICES.md present with dependency licenses
- [ ] No personal info, API keys, usernames, tokens, cookies, or sessions in repo
- [ ] `.gitignore` covers credentials, state, sessions, env files, and runtime data

## Infrastructure

- [ ] Docker builds successfully (`docker build -t xpst .`)
- [ ] CI passes on Linux, macOS, and Windows
- [ ] PyPI Trusted Publishing configured for `.github/workflows/release.yml`
- [ ] PyPI metadata builds cleanly (`python -m build`)
- [ ] Release artifacts include `SHA256SUMS`
- [ ] Release artifacts include `xpst-sbom.cdx.json`
- [ ] Windows executable is built and attached to GitHub Release
- [ ] macOS `.app`/DMG is built and attached to GitHub Release
- [ ] Windows/macOS artifacts are code signed for broad distribution
- [ ] macOS artifacts are notarized before public distribution outside local development

## Documentation

- [ ] docs/ROADMAP_V2.md present
- [ ] docs/COMPETITIVE_ANALYSIS.md present
- [ ] docs/LAUNCH_CHECKLIST.md present
- [ ] docs/ENTERPRISE_READINESS.md present
- [ ] docs/PRIVACY.md present
- [ ] docs/CODESIGNING.md present
- [ ] docs/MACOS_HERMES_VALIDATION.md present
- [ ] All CLI commands documented in README
- [ ] Platform risk posture documented honestly
- [ ] Screenshots/GIFs added or placeholders noted

## Post-Launch

- [ ] Create GitHub release from signed tag
- [ ] Confirm PyPI install works (`pip install xpst`)
- [ ] Confirm executable download works on Windows
- [ ] Confirm `.app`/DMG opens on macOS
- [ ] Monitor issues for first 48 hours
