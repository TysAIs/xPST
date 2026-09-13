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
  `popstate` support
- `src/lib/components/` — reusable `Button`, `Card`, `StatusBadge`,
  `PlatformBadge`, `PlatformIcon`, `EmptyState`, `LoadingSkeleton`,
  `ErrorState`, `FormField`, `BrandMark`, `Shell`, and `Nav` primitives
- `src/pages/*.svelte` — existing read-only dashboard pages refactored to use
  primitives and honest loading/error/empty states
- `src/lib/api.js` — thin fetch client over the `/api` endpoints and route
  definitions
- `src/tokens.css` — semantic light/dark tokens
- `tests/design-system-contract.test.js` — offline static contracts for the
  foundation and route normalization

This wave does not add provider models, mutations, posting, onboarding, or
Tauri workflow behavior. Pages remain read-only views over existing API
contracts.

## Backend contract

JSON endpoints (added in `src/xpst/dashboard/api.py`, Basic-auth protected,
never exempt): `GET /api/summary`, `GET /api/videos`,
`GET /api/videos/{video_id}`, `GET /api/health-status`, `GET /api/settings`.
Existing backend tests live in `tests/test_web_ui_qa.py`.

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

