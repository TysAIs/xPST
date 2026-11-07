# Phone-first UX: the operator composes, xPST routes, the platform answers

**Status**: spec — product direction from the maintainer, 2026-11-03. Parts marked
*shipped* exist on `origin/main`; parts marked *build* are the MVP cut in
[MVP-CUT.md](MVP-CUT.md).
**Companion documents**: [PLATFORM-CAPABILITY-MATRIX.md](PLATFORM-CAPABILITY-MATRIX.md)
(what may be promised), [PROPOSAL-PRODUCT-RESET.md](PROPOSAL-PRODUCT-RESET.md)
(distribution, optional platforms, routing),
[MVP-CUT.md](MVP-CUT.md) (the build order),
[PROPOSAL-RUST-REBUILD.md](PROPOSAL-RUST-REBUILD.md) (engine rebuild).

---

## 1. The rule this document serves

The operator composes once, hands the post to xPST, and xPST routes it to the
destinations that match the criteria he set **for that post**, then reads back
honest per-destination results.

Three sentences that are not negotiable, in order:

1. **No autonomous posting.** Nothing publishes unless a human confirmed *this*
   post *with these destinations*. A schedule is a human-set instruction, not an
   agent's opinion; a scheduler that fires is still answering a confirmation the
   human gave for that entry.
2. **The operator is the only author.** xPST may propose a caption, a crop, a
   destination set. It may never silently substitute one.
3. **The report is the product.** A post that says "posted" when the platform
   returned a container id and nothing else is a defect. Every destination ends in
   a state that is either verified by readback or explicitly unverified.

This is why the phone matters: the confirmation has to be possible at the moment of
composing, on the device the media is already on.

## 2. The one flow (mobile first, desktop identical)

The flow is the same object on every client; only the viewport changes. Six steps,
one screen each, in this order. A step may not be skipped by the client.

| # | Step | Input | What the service returns | Gate before the next step |
|---|---|---|---|---|
| 1 | **Compose** | media (camera roll, share-sheet, file picker), caption, per-destination variants | media id, probed format (container, codec, duration, dimensions, bytes), caption lengths | media accepted and probed |
| 2 | **Criteria** | the set the operator wants for *this* post: destinations or "all that fit", audience/privacy where the platform allows it, schedule-or-now | the resolved envelope per destination | every requested destination resolved to a state, never dropped silently |
| 3 | **Route preview** | — | per destination: publish / refuse + reason / skipped (not connected) / needs a gate / manual handoff, with the envelope that decided it, the estimated cost and the quota left | no network write has happened yet |
| 4 | **Confirm** | explicit tap on a summary that names every destination and its state | plan id (idempotency key) + a frozen plan | a changed plan invalidates the confirmation (`confirm_stale`) |
| 5 | **Publish** | plan id | per-destination progress: queued → uploading → processing → published / deferred (cooldown) / failed + typed error | one destination never blocks another |
| 6 | **Readback** | — | per destination: platform post id, URL if the platform exposes one, verification state (`verified` / `submitted, unconfirmed`), timestamps | the record is durable before the client shows it |

### 2.1 What exists today (measured on `origin/main`, 2026-11-06)

- The engine's canonical destination contract exists and is served identically by
  `xpst capabilities`, MCP `xpst_capabilities` and `GET /api/capabilities`
  (*shipped*). It declares per-destination content types, implemented vs declared,
  and media transport.
- The engine's post path accepts `media_paths` that resolve **on the engine host**
  (*shipped*), and `POST /api/post` is a thin wrapper over it (*shipped*).
- The web UI is a Svelte app served from `ui/dist` with mobile breakpoints at 840 /
  760 / 430 px (*shipped*), with Compose / Result / Activity / Schedule pages.
- There is **no** upload endpoint (*build*): a file in the phone's camera roll cannot
  reach the engine. This is the first MVP blocker, not a polish item.
