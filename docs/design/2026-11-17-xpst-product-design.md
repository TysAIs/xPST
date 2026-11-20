# xPST product design — publishing-first, local-first, Rust

Date: 2026-11-17
Status: draft for owner approval
Supersedes nothing. Builds on `docs/DIRECTION.md` (D-3 all-Rust) and
`docs/PROPOSAL-RUST-REBUILD.md` (phase model P0–P6).

## 1. Intent

One local app where a person, or their agent, sends a piece of content once and
it lands correctly everywhere it belongs — decided from the media itself, not by
asking the user to pick destinations by hand. The same app is a beautiful
human UI and a complete agent surface.

The one job it must be excellent at: **publishing and cross-posting across
accounts.** Everything else (analytics, live chat, bio page) is context around
that job, not a competing centre of gravity.

Why now: the product exists as ~86,700 lines of Python that already encode the
correct behaviour — format classification, refusal-over-truncation, cooldown
discipline, honest failure reporting — but ships as a 95 MB interpreter bundle
that is hard to install and impossible to hand to a stranger. The rewrite is how
the same behaviour becomes one small binary on three operating systems.

## 2. Goals

- **Correct by classification.** Vertical short-form → TikTok / Reels / Shorts;
  long horizontal → YouTube / X / Rumble; text → Threads / X / Bluesky. Decided
  by measurement; ambiguous media asks rather than guesses.
- **Refuse, never truncate.** If a destination cannot take the content, say so
  with the number that caused it before any network call.
- **One surface, two audiences.** Every capability the UI has, the agent has.
- **Local-first.** No hosted backend in the default path. Credentials stay on
  the user's machine, in the OS keychain where possible.
- **Small and fast.** Single binary, no interpreter, cold boot ≤ 1 s, install
  ≤ 30 MB without media tools.
- **Mac, Windows, Linux.** Same behaviour, real installers on all three.

## 3. Non-goals

- Restreaming or re-broadcasting other people's live video.
- Being a hosted service or holding anyone else's tokens.
- LinkedIn (its terms forbid the credential model this app requires).
- Scraping as a default strategy. Unofficial paths exist per platform, are
  labelled in the UI, and can be switched off.

## 4. Anti-goals (failures even if it technically works)

- A surface that reports success the platform did not confirm.
- Silent mutation of the user's caption, or of any published copy.
- Unbounded retry that spends a user's quota or gets an account limited.
- An onboarding that requires reading a developer console without being told
  exactly why.

## 5. What the research established (constraints we cannot design away)

1. **TikTok's official API is closed to this product class.** Its Content
   Sharing Guidelines list as *not acceptable* "an app that copies arbitrary
   contents from other platforms to TikTok" and "a utility tool to help upload
   contents to the account(s) you or your team manages". Unaudited clients are
   capped at 5 users/24h and `SELF_ONLY`. → TikTok is a **manual handoff**
   (prepare the file + metadata, open the composer), not an API integration.
2. **Instagram cannot be published to without a public media URL** for images,
   and Threads for both media kinds. Meta fetches the file. Video has a
   resumable path that does accept a local file. → IG/Threads need either a
   user-supplied host or an explicit unofficial mode.
3. **X has no free tier.** $0.015/post, $0.200/post containing a URL. Its
   OAuth does not need a hosted backend: the official `xurl` client completes
   the flow headless by printing the authorization URL and reading the pasted
   redirect back, so a local app can hold its own X app identity. → never
   append links by default; show cost.
4. **Rumble has no public publishing API at all.** → manual handoff.
5. **Bluesky is the only platform that is fully local-first**: app password, no
   developer app, no review, no redirect URI, no hosted media.
6. **Live aggregation is real but narrower than it looks.** Twitch: buildable
   now (anonymous IRC). YouTube: buildable, quota-bound, cheap via the
   streaming endpoint. Kick: unofficial websocket only. **TikTok Live and
   Instagram Live cannot be done officially** — no chat API, no live embed.
7. **Competitors confirm the model.** Postiz and Mixpost self-host with
   bring-your-own developer apps; neither has a rate-limit trick — both queue,
   honour `Retry-After` and cap per-platform concurrency. The only cookie user
   is Postiz for Skool, a platform with no public API at all.

## 6. Approaches considered

**A. Pure local, bring-your-own developer apps.** Free forever, nothing leaves
the machine. Cost: 20–90 minutes in consoles per user, IG/Threads still need a
public URL, TikTok still manual.

**B. Local + one small maintained broker** for the flows that cannot be local
(TikTok code exchange, IG media hosting). Cost: ~$5/month and the maintainer
becomes a token handler — the thing that makes a free project expensive.

