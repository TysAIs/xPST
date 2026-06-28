# Release Checklist

Pre-release verification steps for cutting an xPST release. Run every item
before tagging a release. Items are grouped by phase; the release is not
shippable until every box in **Gate** and **Artifacts** is checked.

---

## 1. Pre-flight (local)

- [ ] Working tree is clean: `git status` shows nothing to commit
- [ ] On the release branch (`main` or a dedicated `release/*` branch)
- [ ] `CHANGELOG.md` updated with user-visible changes for this version
- [ ] Version bumped in `src/xpst/__init__.py` (or `__version__`) and
      `pyproject.toml`
- [ ] License metadata consistent across `pyproject.toml`, `LICENSE`,
      `NOTICES.md`, `LICENSING_REPORT.md`, and README

## 2. Quality gates

- [ ] `python -m pytest` — full suite green
- [ ] `ruff check src tests` — lint clean
- [ ] `mypy src/xpst` — type checks pass
- [ ] `lint-imports` — architectural import walls hold
- [ ] `pip-audit` — no known vulnerabilities in dependencies
- [ ] `python scripts/scan_public_safety.py --json` — no secrets / PII in repo

## 3. Build & smoke

- [ ] `python scripts/build_package.py` — wheel + sdist build cleanly
- [ ] `python scripts/release_preflight.py --json` — local preflight passes
- [ ] `python scripts/clean_install_smoke.py --dist dist --artifact both` —
      fresh-install of wheel and sdist succeeds in a clean venv
- [ ] `python scripts/verify_desktop_package.py` — desktop package static checks
- [ ] `QT_QPA_PLATFORM=offscreen python scripts/verify_qml_pages.py` — all
      QML pages load
- [ ] CLI smoke: `xpst version --json` and `xpst health --json` succeed from a
      clean install
- [ ] `xpst diagnostics --json` produces a redacted bundle (no secrets leak)

## 4. Platform artifacts

- [ ] macOS: `bash scripts/verify_macos.sh` (and `--public` for signed +
      notarized public release)
- [ ] macOS: `python scripts/verify_macos_artifact.py --app dist/xPST.app --json`
- [ ] Windows: `python scripts/verify_windows_exe.py --path dist/xPST.exe --json --clean-profile`
- [ ] Windows public release: same with `--require-signed`
- [ ] Docker: `docker build -t xpst:ci .` and `docker run --rm xpst:ci version --json`

## 5. Release artifacts (Gate — must all be present)

- [ ] `SHA256SUMS` and `SHA512SUMS` generated and verified
- [ ] `xpst-sbom.cdx.json` (CycloneDX SBOM) included
- [ ] `RELEASE_EVIDENCE.json` included
- [ ] Transitive dependency license report included
- [ ] Release notes attached to the GitHub Release
- [ ] `python scripts/release_artifacts.py --dist dist --output-dir release --skip-checks`
      succeeds

## 6. Publish

- [ ] Tag pushed: `git tag vX.Y.Z` (signed) and pushed to `origin`
- [ ] `.github/workflows/release.yml` triggered by the tag and completes green
- [ ] PyPI publish succeeds (Trusted Publishing configured) — `pip install xpst`
      works from a clean environment
- [ ] GitHub Release published with all artifacts attached
- [ ] macOS artifacts signed with Developer ID and notarized
- [ ] Windows executable signed for broad distribution

## 7. Post-release verification

- [ ] `pip install xpst` works on a clean machine
- [ ] Downloaded macOS `.app`/DMG opens
- [ ] Downloaded Windows executable launches
- [ ] One owner-approved no-surprise workflow per enabled platform
      (dry-run first, then a private-draft upload/delete)
- [ ] `xpst update --check` reports the new version as current
- [ ] Monitor GitHub Issues for 48 hours for regressions

## Rollback plan

If a critical issue is found after publish:

1. Yank the broken wheel from PyPI (`pip uninstall` still works; new installs
   get the previous version).
2. Mark the GitHub Release as a pre-release / draft and pin a known-good
   release notes section noting the retraction.
3. Tag a patch release (`vX.Y.Z+1`) following this checklist again.