- The preflight service and its two entry points exist (*shipped*): MCP
  `xpst_preflight` and `POST /api/preflight` return a per-destination plan with
  `enabled`, `auth_ready`, `quota_ready`, `hard_blockers` and `network_calls: false`,
  measured on a real file (see MVP-CUT.md §8). What is missing is a CLI verb
  (`xpst preflight`), the capability-driven format-route/envelope contract and any UI
  screen that renders it (PR #340) — the preview *screen* is *build*, the preview
  *service* is not.
- There is **no** PWA manifest (*build*): `ui/public/` holds only `favicon.png`, and
  the shell declares a favicon but no manifest, `theme-color` or apple-touch icon.
- The dry run reports the same target list for every video — every enabled
  destination — which is exactly the "implied set" the criteria step must remove
  (*build*; PR #340).

### 2.2 Screen contracts

**Compose.** One media item or a carousel (per-destination carousel support differs;
the preview says which). Caption counter shows the *shortest* destination limit among
the selected set, and the per-destination counters behind a disclosure. A caption
that is too long for a selected destination is refused with that destination's
envelope, never truncated.

**Criteria.** The destination picker lists every destination the catalog knows, each
with its access class and its state for this account: connected + eligible,
connected + gated (with the gate named), not connected (`skipped_unconnected`),
unsupported for this format (`unsupported_format`), manual handoff. "All that fit"
is an explicit choice, never the default by omission.

**Route preview.** The preview is a computation, not a network write: it must be
producible with the network unplugged, and it must show, per destination, the
envelope and transport that produced the verdict. For a `public_url` destination
(Threads video) on an engine with no public media endpoint, the verdict is a refusal
with that reason — see the capability matrix §6.

**Confirm.** The confirm button names the count and the destinations ("Publish to
YouTube Shorts and X"). The plan is frozen: media digest, caption, destination set,
envelopes. If anything changed since preview, the client gets `confirm_stale` with
the reasons and must re-confirm. Double-tap is harmless: the plan id makes the
second call a readback of the first.

**Publish.** Per-destination independent progress. A cooldown defers a destination
and says when; it never drops it. A failure carries the typed error code the engine
produced, not a generic banner.

**Readback.** Each destination row carries the platform's own id and a verification
state. "Submitted, unconfirmed" is a first-class outcome: the post exists, the
platform has not confirmed it, and a retry must not create a duplicate (reconcile
owns that decision).

## 3. Safety invariants carried into the phone UI

These are already enforced in the engine (rate-limit cooldown store, refusal over
truncation, retry ownership, reconcile). The UI must not weaken them:

1. **One verdict per limit.** A 429 produces a cooldown with the provider's own
   window; the UI shows "deferred until HH:MM", never "failed".
2. **Counted dry run is the un-pause gate.** Before posting resumes, the counted dry
   run must produce one stable counts digest with `posts_lost: 0`. The phone UI shows
   the current state (paused / live) rather than assuming.
3. **Rate-limit status is visible before the tap.** Per-destination remaining quota
   and active cooldowns are part of the preview, not a post-hoc error.
4. **No truncation, ever.** Over-limit text or media is refused with the envelope.
5. **Per-destination independence.** One destination's auth failure cannot turn a
   successful publish into a visible failure, or vice versa.
6. **Posting is paused today.** Until the operator unpauses, the UI's primary button
   is "Preview" and a publish is opt-in per post.

## 4. Reaching the service from a phone

The engine is a local service. For phone use it must be reachable over HTTPS with
authentication. Three transports, in the order of implementation:

| Path | What it gives | What it costs | Honest limits |
|---|---|---|---|
| **Tailnet / private tunnel** (e.g. a mesh VPN's HTTPS front) | A real certificate, device-level identity, no public port | Requires the mesh client on the phone | Not usable by a stranger; fine for the operator |
| **Public tunnel** (reverse proxy with TLS, e.g. a managed tunnel) | Works from any phone, no client | A public hostname; must add app-level auth | The engine's own token is the boundary — see below |
| **LAN + HTTP** | Zero setup | No TLS | No service worker, no install prompt, no camera/mic APIs in most browsers, no clipboard write; treat as read-mostly |

What the engine actually enforces on a non-loopback bind (measured in
`src/xpst/dashboard/server.py`, `src/xpst/dashboard/auth.py`):

- Mutating routes (POST/PUT/PATCH/DELETE) **always** require a credential — Basic
  auth or the persisted API token. There is no unauthenticated write path.
- Read-only routes stay open unless `dashboard_username` + `dashboard_password_hash`
  are configured. A non-loopback bind without them logs a warning and exposes reads
  to the network.
- The API token is generated on first run and stored encrypted; `XPST_API_TOKEN` and
  `XPST_UI_TOKEN` are accepted but never persisted.

Therefore the phone-first requirement is: **remote access must be authenticated for
reads too**, and the client must hold a device token, not a query-string secret. A
pairing flow (one-time code shown by the engine, exchanged for a device token with a
label and a revoke control) is part of the MVP.

### 4.1 Installability is a TLS property, not a UI property

A PWA manifest plus a registered service worker installs on Android and desktop
Chrome; iOS "Add to Home Screen" works with a manifest and apple-touch metadata but
gives a WebKit app with no share-target support. Two consequences to state plainly:

- Over LAN HTTP there is no service worker (secure-context rule), so "installable"
  and "offline-capable" are false claims there.
- iOS cannot receive a Web Share Target, so "share from another app into xPST" on
  iPhone needs either the clipboard/file picker or a native wrapper. This is the one
  iOS gap that a PWA cannot close, and it matters for the Class D handoff path.

## 5. Client architecture

| Client | Status | Shape | Why |
|---|---|---|---|
| **Responsive web app served by the engine** | shipped shell, mobile layout partial | Svelte bundle in `ui/`, served at `/` by the FastAPI surface, talks to `/api/*` with a bearer token | One codebase; no store; works on every OS and every phone browser |
| **PWA packaging** (manifest, icons, apple-touch metadata, service worker for shell caching only) | *build* (M1) | Same bundle, no separate code path | Install without a store; offline caching of the shell, never of a pending post |
| **Desktop** (Tauri shell) | shipped | Reuses `ui/`, adds deep links, updater and the sidecar | Already Rust; the rebuild makes the engine in-process |
| **iOS wrapper** | *evaluate after MVP* | Either a thin WKWebView shell (same bundle, adds share-target + background upload) or a native SwiftUI client over the same HTTP contract | Store review and Xcode are not allowed to block the MVP |
| **Android wrapper** | not planned | The PWA plus share-target already covers the common path | Revisit only if background upload is needed |

Architecture rules that follow:

1. **Every capability is an engine route.** A feature implemented in a client is
   invisible to MCP, CLI and the next client. The clients are views over
   `/api/*`, which is a view over the engine — the same path MCP and the CLI use.
2. **The API is the contract, versioned.** The phone, the desktop shell and the
   future iOS wrapper all consume the same JSON. No client may compute capability
   truth locally; it reads `GET /api/capabilities` and the preview route.
3. **No client-side posting logic.** Deferral, cooldowns, retries and reconcile live
   in the engine so two clients cannot disagree.
4. **Idempotency is a first-class field.** Every mutating request carries a plan id;
   the server is the authority on "already done".

## 6. Where a hosted tier changes this design

Nothing here blocks the open-source core. These are the seams to leave open, not to
build now:

- **Accounts.** Today one `~/.xpst` per user is the identity. A hosted tier needs
  tenant ids, per-tenant config/state/credential namespaces, and per-device tokens
  scoped to a tenant. Keep every path through a config-dir resolver so this is a
  parameter, not a rewrite.
- **Billing.** An entitlement check belongs at the plan boundary (destination set
  size, scheduled posts, analytics depth), not inside uploaders.
- **Isolation.** Per-tenant media staging with its own encryption key; per-tenant
  egress for the platform's URL fetch; no shared temp directory names.
- **Shared app credentials cut both ways.** A hosted tier registers one Meta/TikTok
  app for everyone, so *one* review satisfies every tenant — that is the hosted
  product's real moat. But the same app also means shared quotas: YouTube's
  `videos.insert` ceiling is **100 uploads/day per project**, so a hosted tier needs
  per-tenant projects (OAuth BYO) or a quota-raise conversation with every provider.
  Record this before pricing anything.
- **Public media host.** `public_url` destinations (Threads video, Instagram images
  on the Graph path) need a public, unguessable, expiring media URL. In a hosted
  tier that is a CDN and a signed-URL service; in the local tier it is the tunnel.

## 7. Acceptance criteria for "the phone flow works end-to-end"

Measured on a real phone against a real engine, with posting enabled only for the
test account:

1. A video from the phone's camera roll reaches the engine and is probed (container,
   codec, duration, bytes) with the probed values shown before confirm.
2. The preview names, per destination, one of: publish / refuse + reason / skipped
   unconnected / needs gate / manual handoff, and the envelope that decided it.
3. Confirm publishes to at least two destinations with different access classes, and
   each row returns the platform's own id and a verification state.
4. A destination in cooldown is reported as deferred with the window, and a retry
   after the window publishes exactly once.
5. Killing the phone's network after confirm and reopening the app shows the true
   state from the engine, not a spinner and not "posted".
6. The same URL on a desktop browser shows the same plan and the same readback.
