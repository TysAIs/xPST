# xPST Production Readiness — Fix Playbook + Fleet Plan

Verified against commit `ba96505` (v0.1.0-rc released, CI run 27372909227 all green, 15 assets published) on 2026-06-12. Every status below was re-verified by command on a Linux checkout, not carried forward from prior session claims.

Companion docs: `docs/CLAUDE_HANDOFF.md` (release-gate background), `docs/OWNER-SMOKE-CHECKLISTS.md` (manual mac/win protocol), `ISA.md` (full criteria ledger, 167/195), `docs/SHIP_READINESS_PLAN.md`, `docs/LAUNCH_CHECKLIST.md`.

---

## 1. Reality check (corrections to the previous handoff summary)

A prior session reported several items as committed. Re-verification shows:

| Claim | Actual state (verified 2026-06-12) |
|---|---|
| `HANDOFF_FIXES.md` committed | Did not exist in git history or working tree. This file replaces it. |
| Blocker 3 fixed (`health`/`run --dry-run` JSON on missing FFmpeg) | NOT in source. `rg 'ffmpeg' src/xpst/cli.py` → 0 hits; no guard at the CLI boundary. `xpst health --json` works on a machine WITH ffmpeg; the missing-ffmpeg path is unguarded. Treat as OPEN. |
| `kb doctor --json` works | FALSE. `xpst kb doctor --json` → `Error: No such option '--json'`. Treat as OPEN. |
| graphify-out fresh @ ba96505 | TRUE. |
| 1239 passed / 4 skipped | Plausible (last verified suite: 1242 in CI matrix) but re-run before relying on it. |

Rule for all lanes: no item is "done" without the verification command output pasted into the PR/commit body.

---

## 2. Fleet plan — three machines, three lanes

| Lane | Machine | Agent | Role |
|---|---|---|---|
| W | Windows workstation | Codex | PRIMARY CODE LANE. All code fixes below (B1–B4, NB6–NB8). Only machine that can both edit and visually verify QML today. Windows smoke checklist. |
| M | macOS workstation | Nemotron 3 Ultra | VERIFICATION LANE. Real-world no-FFmpeg testing (B3), DMG/`verify_macos.sh`, macOS half of `docs/OWNER-SMOKE-CHECKLISTS.md`, notarization when Apple creds exist. |
| L | Linux box | Claude | REVIEW + GATE LANE. PR review, full local gates (pytest/ruff/mypy/import-linter/pip-audit), released-artifact verification, final preflight. Cannot render QML (no PySide6 aarch64 wheel) — never assign desktop visual checks here. |
| — | Owner | human | Unblockers only: live platform creds (B5), signing certs, PyPI trusted publisher, final sign-offs. |

Sync protocol: Codex branches as `codex/fix-<id>` → PR to `main`. Every PR body includes the issue ID, the verification command, and its output. Linux lane reviews and merges. Keep all gates green at every merge (ISA constraint).

### Execution order

1. **Wave 1 — release infra (Codex):** B1 + B2. Then tag `v0.1.0-rc2` and verify the release is self-consistent (complete checksums, prerelease flag). Everything else rides on a trustworthy release train.
2. **Wave 2 — product fixes (Codex, parallel):** B3 + NB7 + NB8 (CLI/core, headless-testable) and B4 (desktop, visually verified on Windows). NB6 anytime.
3. **Wave 3 — macOS verification (Nemotron):** rc2 DMG smoke, B3 no-ffmpeg behavior, macOS smoke checklist.
4. **Wave 4 — owner gates:** live platform evidence (B5), signing decision, PyPI trusted publisher.
5. **Wave 5 — ship (Linux lane):** full gate run + `release_preflight.py --public --live-evidence ...` → tag `v0.1.0`.

---

## 3. Blockers

### B1 — Release checksum aggregation is broken (VERIFIED, evidence below)

**Evidence:** `gh release download v0.1.0-rc -p SHA256SUMS -O -` returns ONE line (the Linux `xPST` binary). 14 of 15 assets, including `xPST.exe`, `xPST.dmg`, the wheel and sdist, have no published checksum.

