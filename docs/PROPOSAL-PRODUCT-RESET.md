# PROPOSAL — Product reset: distribution, opt-in platforms, format routing

**Status**: proposal, awaiting maintainer decision. Not binding until accepted into
[DIRECTION.md](DIRECTION.md) as D-9…D-13.
**Inputs**: maintainer product input, 2026-11-04 (distribution undecided; add Reddit; every
platform optional; format-aware routing is the core value; live aggregation is phase-2 only).
**Evidence base**: measured against `origin/main` @ `fc2eadd` with the installed engine
(`xpst capabilities`, read of `src/xpst/`, `docs/`). Numbers quoted here are outputs, not
recollection; where a fact is external (platform API behaviour) it is cited, and where it is a
judgement it is labelled as one.

---

## 0. What this document asks for

Five decisions, stated up front so they can be accepted, amended or rejected individually:

| # | Decision | Recommendation (§) |
|---|---|---|
| D-9 | Distribution: binary / hosted / hybrid | **Hybrid, binary-first** — the Rust binary is the product; a hosted *relay* is added only for the two things a binary structurally cannot do (§2.4) |
| D-10 | Every platform optional, opt-in only | Accept as a hard requirement and change the "default: all" semantics that exist today (§4) |
| D-11 | Format-aware routing as the core value | Accept; it is the one thing no incumbent does well, and it forces three concrete gaps closed (§5) |
| D-12 | Reddit added; Rumble added | Reddit: yes, v1. Rumble: yes, but capability-flagged as *integration-uncertain* (§3, §5.3) |
| D-13 | Live-stream aggregation | Out of v1. Phase 2 starts with read-only "who is live" for Twitch/Kick; restreaming is a relay feature (§6) |

The PyPI question is answered by §2.6: it is **not** on the critical path of any of the three
distribution options, and treating it as a gate has already cost more than it is worth.

---

## 1. The product, restated from the inputs

> One asset in. The creator's connected accounts, adapted per platform, in the format that
> platform actually accepts — and nothing the creator did not opt into.

Three properties follow, and they are load-bearing for everything below:

1. **Optionality is the trust story.** A creator connecting X only must get a working product,
   not a degraded one. No platform is "required", none is silently on.
2. **Routing is the value.** Copy-paste-everywhere tools already exist. Deciding *which*
   platform gets *which* rendition of *what* is the job.
3. **Distribution is a product-shape question, not a packaging question.** It decides whether
   tokens ever leave the user's machine, who pays for video egress, and whether a post can fire
   while the laptop is closed.

---

## 2. Distribution: binary, hosted, or hybrid

### 2.1 The three options as they actually are

| Option | What ships | Who holds the tokens | Who holds the media | Who pays for egress | Time to first post |
|---|---|---|---|---|---|
| **A. Rust binary** (open source, local-first) | one signed binary + installer, MIT/Apache-2.0 | the user, on disk | the user | nobody | install → connect → post |
| **B. Hosted subscription** | web app + API, multi-tenant | us (encrypted at rest) | us (object storage + CDN) | us, per view | sign up → OAuth → post |
| **C. Hybrid** | binary as the engine + a relay service with a narrow job | user by default; relay only for relay jobs | user, uploaded on demand and deleted | user, or the relay for relay jobs | install → connect → post, plus an optional relay account |

### 2.2 The capability test — what the product needs, and who can deliver it

This is the part that decides the question. Each row is a requirement implied by the inputs or
already encoded in the repo; the "binary" and "hosted" columns are what each shape can do without
inventing something new.

