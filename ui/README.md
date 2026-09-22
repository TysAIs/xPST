# xPST Web UI (design-system foundation)

Svelte 5 + Vite 7 + Tailwind 4 SPA served by the FastAPI engine
(`xpst.dashboard.server`). The root `DESIGN.md` is the canonical design
contract for semantics, accessibility, spacing, type, shape, elevation, and
motion. `src/tokens.css` implements the semantic light/dark roles and follows
`prefers-color-scheme` unless a host sets `data-theme="light"` or
`data-theme="dark"`.

Inter and Lucide are bundled locally. The canonical icy-X mark is imported from
`../assets/icon.png`; no remote font, icon, telemetry, or script CDN is used.
See `THIRD_PARTY_NOTICES.md` for source and license provenance.

## Build and test

```bash
cd ui
npm ci
npm test         # static design-system and routing contracts
npm run build    # → ui/dist
```

The FastAPI dashboard mounts `ui/dist` at `/` **when it exists**. Without a
build the engine falls back to its built-in string index page. `ui/dist` is
gitignored and must not be committed; packaging runs the build itself.

Set `XPST_UI_DIST=/path/to/dist` to serve a UI build from a non-standard
location.

## Develop (HMR against the local engine)

```bash
xpst ui &
cd ui && npm run dev   # Vite on 127.0.0.1:5173
```

The Vite dev server proxies `/api/*`, `/health`, `/state`, `/bio`, `/metrics`,
and `/oauth/callback` to `http://127.0.0.1:8080`. The app uses the same
same-origin paths in development and production. The engine's Basic auth is
handled by the browser natively.

## Layout

- `../DESIGN.md` — Google DESIGN.md token and usage contract
- `src/App.svelte` — shell entry point and hash router with `hashchange` and
  `popstate` support; opens `#/onboarding` once on a fresh install
  (`first_run_complete: false`) and never hijacks an explicit navigation
- `src/lib/components/` — reusable `Button`, `Card`, `StatusBadge`,
  `PlatformBadge`, `PlatformIcon`, `EmptyState`, `LoadingSkeleton`,
  `ErrorState`, `FormField`, `BrandMark`, `Shell`, and `Nav` primitives
- `src/pages/*.svelte` — dashboard views plus the first-run flow
- `src/lib/api.js` — thin fetch client over the `/api` endpoints, the
  `ApiError` (status + parsed body are preserved so a refusal is rendered
  truthfully), and the route table
- `src/lib/firstRun.js` — pure flow logic: destination rows, wizard steps and
  the result classification/wording (`resultTone`, `resultHeadline`)
- `src/lib/session.js` — session-scoped record of the last post, so `#/result`
  survives a reload and shows exactly what the engine returned
- `src/lib/media.js` — pure media-preview logic: kind detection, the preview
  plan (thumbnail vs stream URL), dropped/picked path handling
- `src/lib/native.js` — the desktop shell bridge: OS file picker and native
  drag-and-drop, which are the only honest sources of a filesystem path
- `src/tokens.css` — semantic light/dark tokens
- `tests/design-system-contract.test.js` — offline static contracts for the
  foundation and route normalization
- `tests/first-run-flow.test.js` — truthfulness contracts for the flow: a
  failed upload is never classified or worded as success, the API client uses
  the right verbs, and every screen compiles and exposes empty/error states
- `tests/media-preview.test.js` — composer preview contracts: a selected asset
  produces a preview element, the preview is a URL (never file bytes in the
  page), and the picker/drop degrade honestly outside the app window

## First-run flow

| Route | Purpose | Endpoints |
| --- | --- | --- |
| `#/onboarding` | Pick the content folder, enable destinations, persist `first_run_complete` | `GET/POST /api/onboarding`, `POST /api/onboarding/complete` |
| `#/connect` | Inspect, enable and verify one destination (live canonical probe) | `GET /api/providers`, `POST /api/connect/{platform}` |
| `#/compose` | Pick a local video, caption, destinations; preview it, then plan or post | `GET /api/media`, `GET /api/media/stream`, `GET /api/media/thumb`, `GET /api/providers`, `POST /api/post` |
| `#/result` | Per-destination outcome of the last post (success, partial, failure) | none (reads the session record) |

`connected`, `uploaded` and `ok` are only ever true when the engine verified
them; a destination that produced no upload result is reported as a failure
with the engine's own error text, never as a completed upload. A refused post
is a `409` carrying the same truthful body the result screen renders, and a dry
run (`dry_run: true`) reports `success: null` per destination because nothing
was attempted.

## Media preview (compose)

The composer shows what will be posted *before* it is posted, and it does so
without ever copying the file:

