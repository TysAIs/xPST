# xPST vs. the competition — an honest teardown

_Last updated 2026-11-06. Purpose: position xPST against paid cross-posting SaaS with
facts we can defend. Prices in the field table were read 2026-08-24 and have not been
re-verified since; the access and architecture claims were re-read on 2026-11-04/06
against vendor docs and this repository._

The competitive field splits along an axis most comparisons hide: **who owns the
developer app.** A hosted product registers one Meta/TikTok/X app, absorbs the review
and pays the quota, and sells that convenience. A self-hosted product leaves the app —
and its review — to the user. That single difference explains most of the feature gap
and all of the onboarding tax.

## The field

| Product | Price (2026-08-24) | Open source? | Platforms claimed | Access reality | Agent/API surface |
|---|---|---|---|---|---|
| **xPST** | **Free, MIT OR Apache-2.0** | **Yes** | 6 publish destinations shipped (YouTube, X, Instagram, Threads, Facebook, TikTok) + Messenger; 12 more designed in the matrix | Mixed, per destination and **stated**: public API, gated API, browser-assisted or manual handoff | **CLI + MCP (42 tools) + HTTP API + desktop (optional)** |
| Content360 | $67 lifetime / "$47/mo soon" | No | ~15, mostly via Zapier-style relays | Closed SaaS OAuth through their apps | None (web app only) |
| Postiz | Free self-host / cloud | AGPL | 9–15 | Their app, their review | Web UI, API |
| Repurpose.io | $35/mo | No | 4–5 | Their app | None |
| Buffer / Later | $18–35/mo | No | 3–6 | Their app | Limited API |
| ManyChat | ~$15–25/mo | No | IG/FB Messenger | Their app | None for agents |

## What the teardown actually shows

1. **"15+ platforms" is a count of buttons, not of public posts.** Every product in
   this table that posts to Instagram, Threads, Facebook, TikTok or Pinterest is
   operating a Class B integration: professional account, business page, app review,
   compliance audit, or a sandbox that only the author can see (see
   [PLATFORM-CAPABILITY-MATRIX.md](PLATFORM-CAPABILITY-MATRIX.md) §2). What differs is
   *who carried the gate*. Hosted vendors carry it; self-hosted ones push it to the
   user; most marketing pages do not mention it at all.
2. **That is the hosted tier's real product.** A paid hosted xPST is worth paying for
   not because the binary is hard to run, but because one reviewed app, one media host
   and one quota arrangement replace every user doing that paperwork alone. It is also
   the hosted tier's hardest constraint: YouTube's `videos.insert` bucket is **100
   uploads/day per project**, so one shared project cannot serve many tenants.
3. **Nobody in this field is agent-native.** The 42-tool MCP server is still the moat:
   every competitor here is a web app a human drives, with a REST API bolted on for
   some of them. An agent can plan, post and reconcile against xPST without a browser.
4. **Nobody is honest per destination.** The differentiator is not breadth; it is the
   report. A row that says "published privately pending audit", "deferred until
   14:20 — provider window", "submitted, unconfirmed" or "prepared, finish in the app"
   is worth more to someone who posts every day than a green checkmark that lies.
5. **Phone-first without a store is a real wedge for a solo operator**, and a real
   weakness against teams: a PWA has no push-to-app-store credibility, no share-target
   on iOS, and no background upload. We claim the first and state the second.

## Where xPST wins outright

1. **Truly open and free.** MIT OR Apache-2.0 — you own it, fork it, sell services
   around it. No seat pricing, no per-platform tier.
2. **AI-agent-native.** 42 MCP tools (`xpst_post`, `xpst_preflight`, `xpst_health`,
   `xpst_analytics`, scheduling incl. cancel, targeted failure retry, KB, transcripts,
   search). No competitor ships an MCP server.
3. **Credentials stay yours.** Encrypted credential store locally (Fernet/scrypt by
   default, OS keychain opt-in); tokens never leave the operator's machine unless the
   destination requires it.
4. **The promise is generated from code, not written by marketing.** The capability
   matrix and the shipped capability document are the same object, and a test fails
   when they drift.
5. **Rate-limit safety as a designed system**: persistent cooldown state machine, one
   verdict per limit, refusal over truncation, retry ownership, a counted dry run that
   must be green before posting is un-paused.
6. **Full test suite in CI, enterprise-hardened** — thread-safe state, circuit
   breakers, anti-bot jitter, MCP security hardening (fail-closed mutation guards).

## Features at parity or better (built 2026-08-24)

- Provider-specific connect paths (`xpst connect …`) for the documented account and
  source flows.
- Messenger auto-reply (ManyChat-lite) and comment auto-reply via Graph.
- Link-in-Bio builder (`/bio`).
- AI content studio: `xpst generate caption|ideas`.
- Analytics dashboard with cross-platform metrics and best-time-to-post.

## Where xPST honestly loses

| Gap | Against whom | What it costs | Position |
|---|---|---|---|
| No shared reviewed app | Every hosted vendor | BYO developer app + review on first run per platform | The onboarding tax the MVP must shrink; the hosted tier is where it disappears |
| No native mobile app yet | Buffer, Later, Hootsuite | Store presence, share-target, background upload | PWA first by design; wrapper evaluated after the service works |
| No team/workspace features | Hootsuite, Buffer | No multi-seat approval flows | "Team/multi-account workspaces" stays under consideration |
| No 24/7 support or community | Content360's pitch | Buyers who want a human | OSS answers with source and tests; paid support is a legitimate later layer |
| No content calendar / bulk scheduling | Later | Planned, not shipped | Not a phone-first blocker |
| Breadth | Everyone's marketing | 6 shipped vs "15+" claimed | Depth + honesty vs. button count |

## Business model (not a blocker)

The open-source core is the default and stays that way. The layers, in the order they
become defensible:

1. **Open core, free forever** — the binary, the CLI, MCP, the web UI.
2. **Hosted tier** — the reviewed app, the public media host, scheduled uptime, and
   per-tenant isolation. This is the layer where a competitor's moat becomes our
   feature. It needs multi-user accounts, billing and stronger isolation; the seams
   are recorded in [PHONE-FIRST-UX.md](PHONE-FIRST-UX.md) §6 so no build decision
   blocks on it.
3. **Pro features or support** — analytics depth, larger destination sets, priority
   support. Priced only after the hosted cost model is measured (per-tenant quotas are
   the dominant variable).

Nothing in the MVP depends on which of these is chosen.

## Positioning line for README

> "The open-source, self-hosted, agent-native content engine. Post from your phone,
> route by the criteria you set, and get back what each platform actually said —
> including the honest refusals. Free forever — or self-host it on hardware you
> already own."