| Requirement | Binary on the user's machine | Hosted service | Evidence / source |
|---|---|---|---|
| Usable from a phone, anywhere (not just the same LAN) | **Fails in the general case.** The daemon listens on a local port; from a phone that means same-Wi-Fi, a VPN/overlay (Tailscale-class), or a tunnel you run yourself | Native | `xpst serve` runs the scheduler/dashboard on a local port |
| Unattended scheduled posting at 03:00 | **Fails structurally.** Needs `xpst serve` (supervised daemon) or an external cron on a machine that is awake; a sleeping laptop misses the fire window | Native | `src/xpst/serve.py` docstring: scheduler loop runs in-process; `schedule_manager` takes a cross-process lock so "cron + serve" cannot double-post |
| Destinations that fetch media from a public URL | **Fails structurally.** Threads is a URL-fetch destination; a local file is refused before any network call, and the engine cannot hand it a URL yet | Native — it already has the bytes and a URL | `xpst capabilities` → threads: `media_transport: public_url`, `THREADS_NEEDS_URL` |
| One-click sign-in to Meta/TikTok | **Fails today by design.** 5 platforms require the user to create their own developer app | One shared, reviewed app: one click per platform | `src/xpst/byo.py`: `BYO_PLATFORMS` = instagram, threads, messenger, facebook, tiktok |
| Platforms gated by app review | Each user's own app is unaudited → TikTok Direct Post stays blocked | Our app is audited once, for everyone | `xpst capabilities` → tiktok note: "publishing is blocked by TikTok app review" |
| Token custody and blast radius | Token on the user's disk; a breach of *our* infra leaks nothing | Tokens in our DB → encryption at rest, refresh jobs, revocation, breach notification | judgement; see §2.5 |
| Zero personal data (DIRECTION D-2) | Structurally satisfied: nothing to store | Requires a privacy policy, processor terms, export/delete, retention limits | D-2 |
| Media storage cost | Zero (user's disk) | Real money: video egress is the dominant hosted cost | judgement |
| Reproducible support | Unbounded environment variance (codecs, proxies, AV, Windows paths) | One environment | judgement |
| Update delivery | Signed release + Tauri updater (already built) | Deploy; no user action | `docs/RELEASE.md`, `docs/tauri-updater-production.md` |
| Monetisation | Hard: a Rust binary cannot be "un-run"; licence keys need a server anyway | Native recurring revenue | judgement |
| Competitive position | Competes with almost nothing; its niche is "no third party ever sees my tokens" | Head-on with Buffer/Later/Publer at $5–12 per channel/month | §2.5 pricing anchors |
| Time to a *flawless* bar | One environment to make flawless per platform, but N user environments to survive | One environment, fully observable — but a much larger surface to build | judgement |

Three rows do the real work: **phone access**, **unattended posting** and **URL-fetch
destinations**. They are not
convenience gaps; they are places where a local-first binary cannot be made to behave, no matter
how good the Rust is. Everything else is a trade.

### 2.3 The honest counter-argument to "hosted is the aspiration"

A hosted service is the aspiration because it can be flawless and it can be paid for. Both are
true. What is also true:

- **A hosted-only xPST is a from-scratch multi-tenant product**, not a port. The Rust engine
  becomes portable, but you add auth, billing, quotas, per-tenant isolation, queues, object
  storage with lifecycle rules, media transcoding at scale, an incident process, and a privacy
  programme. The Rust rebuild's own bar (D-4: ≤ 25 MB binary, ≤ 1.0 s cold boot) buys none of that.
- **It competes on price with tools that already do 90 % of it** for $5–6 per channel per month.
  The differentiator has to be routing and honesty, not "scheduling, but hosted".
- **It gives up the only structural trust claim** the project has: tokens never leave the machine.
  That claim is worth more to a creator with 6 accounts than $6/month of convenience.

Symmetrically, the honest counter-argument to "binary only":

- Two of the five hard requirements fail (above). Today Threads media *does not work at all* and
  scheduled posts only fire when a daemon happens to be running.
- Onboarding for a non-technical creator is currently: install, create a Meta app, paste an app id
  and secret, and hope the loopback redirect is accepted. That is a developer chore, not an
  onboarding flow.

### 2.4 Verdict

**Hybrid, sequenced binary-first. The binary is the product; the relay is a service with exactly
two jobs.** Concretely:

**Phase R (now → Rust 1.0).** Ship the binary. It must stand alone as a complete product for
platforms that need no server: YouTube, X, Instagram (BYO or shipped app), TikTok (when audit
clears), Reddit, Rumble. Fully local, zero personal data, opt-in accounts, format routing, signed
releases. This phase is already funded and in flight (`PROPOSAL-RUST-REBUILD.md`).

**Phase H (only after Phase R has users).** Add a relay with a deliberately narrow contract:

1. **Media hosting for URL-fetch destinations** — the binary uploads a rendition it cannot serve,
   the relay returns a short-lived public URL, the platform fetches it, the relay deletes the
   object after N days. This makes Threads media, and IG's Graph image path, work without turning
   the relay into a data store.
2. **Always-on schedule execution** — the binary hands a job to the relay ("post this at 03:00,
   these accounts, this rendition"), the relay posts it even if the laptop is closed, and reports
   back the platform verdicts.
3. **Remote access for phones** — the binary opens one authenticated outbound channel; the phone
   (PWA or browser) talks to the relay, which reaches the user's running engine when it is online
   and its own job queue when it is not. This is the relay as *door*, not as *database*.

The relay is **stateless with respect to content** (objects expire; no library of the user's
media), and **stateless with respect to tokens by default** — unless the user opts into relay-side
scheduling, in which case only the tokens for the accounts they scheduled are escrowed, and the
UI says so in one sentence. That keeps the "tokens stay home" claim true for everyone who does not
need the relay, which is most of the value of the claim.

Why not hosted-first: you cannot earn the recurring revenue before the product is worth it, and
the cost of the multi-tenant surface lands before any proof of demand.
Why not binary-only forever: it caps the product at "works when your machine is on, and never for
Threads", which is exactly the thing a paying creator notices.

**Falsifiers** (what would change this recommendation, stated so it can be argued with):

- If a majority of target users are single-machine and never use Threads, Phase H is optional
  forever, and the binary alone is the right answer.
- If onboarding completion without a shared app measures below ~50 %, the relay (or at minimum
  shared OAuth apps) becomes mandatory *before* it is a paid feature.
- If hosted egress costs exceed ~30 % of subscription revenue at target mix, the relay must own
  storage lifecycle aggressively (delete-on-publish) or price by uploads, not by views.

### 2.5 Onboarding, pricing, and what "flawless" has to mean

**Onboarding — best practice, adapted to this product.**

- One install, then a connection list. No platform is preselected; each row states what it unlocks
  and what it costs in scopes.
- **One OAuth app per platform, shipped by the project, not by the user.** A creator should never
  see "App ID" and "App Secret". BYO stays as an override for people who want their own quota.
- Redirect handling must be boring: loopback listener first, `xpst://` deep link second, paste-a-code
  third — every one of those paths already exists (`src/xpst/auth_flow.py`), they just have to be
  chosen automatically and narrated.
- Connect → **immediately prove it**: a read-only capability probe per platform, with the honest
  badge. Never show "connected" for a platform that cannot publish.
- The first post is a rehearsal: the default first action is `xpst preflight` showing the resolved
  routing plan — what will go where, what will be refused and why — before anything is published.
- Failure text names the next action, not the error code. This is already the house style
  ("senders refuse rather than truncate"); onboarding must match it.

**Pricing shapes, and which one this product can defend.**

Observed list prices, Nov 2026 (third-party price roundups; treat as anchors, not quotes):

| Tool | Shape | Anchor |
|---|---|---|
| Buffer | per connected channel, volume bands | $6/channel/mo monthly, $5 annual; free tier 3 channels |
| Publer | per account tiers | ~$9.60–17/mo depending on accounts |
| Later | flat tier | ~$25/mo |
| Hootsuite | per seat | from $99/user/mo |
| Flat-unlimited challengers | flat, unlimited accounts | ~€8/mo |

Shapes and honest fit:

- **Per connected account (banded).** The industry's default because it tracks the vendor's cost
  (API quota, per-account review, support). Defensible here for the relay: cost tracks accounts.
  Downside: it taxes adding platforms, which is the product's whole point — mitigate with a free
  band and steep volume steps (Buffer's own $6/$4/$3/$1 curve is the precedent).
- **Per post / per video-minute.** Rejected. It taxes cross-posting, i.e. it prices the core value
  as a cost centre. It also makes the creator's bill unpredictable, which is worse than it being
  slightly too high.
- **Flat unlimited.** Attractive ("$9, everything"), but video egress is not flat; it only works if
  the relay deletes objects and caps upload minutes somewhere.
- **Per seat.** Wrong axis: this is a solo-creator product before it is a team product.
- **One-time licence for the binary.** Honest but weakly enforceable without a licence server. Keep
  the binary free and open source; sell the relay.

**Recommended shape**: binary free forever (open source, no account needed); relay priced per
connected account with a free tier that covers the common case (2–3 accounts, a monthly post cap),
annual discount, and an explicit "local mode is free and complete" sentence on the pricing page.
That sentence is the differentiator, and it is only true if the binary is genuinely complete.

**What "flawless" requires** — the bar, written as gates rather than adjectives:

*Publishing correctness*
- Idempotency: every (asset, platform, account) publish carries a key and a local ledger entry; a
  retry after a timeout never double-posts.
- Partial-failure reconciliation: one platform failing never rolls back the others, and the result
  record says `posted | refused | failed` per platform with the provider's own reason.
- No silent truncation anywhere: text over the limit is refused with the limit in the message;
  video over the duration cap is refused, not clipped.
- Rate limits: one verdict per limit, `Retry-After` honoured (already implemented for TikTok),
  cooldowns persisted across restarts.

*Trust*
- Zero personal data in the repo and in the artifacts (D-2, now enforced by a checked-in deny list).
- No telemetry. If telemetry ever appears, it is opt-in and off by default.
- Tokens in the OS keyring in binary mode; relay escrow only for scheduled jobs, stated in the UI.
- A plain-language "what left your machine" view: which platforms, which files, when.

*Operations (relay only)*
- A published uptime target and a status page; a backup *and* a restore drill, not just backups.
- Media lifecycle enforced by code (delete after N days, or on publish), with a test that proves it.
- Per-user export and delete, one command/one button, verified by a test.
- Billing failure path (dunning, grace period, downgrade to local mode) that does not lose a post.

*Release*
- Signed, notarised artifacts, staged rollout, rollback path, and update channels that cannot be
  pinned backwards by accident.
- A capability truth table generated from the code (the `xpst capabilities` catalog is already that
  object) so docs cannot drift from behaviour.

### 2.6 The PyPI question, decided by this analysis

The premise ("publish to PyPI") came from the era when the Python package *was* the product. Under
D-3 the shipping artifact is a Rust binary; PyPI distributes Python.

- **The binary does not need PyPI.** GitHub Releases carry the installer, Homebrew/winget/Scoop
  carry the convenience installs, crates.io carries the CLI crate. None of those are on the same
  failure path as a Python index.
- **The Python package has exactly one remaining job**: being the escape hatch for the 1.3.x users
  during the rebuild (D-8's open item). That job needs *a* publishable artifact, not a perfect
  pipeline. A token publish is a one-command chore; the OIDC `invalid-publisher` mystery stops
  mattering the moment the binary is the product.
- **The name is still worth claiming** (cheap, prevents squatting, keeps `pip install xpst`
  meaningful), but claiming it is not a release gate and must not block a ship.

**Recommendation**: claim the name once with the existing token at the first 1.3.x final; keep the
Python package publishable for one cycle; after Rust 1.0, the PyPI package is either frozen or a
thin shim. **Do not build a third packaging pipeline, and do not let a 404 on `pypi.org` block a
release again.** The open PR #335 already makes the token path honest without a tag (version/tag
guard, refuses to start without the secret, idempotent re-runs); that is the right amount of
investment, and it is the last PyPI work this project needs. If the pipeline stays broken, the
documented fallback is `uvx --from git+<tag>`, which is honest and works.

### 2.7 Reconciliation with the phone-first reset

A sibling direction (t_26686afa) asks for the same reset from the other end: phone-first, PWA
before app store, "the maintainer stays in charge of every post", a capability matrix for every
major platform, and "do not let open-vs-paid block the build". These are not competing documents;
they are two halves of one decision, and this section is where they meet:

- **Phone-first is a distribution requirement, and it lands on the same answer.** A binary that
  only listens on a local port is not phone-first. Either the user runs an overlay/tunnel (fine for
  a developer, not for a creator) or the relay is the door (§2.4 job 3). Phone-first is therefore
  the strongest product argument for the hybrid, independent of monetisation.
- **"The maintainer stays in charge" is enforced by the routing contract, not by good intentions.** The plan
  is displayed before anything is published, the target set is explicit (§4), and no default
  ("all") can add an account the creator did not choose. Autonomous posting stays off — which is
  also the current posting pause (D-7) and should remain the default posture.
- **The capability matrix is the same object as the routing envelope registry** (§5). One table
  generated from code serves the phone UI, the CLI plan, the docs, and the "never promise automation
  a platform does not allow" rule. Building it twice would guarantee drift.
- **Open-vs-paid does not block the build** (§2.4 sequencing): Phase R is open source and complete
  on its own; Phase H is additive.

So the two cards should not both produce a reset document. This document owns *distribution,
platform set, optionality and routing*; t_26686afa owns the *phone-first UX flow, the full
platform capability matrix and the client architecture*. Where they overlap — the matrix, the
phone access path — one artifact must be produced, not two.

---

## 3. Reddit as a platform

**Why it belongs**: it is the only major destination where long-form video *and* long text reach an
audience by community rather than by follower graph, and it needs a routing parameter (which
subreddit) rather than a platform checkbox. That makes it a genuine test of the routing model.

**Integration surface** (Reddit's documented OAuth2 API, `https://www.reddit.com/dev/api`):

- **Auth**: OAuth2 "installed app" flow, which is loopback-compatible with the listener the repo
  already has. One project-owned app id serves everyone; no per-user developer app needed, and no
  app review gate comparable to Meta/TikTok.
- **Content**: `POST /api/submit` with `kind=self` (title + self text), `kind=link`, `kind=image`,
  `kind=video`. Video is a three-step flow: `POST /api/media/asset.json` returns an upload lease
  (S3 action URL + fields + asset id), the client uploads to S3, then submits with `kind=video` and
  the asset id. That is a second multipart flow in the codebase, and it must be proven with a fake
  transport, not assumed.
- **Constraints that are part of the product surface**: a title is mandatory and capped (~300
  chars); self text ~30 000; media posts cannot mix image and text body; **flair and submission
  rules are per-subreddit** and rejections arrive as moderation, not as clean API errors; the free
  tier is limited per OAuth client (100 requests/minute, `X-Ratelimit-*` headers), and higher volume
  is commercially metered.
- **Identity**: a descriptive `User-Agent` is required; bots that ignore subreddit self-promotion
  rules get banned by humans, which the engine can never prevent.

**Scope for v1**: text (self post) + link, per-subreddit target, per-target verdicts. Video behind a
capability flag until the lease→S3→submit flow has a test transport and one live proof. Image only
after text/link are proven.

**Design consequence**: the config model needs a *community destination* — a platform whose target
is `(subreddit, flair?)`, not just an account. Routing must resolve to subreddit sets, and the
pre-publish plan must show them ("r/foo, r/bar — both allow video; r/baz requires flair"). Refusals
caused by community rules must be reported as `refused_by_community` with the subreddit named, not
as a generic failure.

---

## 4. Every platform optional — the hard requirement

This is not a toggle; it is a contract on defaults, and the current code violates it in one place:
`--platforms` defaults to **all enabled platforms** (`src/xpst/cli.py`: "Comma-separated platforms
(default: all)"; the engine treats `None` as "all enabled"). A newly connected account therefore
joins every future post without being asked. That has to go.

**Rules**

1. **Connecting is opt-in and per account.** No platform is connected by default; no "recommended
   set" is pre-ticked; the wizard shows an empty list with "connect what you want".
2. **A post targets an explicit or derived set, never an implied one.** The target set is
   `explicit_targets` if given, otherwise the *format route* ∩ *connected accounts*. `--all` exists
   and means all connected accounts that accept the format — explicit, not default.
3. **The resolved set is shown before publishing** (CLI plan, desktop confirmation) and is the same
   object the scheduler persists. If the user cannot see where it goes, it is not optional.
4. **Unconnected is not failed.** Preflight and results distinguish
   `skipped_unconnected` (with the reason), `unsupported_format` (per platform envelope),
   `refused_by_community`, `failed`. A partial route is a normal, successful outcome.
5. **Any subset works, including one.** Tests must cover 1…N−1 platforms, zero platforms (one
   actionable error naming the connect command), and a requested-but-unconnected target (skipped,
   not an error the user has to decode).
6. **Mode flags must not silently remove platforms.** The existing `official`/`community` visibility
   switch hides unofficial platforms; optionality means *the user* decides, so mode only changes
   the default suggestion, never the connected set.
7. **Migration is explicit.** Existing configs keep their accounts but are *not* auto-added to
   default posts; one confirmation during upgrade records the intent.

---

## 5. Format-aware routing — the core value

### 5.1 Measured reality today (`xpst capabilities`, origin/main `fc2eadd`)

| Destination | text | image | carousel | video | Measured note |
|---|---|---|---|---|---|
| YouTube | — | — | — | ✅ | **forced to Shorts** (`#shorts` appended); "no long-form path" |
| X | ✅ 280 ch | ✅ | ✅ (as thread) | ✅ | video duration cap 140 s in the platform module |
| Instagram | — | ✅ (JPEG) | ✅ (2–10) | ✅ | Graph path needs a public image URL |
| TikTok | — | — | — | ✅ | publishing blocked by app review; connection is source-only |
| Threads | ✅ 500 ch | — | — | ⚠️ | URL-fetch only; local file refused (`THREADS_NEEDS_URL`); no URL delivery exists yet |
| Facebook Page | — | — | — | ✅ | Page-scoped only |
| Messenger | ✅ (DM) | — | — | — | messaging, not publishing |

So the routing promise is currently **partially impossible** for the exact formats requested:

1. **Long-form video → YouTube** does not exist. The YouTube path hard-forces Shorts.
2. **Long-form video → X** is capped at 140 s by our own module.
3. **Text → Threads** works; **video → Threads** cannot work without either a relay or a
   user-supplied public URL.
4. **Rumble** is not in the codebase at all (no occurrence in `src/` or `docs/`).

**This table is a snapshot at `fc2eadd` and three of its four findings have since moved**
(gaps 1, 2 and 4; gap 3 has not). PR #339 added real long-form YouTube (explicit
`--shorts`/`--long`, resumable `videos.insert`, watch URL), a configurable
`x.max_video_duration_seconds` for the X envelope, and a Rumble destination whose honest
path without a partner key is a manual handoff (`integration_uncertain`).
[PLATFORM-CAPABILITY-MATRIX.md](PLATFORM-CAPABILITY-MATRIX.md) §3 is the generated,
test-pinned per-destination table and it wins wherever the two differ; read that before
quoting this snapshot.

### 5.2 The routing contract this proposal adopts

A route is data, resolved by code, printed before execution:

```
format classification (probe: duration, aspect, kind, transcript?)
  → candidate destinations (declared envelope per platform, from the capability registry)
  → connected accounts (opt-in set)
  → rendition plan (transform or refuse; never truncate)
  → resolved plan (posted / skipped_unconnected / unsupported_format / refused_with_reason)
```

Target matrix (the product promise, with the gaps made explicit):

| Format | Destinations | Status |
|---|---|---|
| Short-form video (≤ 60 s, vertical) | Instagram Reels, TikTok, YouTube Shorts, X, Facebook Page, Reddit (video, flagged) | mostly built; TikTok gated by review |
| Text | X, Threads, Reddit (self post) | X + Threads built; Reddit new |
| Long-form video (landscape, > 180 s) | YouTube (long-form), X (needs cap raised), Rumble (integration-uncertain), Reddit | **needs new work in 3 of 4** |

**Required implementation decisions** (each is a small, testable unit):

- **YouTube**: replace the implicit Shorts forcing with an explicit mode (`--shorts` / `--long`,
  or derived from the probe), and refuse long-form uploads until the resumable long-form path
  exists rather than silently shipping a landscape video as a Short.
- **X**: raise the 140 s cap to X's real per-account limit, or keep the cap and classify > 140 s
  as unsupported for X (refusal, not truncation).
- **Rumble**: treat as integration-uncertain. Rumble's upload API is partner-gated with no
  self-serve developer console comparable to Meta/Google; plan a capability-flagged API path plus a
  manual fallback (export the rendition, hand off a link). Never advertise Rumble as ready before a
  live upload succeeds.
- **Transform contract**: per-platform envelope (duration, aspect, codec/container, max bytes,
  caption length) held in the capability registry; `ffmpeg` invoked as the documented external tool
  D-3 permits; out-of-envelope input is refused with the envelope in the message.
- **Classification thresholds** must be stated (they are a product decision, not a hidden heuristic):
  ≤ 60 s and vertical → short-form; ≥ 180 s or landscape → long-form; ambiguous → ask, never guess.
- **Caption adaptation**: per-platform limits with refusal over truncation; the same text must not
  be silently cut to fit the shortest platform in the route.

This is also the honest answer to "why not just use Buffer": they publish the same asset everywhere
and let the creator fix it afterwards. The routing plan *is* the product.

---

## 6. Live-stream aggregation — phase 2, feasibility only

Three different features hide under "aggregation"; they have very different costs:

1. **Status aggregation** — "which of my accounts are live now, with links" (read-only).
2. **Chat/comment fan-in** — one inbox for live chat.
3. **Restream** — one ingest, N outputs.

| Platform | Status read | Chat fan-in | Restream out | Verdict |
|---|---|---|---|---|
| Twitch | Official Helix `streams` + EventSub `stream.online` | Helix/EventSub chat | No RTMP-out (needs a relay/fan-out) | Easy (status), moderate (chat) |
| Kick | Public REST channel/livestream endpoints + webhooks (`livestream.status.updated`); semi-official | WebSocket/Pusher chat | Relay fan-out | Moderate, watch for API churn |
| TikTok LIVE | No general public REST for a live session; the official LIVE API exists but is scope-gated; the common path is an unofficial signed WebSocket connector | Same connector | Relay fan-out | Fragile |
| Instagram Live | Graph API creates a broadcast (`live_media`) returning RTMP ingest URL + stream key; `live_media` collection reads what is live; **no scheduling of a live**, no chat fan-in | Not in Graph API | RTMP in only | Hardest |

**Recommendation**: keep live entirely out of v1. Phase 2 starts with **(1) status for Twitch and
Kick** — read-only, fits local-first, needs no server, and answers the actual user question ("am I
live everywhere I should be?"). Chat fan-in only where the platform gives it (Twitch, then Kick).
**Restream is a relay feature** (upload the stream once, fan it out server-side); doing it locally
means uploading N times from the creator's connection, which is exactly the kind of thing a hybrid
relay exists to fix. That is the second product argument for §2.4.

---

## 7. Proposed amendments to DIRECTION.md

- **D-9 (new) — Distribution**: hybrid, binary-first. The Rust binary is the shipping product and
  the trust boundary; a relay with exactly three jobs (URL-fetch media hosting, always-on scheduled
  execution, remote access for phones) is added after Rust 1.0, priced separately, and never becomes
  a content store or a second source of truth.
- **D-10 (new) — Opt-in platforms**: every platform is optional; connection is per account and
  opt-in; no post targets an implied "all"; unconnected is `skipped`, not `failed`.
- **D-11 (new) — Format-aware routing is the core value**: routes resolve from a per-platform
  envelope registry, are displayed before publishing, and refuse rather than truncate.
- **D-12 (new) — Platform set**: Reddit joins with text/link and a community target model; Rumble
  joins as integration-uncertain behind a capability flag.
- **D-13 (new) — Live is phase 2**: read-only status aggregation for Twitch/Kick first; restreaming
  is a relay feature.
- **D-8 (amend)**: remove "whether PyPI keeps the name `xpst`" from the undecided list — §2.6
  answers it: not on the critical path, claim the name cheaply, keep the Python package for one
  cycle as the escape hatch only.

---

## 8. Open questions for the maintainer (each with a recommended default)

1. **Relay media: do we host it, or does the user?** Recommended: relay hosts and deletes after
   N days (7 is enough for every platform's ingest window); a "bring your own bucket" option can
   come later. Without hosting, Threads media stays impossible.
2. **Pricing**: free binary + per-account relay with a free tier (2–3 accounts) — is a free tier
   acceptable, or should the relay be paid from day one? Recommended: free tier, because the free
   tier is how the local story gets discovered.
3. **Reddit app ownership**: one project-owned Reddit app for everyone (recommended; no review gate)
   versus BYO. Either way the subreddit list is per-user.
4. **YouTube long-form**: add an explicit mode (recommended) or keep Shorts-only and declare
   long-form unsupported until later? Shorts-only makes the "long-form → YouTube" requirement false.
   *Settled by #339 in the recommended direction: explicit `--shorts`/`--long` with a real long-form
   upload path.*
5. **Rumble appetite**: is "API path when it works, manual handoff otherwise" acceptable for v1, or
   should Rumble wait until a live upload can be proven? *#339 shipped exactly that split, flagged
   `integration_uncertain`; the manual handoff is the default until a live upload proves the key
   path.*

---

## 9. Work this implies (ordered, small)

| # | Work | Why now |
|---|---|---|
| 1 | Kill the "default: all" semantics; add `skipped_unconnected` / `unsupported_format` result states | The hard requirement; smallest change with the largest honesty gain |
| 2 | Capability-driven route resolver + `preflight --format auto` plan output | The core value; also the surface the UI and scheduler share |
| 3 | YouTube Shorts/long-form mode + X duration contract | Requirement 4 is currently false in 2 of 3 rows |
| 4 | Reddit adapter (text/link first, subreddit target model) | Requirement 2 |
| 5 | Rumble capability flag + manual fallback | Requirement 4 without over-promising |
| 6 | Threads media via relay (or user-supplied public URL) | The only requirement a binary cannot satisfy |
| 7 | Relay: media hosting + scheduled execution (Phase H) | The paid product |

---

## Appendix — evidence commands

```bash
# capability truth for every destination (the source of §5.1)
xpst capabilities

# BYO burden
rg -n '^\s+"[a-z]+": ByoPlatformSpec' src/xpst/byo.py

# "default: all" semantics
rg -n '"--platforms"' src/xpst/cli.py

# unattended execution requires a supervised daemon
sed -n 1,20p src/xpst/serve.py
```
