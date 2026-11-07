# MVP cut: one phone, one file, honest results

**Status**: spec — the build order for the product reset. Each slice below is a card,
and each slice must work end-to-end before the next one starts.
**Read with**: [PHONE-FIRST-UX.md](PHONE-FIRST-UX.md) (the flow),
[PLATFORM-CAPABILITY-MATRIX.md](PLATFORM-CAPABILITY-MATRIX.md) (what may be promised),
[PROPOSAL-PRODUCT-RESET.md](PROPOSAL-PRODUCT-RESET.md) (distribution and routing).

The maintainer's instruction is explicit: **build it and make it work first.** That
rules out a big-bang rebuild and rules out breadth-first platform chasing. The cut
below publishes a real post from a real phone to two destinations that use different
access classes, and reports the platform's own answer for each. Everything else waits.

## 0. Rules for the cut

1. **The cut ships on whatever engine is current.** The Rust rebuild
   ([PROPOSAL-RUST-REBUILD.md](PROPOSAL-RUST-REBUILD.md)) must not gate the MVP; the
   MVP must not fork behaviour from it. New behaviour lands behind the same CLI/MCP/
   HTTP contract the rebuild has to preserve.
2. **No slice may claim success it cannot read back.** The acceptance test for every
   slice includes a per-destination readback.
3. **Posting stays paused until the counted dry run is green.** The gate already
   exists (`python -m xpst.rate_limit_dry_run --json <path>`, `docs/RATE-LIMIT-DRY-RUN.md`);
   the MVP inherits it rather than inventing a second switch.
4. **No destination is added before its matrix row.** A provider without a capability
   row, a refusal reason and a readback path is not in the cut.

## 1. M0 — a phone can publish and read back (the only slice that matters first)

**Works end-to-end means**: from a phone browser on the operator's tailnet, pick a
video from the camera roll, see the probed format and the resolved plan for YouTube
and X, confirm once, and get back two rows each carrying a platform id, a URL where
the platform exposes one, and a verification state.