**Root cause:** all four build jobs write identical filenames `SHA256SUMS`/`SHA512SUMS` (`scripts/release_artifacts.py:428-429`); the `github-release` job downloads with `merge-multiple: true` (`.github/workflows/release.yml:214-217`), so the last-downloaded artifact's checksum files silently overwrite the others.

**Fix (recommended — compute once in the aggregator):**
1. In each per-OS `release_artifacts.py` invocation, stop shipping `SHA256SUMS`/`SHA512SUMS` into the uploaded artifact (keep them as job-local evidence if desired, but exclude from the `upload-artifact` path or rename to `SHA256SUMS.<os>`).
2. In the `github-release` job, after download and before the softprops step, add:
   ```yaml
   - name: Aggregate checksums
     run: |
       cd release-artifacts
       rm -f SHA256SUMS SHA512SUMS
       sha256sum * > SHA256SUMS
       sha512sum * > SHA512SUMS
   ```
3. Update the expected-files list in `scripts/release_artifacts.py:233-234` if it gates on those names.

**Verify:** tag `v0.1.0-rc2`; then `gh release download v0.1.0-rc2 -p 'SHA256SUMS'` — line count must equal the number of non-checksum assets; spot-check `sha256sum -c` against a downloaded asset.

### B2 — RC is not marked prerelease

**Root cause:** the softprops step (`.github/workflows/release.yml:219-223`) has no `prerelease` input, so `v0.1.0-rc` displays as "Latest" to the public.

**Fix (one line in the `Create GitHub Release` step):**
```yaml
prerelease: ${{ contains(github.ref_name, '-rc') }}
```
This also future-proofs `v0.1.0` (evaluates false). Full signing infra remains a separate owner-gated track (`docs/CODESIGNING.md`); do not block rc2 on it — owner already approved shipping unsigned RC with SmartScreen/Gatekeeper notes.

**Verify:** `gh release view v0.1.0-rc2 --json isPrerelease` → `true`.

### B3 — `health` and `run --dry-run` traceback when FFmpeg is missing

**Status:** OPEN (prior "fixed" claim was false — see §1). FFmpeg detection exists only in `src/xpst/readiness.py`; nothing guards the `health`/`run` CLI paths (`src/xpst/cli.py` — `run` at lines 240-272).

**Fix pattern:**
1. Add a small guard helper (suggested home: `src/xpst/utils/platform.py`, shared with NB8): `ensure_ffmpeg() -> str | None` using `shutil.which("ffmpeg")`.
2. At the top of `health` and `run` command bodies, when ffmpeg is absent: with `--json`, print `{"ok": false, "error": "ffmpeg_not_found", "remedy": "install ffmpeg and ensure it is on PATH"}` and `sys.exit(3)`; without `--json`, a one-line human message. Never let the engine-init traceback escape.
3. Decide intentionally whether `run --dry-run` should still list pending posts without ffmpeg (it does no encoding) — if yes, only warn; if no, same structured error. Document the choice in the command help.

**Tests (headless, any OS):** new `tests/test_cli_no_ffmpeg.py` using `CliRunner` with `shutil.which` monkeypatched to `None`: `health --json` and `run --dry-run --json` must produce parseable JSON, no traceback, expected exit codes.

**Real-world verify (Lane M):** macOS without brew ffmpeg → both commands behave per spec.

### B4 — "Schedule New" button is a dead toast

**Status:** OPEN. `src/xpst/desktop_app/qml/pages/SchedulePage.qml:159` defines the `+ Schedule New` button; line 173 fires `showToast("Schedule New - coming soon", false)`. `DesktopBackend` (`src/xpst/desktop_app/backend.py`, 26 `@Slot`s) has no schedule-create slot.

