# GitHub Launch Checklist

Use this checklist before making xPST public on GitHub.

## Pre-Launch Verification

- [ ] All tests pass (`pytest tests/ -v`)
- [ ] Lint clean (`ruff check src tests`)
- [ ] Type checks pass (`mypy src/xpst`)
- [ ] Vulnerability audit passes (`pip-audit`)
- [ ] README complete with badges, disclaimer, installation, commands
- [ ] LICENSE correct (`MIT OR Apache-2.0`)
- [ ] License metadata is consistent across `pyproject.toml`, `LICENSE`, README, NOTICES, and licensing report
- [ ] CONTRIBUTING.md present with guidelines
- [ ] CHANGELOG.md present with version history
- [ ] SECURITY.md present with vulnerability reporting policy
- [ ] NOTICES.md and LICENSING_REPORT.md present with dependency license posture
- [ ] Credential docs match OS keychain plus encrypted `.enc` fallback behavior
- [ ] No personal info, API keys, usernames, tokens, cookies, or sessions in repo
- [ ] Public safety scan passes (`python scripts/scan_public_safety.py --json`)
- [ ] `.gitignore` covers credentials, state, sessions, env files, and runtime data
- [ ] `.dockerignore` excludes credentials, state, env files, build output, and runtime data

## Infrastructure

- [ ] Docker builds successfully (`docker build -t xpst .`)
- [ ] CI passes on Linux, macOS, and Windows
- [ ] PyPI Trusted Publishing configured for `.github/workflows/release.yml`
- [ ] PyPI metadata builds cleanly (`python scripts/build_package.py`)
- [ ] Local release preflight passes (`python scripts/release_preflight.py --json`)
- [ ] Public owner release evidence passes (`python scripts/public_release_check.py --json`)
- [ ] Built wheel and sdist pass clean-install smoke (`python scripts/clean_install_smoke.py --dist dist --artifact both`)
- [ ] Desktop package static checks pass (`python scripts/verify_desktop_package.py`)
- [ ] Windows executable launch smoke passes locally (`python scripts/verify_windows_exe.py --path dist/xPST.exe --json --clean-profile`)
- [ ] Windows public-release smoke passes with signing required (`python scripts/verify_windows_exe.py --path dist/xPST.exe --json --clean-profile --require-signed`)
- [ ] macOS app artifact verification passes (`python scripts/verify_macos_artifact.py --app dist/xPST.app --json`)
- [ ] macOS public-release validation passes with Developer ID signing and notarization (`bash scripts/verify_macos.sh --public`)
- [ ] Release metadata script succeeds (`python scripts/release_artifacts.py --dist dist --output-dir release --skip-checks`)
- [ ] Release artifacts include `SHA256SUMS`
- [ ] Release artifacts include `SHA512SUMS`
- [ ] Release artifacts include `xpst-sbom.cdx.json`
- [ ] Release artifacts include `RELEASE_EVIDENCE.json`
- [ ] Release artifacts include generated transitive dependency license report
- [ ] Redacted diagnostics bundle works (`xpst diagnostics --json`)
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
- [ ] GitHub issue templates present for platform breakage, install failures, and provider requests
- [ ] All CLI commands documented in README
- [ ] Platform risk posture documented honestly
- [ ] README screenshots are present and validated by repo asset tests

## Post-Launch

- [ ] Create GitHub release from signed tag
- [ ] Confirm PyPI install works (`pip install xpst`)
- [ ] Confirm executable download works on Windows
- [ ] Confirm `.app`/DMG opens on macOS
- [ ] Run one owner-approved no-surprise upload/delete or private-draft workflow per enabled platform
- [ ] Monitor issues for first 48 hours