| Item | Today | Work in M0 |
|---|---|---|
| Media ingest from a phone | **missing** — `POST /api/post` takes `media_paths` on the engine host | `POST /api/media` multipart: size cap, container sniff, probe, staging dir under the config dir, plan-scoped id, idempotency key |
| Route preview | **service shipped** (MCP `xpst_preflight`, `POST /api/preflight`: per-destination `enabled`/`auth_ready`/`quota_ready`/`hard_blockers`, `network_calls: false`); no CLI verb, no UI screen, no capability-driven envelope/transport contract (PR #340) | render the shipped plan in a phone screen; add `xpst preflight` and per-content-type transport so the preview can refuse honestly |
| Confirm + idempotency | partial (`/api/post` exists, no frozen plan) | frozen plan, `confirm_stale`, replay returns the original result |
| Per-destination readback | partial (engine records ids in state) | readback endpoint returning `verified` / `submitted, unconfirmed` + platform id + URL |
| Phone access | engine binds loopback; token is required for writes; reads open unless Basic auth is configured | device pairing (one-time code → device token with label + revoke); reads authenticated over a non-loopback bind |
| Client | Svelte pages with mobile breakpoints | compose → criteria → preview → confirm → result on a 390 px viewport with no horizontal scroll and 44 px targets |

**Deliberately excluded from M0**: PWA install, offline drafts, scheduling from the
phone, Instagram/Threads/Facebook, carousels on any destination, caption variants.

**Done when** the acceptance criteria in PHONE-FIRST-UX.md §7 hold with YouTube and X,
and one destination is deliberately put into a cooldown to prove the deferred state is
reported as deferred rather than failed.

## 2. M1 — criteria, gates and refusals (still two destinations)

The difference between "posts to two things" and "routes honestly":

- the per-post destination criteria object (opt-in, no implied set — PR #340's R3),
- every gate surfaced before the tap: not connected, needs app review, needs a
  professional account, unsupported format, transport unavailable, quota exhausted,
  cooldown active,
- the canonical outcome vocabulary (`posted`, `skipped_unconnected`,
  `unsupported_format`, `refused_by_community`, `failed`) used in the UI verbatim,
- quota and cost preview (YouTube 100 uploads/day per project; X per-request pricing,
  ~13x for a post containing a URL).

**Done when** a plan can be produced that publishes to zero destinations and exits
successfully with every requested destination named and refused, and when a
half-published plan (one succeeds, one fails) reports `partial` and names the failure.

## 3. M2 — installable, and honest about where it is not

- PWA manifest, icons, apple-touch metadata, `theme-color`; service worker caches the
  **shell only** — a pending post is never served from cache as if it were published.
- The install prompt is only offered in a secure context; over LAN HTTP the UI says
  "installable over HTTPS" rather than silently hiding the button.
- iOS: "Add to Home Screen" works; share-into-xPST does **not** (no Web Share Target
  in WebKit) — the UI offers file picker + clipboard instead of pretending.

**Done when** the app installs onto Android from the tunnel URL, launches standalone,
and a killed network during publish recovers to the engine's true state on reopen.

## 4. M3 — the gated classes, with the gates visible

Instagram, Threads and Facebook Page are Class B: a review and a professional/page
account stand between the code and a public post. This slice is as much paperwork as
code.

- Instagram: Graph publishing path (the repository's session mode is the fallback, not
  the promise), professional account detection, `content_publishing_limit` polling,
  public image URL requirement surfaced in the preview.
- Threads: text and video-with-`public_url`; the media host is a prerequisite, not a
  detail.
- Facebook: Page only.

**Done when** each destination's row in the UI states the gate that applies to *this*
account, and an unaudited TikTok app says "published privately" rather than "posted".

## 5. M4 — breadth, cheapest classes first

Ordered by actual access, not by fame:

1. **Bluesky, Mastodon** — Class A, open, no review. Cheapest real breadth.
2. **Telegram, Discord** — Class A, but the unit is a channel/webhook, so "connect"
   means choosing a channel.
3. **Reddit** — Class A with community rules; the subreddit is the gate, so the
   preview must name the subreddit and its rule check.
4. **LinkedIn (member)** — self-serve; the company-page variant is Class B and comes
   later.

**Done when** each new destination lands with its matrix row, refusal reasons, readback
path and a fake-transport test in the failure-injection harness.

## 6. M5 — manual handoff, done properly

Snapchat (regular accounts), Substack, Rumble and anything else without a write API:

- export a rendition that matches the destination (aspect, duration, bytes),
- prepare the caption in a copyable form,
- produce a share sheet on the phone where the platform supports one, and a copy-link
  flow where it does not,
- record the handoff as `prepared, not posted` and let the operator mark it done.

This is the class most products fake. xPST says "prepared — finish in the app" and
means it.

## 7. What is explicitly not in the MVP

| Deferred | Why |
|---|---|
| Native iOS app | The web app plus a wrapper is evaluated only after the service works; store review must not gate the product |
| Long-form YouTube | Forced Shorts today; PR #339 adds the real envelope. Not a phone-first blocker |
| Carousels on every destination | Support is per-destination and genuinely uneven; the preview names it |
| Offline posting queue | A post needs the network and the platform; a queued publish is a scheduling feature, not an offline feature |
| Multi-user / hosted billing | Seams only — see PHONE-FIRST-UX.md §6 |
| Live streaming | Phase 2, after the publish path is boring |

## 8. Proof the cut is real, today

Every claim in §1 was measured on `origin/main` (2026-11-06) rather than assumed:

- `xpst capabilities --json` returns the destination contract (YouTube, X, Instagram,
  Threads, Facebook, TikTok, Messenger) with implemented content types and media
  transport; `docs/PLATFORM-CAPABILITY-MATRIX.md` §3 is generated from it and pinned
  by `tests/test_platform_capability_matrix.py`.
- `xpst run --dry-run --json` completes with no uploads and reports the same target
  list for every video — the "implied set" M1 removes.
- The preflight service (MCP `xpst_preflight`, real file
  `~/.xpst/downloads/*.mp4`, requested destinations youtube/x/threads/instagram)
  returned `network_calls: false` and a per-destination plan: `youtube` and `x`
  `ready: true` with `auth_ready` and `quota_ready` true; `threads` `ready: false`
  (`enabled: false`) with the hard blocker *"Threads has no upload endpoint: its API
  fetches your media from a public server, and xPST is a local tool with no server to
  host a file for it"*; `instagram` `ready: false` (`enabled: false`,
  `auth_ready: true`). This is the preview the phone screen renders — and it is the
  proof for capability-matrix §6 rather than an argument for it.
- `src/xpst/dashboard/api.py` has `GET /api/media`, `/media/stream`, `/media/thumb`
  and `POST /api/post`, but no media **upload** route — the M0 gap.
- `src/xpst/dashboard/server.py` warns on a non-loopback bind without Basic auth, and
  `src/xpst/dashboard/auth.py` requires a credential on every mutating route — the
  M0 pairing requirement.
- `ui/public/` contains only `favicon.png`; `ui/index.html` declares an icon and no
  manifest — the M2 gap.
