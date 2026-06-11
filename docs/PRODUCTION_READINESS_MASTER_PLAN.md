# xPST Production-Readiness & Enterprise Master Plan

**Date:** 2026-06-10
**Source:** 7-lane parallel code audit (macOS launch bug, UI/UX, branding, cross-platform, enterprise/prod, KB Phase 2-5, test strategy). Every claim below is grounded in real `file:line` from that audit.

## The honest truth about current state

xPST is genuinely well built out, but it is **not yet production-ready and the "enterprise-ready" claims in the docs are not reproducible.** Concretely:

- The test suite does **not** pass clean. 26 tests fail and 2 modules cannot even be collected. The `ENTERPRISE_READINESS.md` "8.9/10, 866 passing" claim is currently false. (Most failures are an environment/test-hygiene issue, not deep bugs — see W0.)
- There are **two real security holes**: the credential "encryption" derives its key from a world-readable machine-id with no KDF, and the plugin loader auto-`pip install`s dependencies from any dropped plugin file (remote code execution) while its sandbox runs *after* the code executes.
- The product ships **three different logo concepts** simultaneously, and the primary mark is a bare "X" that reads as the Twitter/X logo — i.e. one of the platforms it posts *to*.
- **Linux has no desktop release lane** (Win/mac do). Desktop bundles don't resolve resource paths when frozen and don't bundle ffmpeg.
- The desktop UI mixes Material-purple native controls with hand-rolled Apple-blue widgets, has no icon font (icons render as empty boxes), and a corrupted glyph in the sidebar.

None of this is fatal. It's a finite, well-understood punch list. This plan sequences it.

## Status

- ✅ Graphify knowledge-graph map of the repo (branch `graphify-map`)
- ✅ KB Phase 1 (ingest → faster-whisper → nugget → JSON store → `xpst kb add/query`), 18 tests green
- ✅ **macOS launch "giant X" bug — FIXED** (`fix(desktop): bound launch splash size`). Splash now bounded to ≤480×360 and closed on window-show. *Owner to confirm by rebuilding the `.app`.*
- ⬜ Everything below

---

## Workstreams (sequenced)

### W0 — Make the test suite honest + CI green  ⟵ DO FIRST (blocker)
"Enterprise-worthy" starts with a suite that actually passes. Root causes are precise (enterprise lane):

| Fix | Detail | Files |
|---|---|---|
| Stub ffmpeg in tests | 23 of 26 failures: `engine.py` builds `VideoProcessor` → `_verify_ffmpeg` raises because ffmpeg is absent. Patch it in `conftest.py`. | `tests/conftest.py`, `src/xpst/utils/video.py` |
| Import-skip optional extras | `test_desktop_backend.py` (PySide6) and `test_mcp_server.py` (mcp) hard-fail collection. Add `pytest.importorskip`. | those two test files |
| `test_stress` source/test drift | `VideoProcessor` empty-string ffmpeg default mismatch | `tests/test_stress.py`, `src/xpst/utils/video.py` |
| `dockerignore` | Test requires it; file is absent AND gitignored so it can never be committed. | `.gitignore`, `Dockerfile`, `tests/test_repo_assets.py` |
| `release_artifacts` drift | Generator emits different strings than the test asserts | `scripts/release_artifacts.py` |
| CI matrix | Confirm mac/win/linux × Python matrix runs the green suite | `.github/workflows/ci.yml` |

**Acceptance:** `pytest` green (or every skip/quarantine has a documented reason); CI green on all three OSes.