**Fix (reuse existing machinery — do NOT write new scheduling logic):** `src/xpst/schedule_manager.py` and the 16 MCP scheduling tools already implement create/list/cancel. Mirror that API:
1. Backend: add `@Slot` `scheduleNew(content_path, caption, when_iso, platforms) -> bool` (match the MCP create-tool's parameter semantics), call the same `schedule_manager` entry point, emit a signal to refresh the schedule list model, surface errors via the existing toast/error channel.
2. QML: replace the toast with a dialog — datetime picker, platform multi-select, content selector — calling the new slot; on success refresh the list.

**Verify:** backend slot gets a headless unit test (create → appears in `schedule list`; bad input → clean error). UI path verified on Windows (Lane W): create via dialog → row appears in SchedulePage AND `uv run xpst schedule list --json` shows it.

### B5 — Live platform smoke evidence (owner-gated)

**Status:** blocked on real account credentials; scripts exist (`scripts/verify_live_platforms.py`, 135 lines; `scripts/public_release_check.py`).

**When creds are available (owner machine, NOT CI):**
```bash
python scripts/verify_live_platforms.py --require --json > release/live-platforms.json
python scripts/release_preflight.py --public --live-evidence release/live-platforms.json --json
```
Extend `verify_live_platforms.py` per-platform as gaps appear. **Review `release/live-platforms.json` before sharing and never commit raw evidence** — health output can embed account specifics (warning already in `docs/CLAUDE_HANDOFF.md`).

---

## 4. Non-blockers

### NB6 — README leads with source install

`README.md:130` says "Install from source (recommended today)" with `git clone` first. The release now publishes binaries + wheel. Reorder: (1) download platform binary from Releases (link checksums once B1 lands), (2) `pip install` the wheel from the release URL — note PyPI name pending trusted-publisher setup, (3) source install for contributors. Keep the SmartScreen/Gatekeeper unsigned-RC notes adjacent to the binary instructions.

### NB7 — `kb` subcommands lack `--json`

**Status:** OPEN (prior claim false). Working pattern to copy: `kb query` (`src/xpst/knowledge/cli_kb.py:81-94` — `--json` flag + `console.print_json`). Apply to:
- `kb doctor` (line 256)
- `kb areas` (line 202)
- `kb course` (line 223)

**Verify:** each `--json` output parses via `json.loads`; add `CliRunner` tests alongside existing kb CLI tests. Agent-equal citizenship is an ISA principle — every human-readable output needs a machine-readable twin.

### NB8 — Config dir resolution scattered across 10 files

`~/.xpst` / `Path.home()` is hardcoded in `desktop.py`, `analytics_store.py`, `i18n.py`, `analytics.py`, `cli.py`, `state.py`, `setup.py`, `config.py`, `config_migration.py`, `schedule_manager.py` (14 `Path.home()` sites; per-OS split visible at `config_migration.py:36`).

**Fix:** create `get_config_dir()` in `src/xpst/utils/platform.py`: honor `XPST_CONFIG_DIR` env override first, then per-OS default (Windows `%APPDATA%/xPST`, else `~/.xpst` — preserve exactly the migration semantics in `config_migration.py`). Migrate all call sites. This also unlocks clean test isolation (`XPST_CONFIG_DIR=tmpdir`) and portable installs.

**Verify:** `rg -n '\.xpst' src/` hits only `utils/platform.py` (+ docs); full suite green; `XPST_CONFIG_DIR=/tmp/xpst-test uv run xpst health --json` touches nothing under `~/.xpst`.

---

## 5. Definition of production-ready (v0.1.0 exit criteria)

1. B1–B4 merged with pasted verification output; NB6–NB8 merged.
2. `v0.1.0-rc2` published: complete SHA256SUMS/SHA512SUMS covering every asset, `isPrerelease: true`.
3. macOS + Windows halves of `docs/OWNER-SMOKE-CHECKLISTS.md` signed off on rc2 artifacts.
4. Live platform evidence captured and `release_preflight.py --public --live-evidence ... --json` passes (signing items resolved per owner decision: unsigned-with-notes or real certs).
5. Full gates green at the tag SHA: pytest (0 failed), ruff, mypy, import-linter, pip-audit.
6. Tag `v0.1.0`; release notes generated; README install section matches reality (NB6).
7. Post-ship (non-gating roadmap): PyPI trusted publisher, Authenticode + Apple notarization, KnowledgePage.qml (explicit v1 descope).

## 6. Refresh the architecture map after code changes

```bash
graphify update . --no-viz   # updates graphify-out/ in place, AST-only
```
