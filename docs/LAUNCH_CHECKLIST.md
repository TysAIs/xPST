# 🚀 GitHub Launch Checklist

Use this checklist before making XPST public on GitHub.

## Pre-Launch Verification

- [ ] All tests pass (`pytest tests/ -v`)
- [ ] Lint clean (`ruff check src/ tests/`)
- [ ] Type checks pass (`mypy src/xpst/ --ignore-missing-imports`)
- [ ] README complete with badges, disclaimer, installation, commands
- [ ] LICENSE correct (MIT/Apache-2.0 dual license)
- [ ] CONTRIBUTING.md present with guidelines
- [ ] CHANGELOG.md present with version history
- [ ] SECURITY.md present with vulnerability reporting policy
- [ ] NOTICES.md present with dependency licenses
- [ ] No personal info (API keys, usernames, tokens) in repo
- [ ] No credentials in repo (`.gitignore` covers all secret patterns)
- [ ] `.gitignore` is comprehensive (credentials, state, sessions, env files)

## Infrastructure

- [ ] Docker builds successfully (`docker build -t xpst .`)
- [ ] CI/CD works (GitHub Actions tests pass)
- [ ] PyPI ready (`pyproject.toml` metadata correct, builds with `python -m build`)

## Documentation

- [ ] docs/ROADMAP_V2.md present
- [ ] docs/COMPETITIVE_ANALYSIS.md present
- [ ] docs/LAUNCH_CHECKLIST.md present (this file)
- [ ] All CLI commands documented in README
- [ ] Screenshots/GIFs added (or placeholders noted)

## Post-Launch

- [ ] Create GitHub release with tag
- [ ] Announce on social channels
- [ ] Monitor issues for first 48 hours