| Step | What happens |
| --- | --- |
| Selection | `GET /api/media` lists the folder; the first video is preselected |
| Image | `<img>` pointed at `GET /api/media/thumb` (a cached, ffmpeg-generated JPEG); if no thumbnail can be generated the element falls back to `GET /api/media/stream` |
| Video | `<video controls preload="metadata">` pointed at `GET /api/media/stream`, with the generated frame as its poster |
| Playback / seeking | `/api/media/stream` answers `Accept-Ranges: bytes` and real `206 Partial Content` responses, so the webview pulls only the byte ranges it needs |
| OS file picker | `plugin:dialog|open` over Tauri IPC — the shell returns a path, never bytes. Unavailable in a plain browser, where the composer says so instead of guessing |
| Drag and drop | The shell owns the native drop (a webview drop carries no path) and pushes the paths in as `window.__xpstMediaDrag(phase, paths)` |

The engine reads the source in bounded chunks (`xpst/dashboard/media_preview.py`,
1 MiB per read) and never materializes the file, so selecting a 2 GB video
costs the same as selecting a 5 MB one. Thumbnails are cached under
`~/.xpst/cache/previews` keyed by path + size + mtime, so replacing the source
invalidates the stale frame.

### Evidence for the preview contract

```bash
# engine side: ranges, refusals, thumbnails
python -m pytest tests/test_dashboard_media_preview.py

# UI side: preview element present, no file bytes in the page, drop/pick honesty
cd ui && npm test

# shell side: the picker grant covers the loopback port the app picks
cd src-tauri && cargo test
```

For a manual pass on a real machine, boot the engine with a throwaway config
dir and the built UI on loopback, then open `#/compose` in the app window: the
video preview plays, the image preview shows the generated frame, and dropping
a file from Finder selects and previews it. Measured on macOS with a real
engine: the page pulled the video with `206` range responses, and serving a
300 MB file grew the engine's RSS by ~10 MB, not by the file's size.

## Headless first-run audit

`scripts/ui_first_run_audit.py` serves the built UI from the real engine on a
spare loopback port with a throwaway config dir and drives headless Brave over
CDP, capturing per route: the rendered `<h1>`, the API requests made, console
errors, non-2xx responses, and an axe-core violation list. It then drives the
flow itself (save setup, inspect a destination, plan, post) and reports the
result screen's text.

```bash
cd ui && npm ci && npm run build && cd ..
python scripts/ui_first_run_audit.py --ui-dist ui/dist --engine fake --fake-outcome ok
python scripts/ui_first_run_audit.py --ui-dist ui/dist --engine fake --fake-outcome fail
python scripts/ui_first_run_audit.py --ui-dist ui/dist --engine real
```

`--engine real` uses the production app (nothing is publishable, so the post is
refused and the result screen must say so). `--engine fake` keeps the same
router and the real engine but injects a local fake platform uploader, so a
real post runs through the real code path with no network access and no real
account. The config dir is temporary and every credential path points inside
it, so the audit never reads `~/.xpst`.

## Backend contract

JSON endpoints (in `src/xpst/dashboard/api.py`, Basic-auth protected, never
exempt): `GET /api/summary`, `GET /api/videos`, `GET /api/videos/{video_id}`,
`GET /api/health-status`, `GET /api/settings`, `GET /api/onboarding`,
`POST /api/onboarding`, `POST /api/onboarding/complete`, `GET /api/media`,
`GET /api/media/stream`, `GET /api/media/thumb`, `GET /api/providers`,
`POST /api/connect/{platform}`, `POST /api/post`, `POST /api/preflight`.

`POST /api/post` delegates to `xpst.services.post_service.PostService`, which
plans with the canonical `PostPreflightService` and uploads with the real
`CrossPostEngine.post_manual`; no surface re-implements either. Backend tests:
`tests/test_web_ui_qa.py`, `tests/test_dashboard_first_run_api.py`, and the
end-to-end `tests/test_dashboard_first_run_e2e.py` (a real uvicorn server on a
temporary config dir with the platform uploader replaced, so nothing leaves the
machine).

## Accessibility audit

`scripts/a11y_audit.py` runs axe-core against the running UI over the Chrome
DevTools Protocol (headless Brave) and audits every route in both themes:

```bash
xpst ui --no-browser --port 8092 &
uvx --with websockets python scripts/a11y_audit.py --base-url http://127.0.0.1:8092 --theme both
```

It exits non-zero when any route reports a violation, so it can gate a release
check on a machine that has the browser. Two traps are already handled: the
devtools "new tab" endpoint drops a URL fragment (so routing is driven in-page
and each result reports the rendered `<h1>`), and a theme switch is verified
against the computed background colour before auditing, so "clean in light mode"
cannot silently mean "the dark theme twice".

This is not a CI gate — CI has no browser — but it is the check that caught the
`.xpst-button` contrast regression (1.11:1 for the primary CTA in dark mode).

