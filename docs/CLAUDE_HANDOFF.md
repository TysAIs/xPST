# Claude Handoff: xPST Ship-Readiness

This repo is the active xPST release-readiness worktree. The goal is to make xPST a fully agnostic, free, open-source cross-posting app that is safe enough to ship publicly after final platform and signing validation.

## Current State

- Branch: `main`
- Remote: `https://github.com/TysAIs/xPST.git`
- Current local platform: Windows
- Linux device is expected to continue validation and release hardening.
- Public release is not fully cleared yet. The local package and Windows app smoke paths are in good shape, but external release gates still require owner credentials, signing, macOS artifact proof, and live platform validation.

## Major Work Completed

- Provider-agnostic source/destination metadata was added through provider manifests and registry listing.
- CLI and desktop now expose provider/readiness/update/diagnostic surfaces.
- First-run readiness reports and repair flow were added for onboarding.
- Diagnostics bundle generation was added with redaction.
- State handling was hardened with locking/backups/migration support.
- Desktop posting flow was tightened so content is previewed before posting.
- Release tooling was expanded:
  - clean install smoke checks
  - public safety scan
  - release preflight
  - desktop package verification
  - Windows executable smoke check
  - macOS artifact verification
  - live platform smoke helper
  - release evidence/checksum/SBOM generation
- Windows executable was rebuilt locally and smoke-tested with a clean profile.
- Public preflight now blocks on live platform evidence, not just signing/macOS gaps.
- A safe package build wrapper was added at `scripts/build_package.py` because `python -m build` can be shadowed by the local `build/` directory on this worktree.
- A one-command public release evidence helper was started at `scripts/public_release_check.py`.

## Latest Known Good Verification

Recently passing checks from this worktree:

```bash
uv run pytest tests/test_release_artifacts.py tests/test_repo_assets.py tests/test_live_platform_smoke.py -q
uv run ruff check scripts/release_preflight.py scripts/release_artifacts.py scripts/verify_live_platforms.py tests/test_release_artifacts.py tests/test_repo_assets.py tests/test_live_platform_smoke.py
uv run mypy scripts/release_preflight.py scripts/release_artifacts.py scripts/verify_live_platforms.py
uv run python scripts/build_package.py
uv run python scripts/clean_install_smoke.py --dist dist --artifact both
uv run python scripts/release_preflight.py --json
uv run python scripts/release_preflight.py --public --json
uv run python scripts/verify_windows_exe.py --path dist/xPST.exe --seconds 5 --json --clean-profile
```

Important expected behavior:

- Local preflight should pass with warnings.
- Public preflight should fail until signing, macOS notarization, macOS artifact, and live platform evidence are present.
- Windows `--require-signed` check should fail locally unless a valid Authenticode signature has been applied.

## Current Dist Artifacts

The `dist/` directory has been rebuilt recently and should contain:

- `xpst-0.1.0-py3-none-any.whl`
- `xpst-0.1.0.tar.gz`
- `xPST.exe`

Before using them for any release, regenerate on the target machine:

```bash
python scripts/build_package.py
```

On Windows, rebuild the desktop executable with:

```powershell
uv run pyinstaller --clean --noconfirm build_windows.spec
```

## Public Release Gates Still Blocking

These are intentionally not solved in this Windows worktree:

1. Windows signing certificate and signing proof.
2. macOS Developer ID signing identity.
3. Apple notarization credentials.
4. macOS signed/notarized `.app` or DMG artifact.
5. Owner-approved live platform validation evidence.

Public preflight now expects live evidence:

```bash
python scripts/verify_live_platforms.py --require --json > release/live-platforms.json
python scripts/release_preflight.py --public --live-evidence release/live-platforms.json --json
```

The newer helper should do both:

```bash
python scripts/public_release_check.py --json
```

If that helper fails, inspect:

- `release/live-platforms.json`
- `release/public-preflight.json`

## Linux Continuation Checklist

Run these first on the Linux device:

```bash
python --version
python -m pip install --upgrade pip
python -m pip install -e ".[full,dev]" pip-audit pyinstaller
python scripts/build_package.py
python scripts/clean_install_smoke.py --dist dist --artifact both
python scripts/scan_public_safety.py --json
python scripts/release_preflight.py --json
pytest tests/ -q
ruff check src tests scripts
mypy src/xpst scripts/release_artifacts.py scripts/clean_install_smoke.py scripts/verify_desktop_package.py scripts/verify_windows_exe.py scripts/verify_macos_artifact.py scripts/verify_live_platforms.py scripts/scan_public_safety.py scripts/release_preflight.py scripts/public_release_check.py
```

If Linux has Docker, also run:

```bash
docker build -t xpst:linux-smoke .
docker run --rm xpst:linux-smoke version --json
```

For Qt/QML smoke on Linux:

```bash
QT_QPA_PLATFORM=offscreen python scripts/verify_desktop_package.py
QT_QPA_PLATFORM=offscreen python scripts/verify_qml_pages.py
```

## macOS Release Work

The macOS path is expected to run on macOS, not Linux:

```bash
bash scripts/verify_macos.sh
bash scripts/verify_macos.sh --public
```

Public mode requires:

- `MACOS_CODESIGN_IDENTITY`
- `APPLE_ID`
- `APPLE_TEAM_ID`
- `APPLE_APP_PASSWORD`

## Files Worth Reviewing Next

- `scripts/public_release_check.py`
- `scripts/release_preflight.py`
- `scripts/verify_live_platforms.py`
- `scripts/release_artifacts.py`
- `.github/workflows/release.yml`
- `docs/LAUNCH_CHECKLIST.md`
- `docs/CODESIGNING.md`
- `docs/SHIP_READINESS_PLAN.md`
- `src/xpst/desktop_app/backend.py`
- `src/xpst/desktop_app/qml/pages/ContentPage.qml`

## Suggested Next Actions For Claude

1. Run full Linux test/lint/type suite.
2. Fix any Linux-only path, packaging, or dependency failures.
3. Run Docker smoke if Docker is available.
4. Validate `scripts/public_release_check.py` end to end with mocked or owner-provided live credentials.
5. Review `.github/workflows/release.yml` after the new helper was added; consider whether tag releases should require uploaded live evidence as an artifact or keep it owner-local.
6. On macOS, run `scripts/verify_macos.sh --public` with real Developer ID and Apple credentials.
7. On Windows, sign `dist/xPST.exe`, then rerun:

```powershell
python scripts/verify_windows_exe.py --path dist/xPST.exe --seconds 12 --json --clean-profile --require-signed
```

## Notes And Caveats

- Do not mark this release complete just because tests pass. Public launch requires real signing/notarization/live-account evidence.
- Do not commit credentials, cookies, sessions, OAuth tokens, state files, or release evidence that contains private account details.
- `release/live-platforms.json` should be reviewed before sharing. It is designed to be structural evidence, but account-specific details may still appear in health-check output.
- The repo has many accumulated changes. Avoid broad rewrites unless a specific gate fails.
- Prefer adding narrow tests around release gates and platform-edge behavior.