**C. Hosted proxy for everything (Ayrshare model).** Best onboarding, worst
cost and liability; the maintainer is the OAuth counterparty for every user.

**Chosen: A, with B available per-platform as an opt-in the user configures.**
It is the only model that keeps the maintainer cost at zero, honours
local-first, and never puts someone's credentials on someone else's server.
C is explicitly rejected.

## 7. The design

### 7.1 Shape

One Rust workspace, one `xpst` binary, a Tauri 2 shell and the existing web UI.
Crates follow the P0 map; provider adapters are separate crates so a broken
platform cannot take the engine down.

### 7.2 Publishing pipeline (the core)

1. **Ingest** — local file, or a URL handed to the source adapters.
2. **Measure** — ffprobe: duration, dimensions, codec, bitrate, audio.
3. **Classify** — `classify_video_mode` returns short/long/**ambiguous**;
   ambiguous asks. Already ported to `xpst-core` with a 447-row parity fixture.
4. **Plan** — per destination: accept or refuse with a stable code, the numbers
   that caused it, the transform that would be applied, and any advisory.
5. **Confirm** — the plan is frozen (media digests, caption, destinations) and
   is what gets published; a changed file invalidates it.
6. **Publish** — per destination, idempotent, with a cooldown state machine and
   bounded retry. A deferred destination stays retryable; a published one never
   re-publishes.
7. **Verify** — read back where the platform allows it (`verified`,
   `submitted_unconfirmed`, `not_found`); never claim what was not confirmed.

### 7.3 Destination policy (per platform, from the research)

| Destination | Path | Gate |
|---|---|---|
| YouTube | official, loopback OAuth | 100 uploads/day per project; OAuth verification to serve the public |
| Bluesky | official, app password | none — reference implementation |
| X | official, headless OAuth | paid per post; no links by default |
| Instagram | official + public URL, or explicit unofficial session mode | Meta fetches media |
| Threads | official + public URL | text and media both fetched |
| Facebook Pages | official + hosted redirect | Page token, review for other people's Pages |
| TikTok | **manual handoff** | official API structurally excludes cross-posters |
| Rumble | **manual handoff** | no public API |
| LinkedIn | **not supported** | terms forbid BYO credentials |

### 7.4 Agent surface

MCP tools mirror the CLI one-for-one; every mutating tool is consent-gated by
default; read-only tools never require consent. The agent must be able to do
everything the UI can, and nothing the UI cannot.

### 7.5 Live (deferred, scoped honestly)

Chat-first aggregation: Twitch (buildable now), YouTube (quota-aware), Kick
(unofficial, feature-flagged, visibly marked). **TikTok Live and Instagram Live
are out of scope**, surfaced as "open in browser" rather than an empty pane.
Video panes use each platform's official embed, never a native stream pull.

### 7.3a Two modes per platform: official and direct

Where an unofficial path exists (X via the web session, Instagram via
instagrapi, TikTok via browser publish, YouTube Studio via browser automation),
it ships as a **direct mode**: off by default, labelled in the UI with what it
costs the user (terms of service, account risk, and that it can break without
notice), and switchable per platform.

This is the honest version of "can't we just get around the API": yes, and we
already do it — but a distributed product cannot make violating platform terms
its default behaviour, because every break is a support incident and every
challenge is the user's account at risk. Direct mode is a deliberate choice a
user makes for their own accounts on their own machine, not something the
software does quietly.

The practical consequence: official is the default and the supported path;
direct mode exists, is documented, and is never silently enabled.

## 8. Decisions I am making as engineering lead

- TikTok and Rumble ship as manual handoff. A fabricated integration is worse
  than an honest handoff.
- IG/Threads and X support two modes: official (IG/Threads require a public
  file URL the user supplies) and direct/unofficial (explicitly labelled, off
  by default, switchable per platform).
- No shared platform credentials anywhere in the repo or the release artifacts.
- X default: do not append links. Cost is shown before publishing.
- The Python engine remains the shipped product until Rust parity is proven per
  slice; nothing is deleted early.

## 9. What is left to the builder

- Packaging/signing: Apple membership is the only unavoidable cost ($99/yr);
  Windows via SignPath Foundation or Certum; Linux free.
- The exact wording of the onboarding steps per developer console.
- Whether a future paid tier funds a TikTok broker. Not required for v1.

## 10. Open questions for the owner

1. Instagram/Threads: is a "public URL for media" step acceptable, or should the
   unofficial session mode be the default until Meta changes?
2. Live aggregation: ship Twitch + YouTube + Kick only, or wait until there is
   a maintainable path for TikTok/Instagram?