### W1 — macOS launch bug  ✅ DONE
Splash sizing math extracted to `splash_sizing.py` (Qt-free, unit-tested), bounded to ≤480×360, `splash.finish()` + 120ms fallback. **Remaining:** the owner rebuilds `.app` to confirm visually (can't be reproduced on Linux). Follow-up (branding, W5): the splash still shows the "X" image — what it shows is a brand decision.

### W2 — Security & enterprise hardening
| Sev | Issue | Fix | Files |
|---|---|---|---|
| HIGH | Weak Fernet key (world-readable machine-id, no KDF; plaintext fallback) | Per-install random secret + scrypt/PBKDF2 + stored salt, 0600, refuse plaintext | `src/xpst/utils/credentials.py`, `SECURITY.md` |
| HIGH | Plugin loader = RCE (auto pip-install from any plugin; sandbox runs after exec) | Default no-deps true, never auto-install, drop the false isolation claim or build a real boundary | `src/xpst/plugins/__init__.py` |
| MEDIUM | No `dockerignore`, image runs as root | Commit dockerignore, add non-root `USER` | `.gitignore`, `Dockerfile` |
| MEDIUM | Deps floored not pinned; pip-audit/SBOM aspirational; faster-whisper missing from NOTICES | Install from lockfile in CI; blocking pip-audit + SBOM; add attribution | `pyproject.toml`, `uv.lock`, `Dockerfile`, `NOTICES.md` |
| LOW | `datetime.utcnow` (54k deprecation warnings) | timezone-aware `datetime.now(UTC)` | `src/xpst/state_manager.py`, `src/xpst/state.py` |
| LOW | Optional imports fail hard instead of degrading | try/except + install-the-extra message | `src/xpst/mcp/*`, `src/xpst/desktop_app/backend.py` |

### W3 — Cross-platform agnosticism (macOS / Windows / Linux)
- **Frozen resource paths (HIGH):** no `sys._MEIPASS` handling anywhere → packaged apps can't find bundled assets/QML. Add a `resource_path()` helper, route splash/tray/QML lookups through it. `src/xpst/desktop_app/main.py`.
- **Linux desktop release lane (HIGH):** add a `build-linux` job (`build.sh linux` → AppImage/tarball + checksums + attestation) and `scripts/verify_linux_binary.py` mirroring the Windows/mac verify scripts. `.github/workflows/release.yml`.
- **ffmpeg not bundled (MEDIUM):** desktop bundles depend on a system ffmpeg. Bundle a static ffmpeg per platform OR add a clean first-run "ffmpeg missing" dialog instead of a raw `RuntimeError`. `build_*.spec`, `src/xpst/utils/video.py`.
- **`get_config_dir()` dead abstraction (LOW):** 89 hardcoded `~/.xpst` literals bypass it; either route through it (so Windows uses `%APPDATA%`) or delete it. De-Mac the scheduler log strings.
- **Linux crontab/credential edge cases (MEDIUM):** expand `~` before writing cron lines, detect missing `crontab`, salt the Fernet key. `src/xpst/cli.py`, `credentials.py`.
- **Icon sourcing (LOW):** mac icon sourced from `docs/`, win from `assets/`; unify under `assets/` and hard-fail if missing.

### W4 — UI/UX polish & design system
The "wacko" look has concrete causes:
- **#1 driver:** Material style is set globally (`main.py:230`) but no control is themed, so Material-purple `ComboBox/Switch/Slider/CheckBox/BusyIndicator/ProgressBar` render beside Apple-blue custom widgets. → bind `Material.accent/Material.theme` to `ThemeProvider`. (HIGH)
- **No icon font** anywhere → emoji + geometric glyphs + ASCII words ("OK"/"Edit"/"Web") used as icons, many render as tofu boxes. → bundle Material Symbols / lucide, one `Icons.qml`. (HIGH)
- **Corrupted U+FFFD glyph** as the Analytics sidebar icon (`Sidebar.qml:63`). (HIGH)
- **Duplicate theme systems:** dead `Theme.qml` singleton vs live Python `ThemeProvider`; pages reference `theme.iconYouTube` etc. that don't exist on the live object → blank icons. Pick one source of truth. (HIGH)
- **Platform-only font** (Segoe UI / Cascadia Mono) never applied → metric drift on mac/Linux. Set a platform-aware default font. (MEDIUM)
- **Literal bugs:** `DashboardPage.qml` pill `opacity:5.0` + `color: parent.color` (washed-out text); `main.qml` custom ProgressBar fill bound via a broken `parent.parent.parent` chain → always 0. (MEDIUM)
- **Token drift:** identical `radiusLg/radiusXl`, button heights 26/28/30/32/34/36/40, mixed paddings → define a control-height + spacing scale. (MEDIUM)
- **Incomplete dark mode:** hardcoded `Qt.rgba()` tints + `#ffffff` text. (LOW)
- Extract shared components (`PlatformChip`, `AppButton`, `MetricCard`). (MEDIUM)

### W5 — Branding & icon system  ⟵ NEEDS OWNER'S TASTE
- **One mark, not three.** Today: white-X-on-black (tray), glowing-X-on-navy (splash/win), browser-windows (mac/README). The bare "X" collides with the X/Twitter destination. Pick ONE concept that depicts **one-to-many distribution** (hub/fan-out/broadcast), explicitly not a bare X.
- Produce a **master SVG** (mark + wordmark + horizontal lockup) under `src/xpst/assets/brand/` (none exists today — all assets are locked rasters, some 486 bytes).
- **Export script** → `.icns`, multi-size `.ico`, Linux hicolor PNGs, tray 16/24/32/64, web favicon + apple-touch + 192/512 maskable + manifest. Replace all three current sets.
- Fix icon-resolution code that points at non-existent files; move assets under `src/xpst/assets` so the wheel actually ships them (`pyproject` packages only `src/xpst`).
- Unify tagline (drop "Cross-Platform Studio" — it implies a desktop-app builder) and casing ("xPST").
- Dashboard favicon + manifest.

### W6 — Knowledge Base Phases 2-5
Grounded task plan (KB lane). Key reconciliations first: extend `KnowledgeStore` ABC to the full 8-method port; add `embedding`/`created_at` to `Nugget` (frozen, defaulted for back-compat, id stays a hash of source+point); `pipeline.ingest()` becomes list-returning (ripples to `cli_kb`); MCP target is `mcp/server.py` (static TOOLS + dispatch), not `mcp_server.py`.

- **Phase 2:** full ABC; `Nugget`+`Area` model changes; OpenAI-compatible `LLMClient` + `extract.py` (strict JSON, graceful fail); `fastembed` nomic embedder (lazy); **LanceDB** adapter + `manifest.py` (atomic write mirroring `state_store.py`); rework `pipeline.ingest()` to extract→embed→dedup→store many. New deps in `[knowledge]`: `fastembed>=0.3`, `lancedb>=0.5`.
- **Phase 3:** embedding `router.py` (nearest area, grow new), clustering area discovery + auto-labeling (small LLM), difficulty tagging, deterministic ordering. All pipeline-intelligence, no big model.
- **Phase 4:** KB tools into `mcp/server.py`; full `xpst kb` CLI; `kb_course` assembly (hands the AI pre-ordered cited nuggets).
- **Phase 5:** durable sqlite queue + background worker; desktop "drop a link into an area" intake; `kb doctor`; import-linter rule enforcing the wall.

Each phase keeps the Phase 1 discipline: a golden corpus, idempotency, graceful failure, and a hard acceptance test.

### W7 — Test strategy (woven through every workstream)
Layered: unit (pure logic) → integration (boundaries, mocked providers/ffmpeg) → desktop e2e (offscreen Qt) → per-OS smoke (launch the built bundle headless, assert assets load) → CI matrix (mac/win/linux). The 26 current failures are W0. No workstream is "done" without its tests.

---

## Recommended sequence

1. **W0** (suite honest) — unblocks trustworthy iteration. *Starting now.*
2. Then parallel: **W2** (security) + **W3** (cross-platform/frozen-paths) + **W6 Phase 2** (KB) + **W4** (UI design-system pass).
3. **W5** (branding) gated on the owner's direction — it's the one workstream that needs taste, not just engineering.
4. **W7** continuously.

Branding and the macOS rebuild-confirm are the two items that need the owner in the loop. Everything else is executable now.
