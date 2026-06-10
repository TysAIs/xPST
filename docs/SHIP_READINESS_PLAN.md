# xPST Ship Readiness Plan

This is the working release plan for making xPST a free, open-source,
platform-agnostic cross-posting studio that non-technical users can install,
connect, update, recover, and trust.

## Product Standard

xPST is ship-ready when a new user can install the app, connect supported
platforms, preview content, post safely, recover from failures, and update the
app without reading Python, YAML, API docs, or social-platform internals.

The core app must stay local-first and platform-agnostic. Platform-specific
behavior belongs in providers/adapters, not in the engine, scheduler, desktop
UI, state layer, or updater.

## Definition of Done

- Fresh installs pass on Windows, macOS, and Linux.
- A first-run user can complete setup from the desktop UI without editing files.
- Every platform provider reports capabilities, auth requirements, quotas, and
  health in a uniform shape.
- A provider failure never blocks unrelated providers.
- Posting supports dry-run preview, queue visibility, retry, cancellation,
  crash recovery, and clear user-facing error messages.
- App updates, provider metadata updates, and helper-tool checks are visible,
  reversible where possible, and do not destroy user state.
- Tests include unit, integration, stress, UI smoke, packaging, updater, and
  fake-provider workflow coverage.
- Release artifacts are signed or clearly marked unsigned, checksummed, and
  reproducible from documented commands.

## P0 - Release Blockers

1. Provider contract
   - Add provider metadata for all sources and destinations.
   - Track capabilities such as source, destination, analytics, carousel,
     delete, official API, cookie/session auth, and local-only behavior.
   - Make provider status renderable in CLI, MCP, dashboard, and desktop UI.

2. First-run onboarding
   - Replace config-first setup with a guided UI flow.
   - Show required tools, missing credentials, health, and next actions.
   - Never require YAML edits for normal use.

3. State and recovery
   - Keep cross-platform file locking.
   - Prove large queues, interrupted saves, corrupt state, and concurrent
     desktop/CLI access recover correctly.
   - Keep backups bounded and readable by support tools.

4. Update system
   - Separate app updates from provider metadata/helper updates.
   - Detect stale helper tools such as yt-dlp and FFmpeg.
   - Support update checks without network side effects.
   - Provide rollback guidance or state backup before risky updates.

5. Real workflow test harness
   - Add fake providers that simulate auth, upload, rate limits, deletes,
     analytics, network failures, and expired credentials.
   - Use fake providers in CLI, engine, scheduler, MCP, and desktop smoke tests.

## P1 - Public Beta Quality

1. Desktop UX
   - Dashboard should prioritize queue, platform health, next action, and recent
     failures over decorative metrics.
   - Content posting needs per-platform preview and validation before upload.
   - Errors should be plain-language with a retry/connect/open-settings action.

2. Packaging
   - Produce tested Windows and macOS desktop builds.
   - Include checksums, changelog, install docs, and uninstall/state-retention
     docs.
   - Verify app launch from a clean machine profile.

3. Observability
   - Local diagnostic bundle export is available through `xpst diagnostics`,
     including logs, provider status, config shape, versions, state counts, and
     redacted credential status.
   - Keep all private tokens, cookies, captions, and local paths redacted unless
     explicitly exported by the user.

4. Open-source maintainability
   - Issue templates cover platform breakage, install failures, and provider
     requests.
   - Docker context excludes local runtime state, credentials, env files, and
     release smoke output through `.dockerignore`.
   - Document provider adapter development.
   - Add contribution tests for fake providers and release gates.

## P2 - Post-Beta Polish

1. Plugin marketplace/discovery for community providers.
2. Background update notifications in the desktop app.
3. Import/export profiles for creators and agencies.
4. Accessibility pass for keyboard navigation, screen readers, contrast, and
   localized text expansion.
5. Optional telemetry-free diagnostics that users can attach to bug reports.

## Required Test Matrix

- Unit: provider metadata, state, config, updater, credentials, retry, quota.
- Integration: engine with fake providers, scheduler, CLI JSON, MCP tools.
- Stress: large queues, concurrent state access, corrupt files, slow disk.
- UI smoke: QML page load, first-run flow, connection states, queue view.
- Packaging: wheel build, clean wheel install smoke, desktop bundle build,
  clean install launch.
- Update: app version check, helper-tool check, provider metadata refresh,
  offline/no-network behavior, failed update handling.
- Security: plaintext secret scan, credential fallback encryption, redaction.

## Release Evidence

Every release candidate must include:

- `RELEASE_EVIDENCE.json` generated by `scripts/release_artifacts.py`.
- Full test output summary.
- Lint and type-check output.
- QML/UI smoke output.
- Packaging artifact paths and checksums.
- Manual smoke notes for Windows and macOS.
- Live platform health smoke output from `scripts/verify_live_platforms.py`
  when owner credentials are available.
- Known limitations and platform caveats.
