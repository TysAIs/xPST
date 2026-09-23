# xPST vs. the competition — why we win

_Last updated 2026-08-24. Purpose: position xPST against paid cross-posting SaaS so
the README and docs can speak to a "beat the incumbents" story with facts, not hype._

## The field

| Product | Price | Open source? | Platforms | Auth | Agent/API surface |
|---|---|---|---|---|---|
| **xPST** | **Free, MIT OR Apache-2.0** | **Yes** | YT, X, IG, TikTok, Threads, Messenger | Mixed: official APIs and user-owned sessions; current status varies | **CLI + MCP (40 tools) + FastAPI dashboard + desktop (optional)** |
| **Content360** | $67 lifetime / "$47/mo soon" | No | ~15 (mostly via Zapier-style) | Closed SaaS OAuth | None (web app only) |
| **Postiz** | Free self-host / cloud | AGPL | 9-15 | Official OAuth | Web UI, API |
| **Repurpose.io** | $35/mo | No | 4-5 | Closed SaaS | None |
| **Buffer / Later** | $18-35/mo | No | 3-6 | Closed SaaS | API limited |
| **ManyChat** | ~$15-25/mo | No | IG/FB Messenger | Closed SaaS | None for agents |

## Where xPST wins outright

1. **Truly open + free.** Content360 is $67 lifetime (one-time price anchors the value of what we give away), Postiz is AGPL, Repurpose/Buffer are subscriptions. xPST is MIT OR Apache-2.0 — you own it, fork it, sell services around it.
2. **AI-agent-native.** 40 MCP tools (`xpst_post`, `xpst_suggest_caption`, `xpst_health`, `xpst_analytics`, scheduling incl. cancel, targeted failure retry, KB, transcripts, search). **No competitor ships an MCP server** — this is the "for AI agents" moat.
3. **Auditable local credentials.** The live-verified paths use sanctioned APIs
   or user-owned sessions as documented. TikTok destination publishing is
   pending external review, and Threads/Messenger are disabled/unauthenticated;
   see [INSTALL.md](INSTALL.md#capability-truth-table) rather than assuming all
   listed integrations are ready.
4. **Encrypted credential-store values + optional OS keychain.** xPST keeps
   credential-store values locally in Fernet/scrypt `.enc` files by default or
   in the OS keychain when enabled; platform-specific token/session files remain
   owner-only and the whole `~/.xpst/` directory is sensitive.
5. **Cross-platform packaging paths** (macOS/Windows/Linux, Docker, PyInstaller)
   exist, but each published artifact and platform lane still needs its own
   verification; see [INSTALL.md](INSTALL.md).
6. **1524 tests, enterprise-hardened** — thread-safe state, circuit breakers, anti-bot jitter, rate-limit calendars, MCP security hardening.

## Features where we're now parity-or-better (built 2026-08-24)

- **Provider-specific connect paths** — `xpst connect x/instagram/tiktok` cover the documented account/source flows; TikTok destination setup remains pending external review.
- **Messenger auto-reply** (ManyChat-lite) — keyword `reply_rules` + auto_reply flag on IG/FB Messenger DMs.
- **Comment auto-reply** — IG/FB comment fetching + keyword reply via Graph API (`xpst messenger check-comments`).
- **Link-in-Bio builder** — self-hosted `/bio` page from enabled accounts + custom links (`xpst bio`).
- **AI content studio** — `xpst generate caption|ideas` (deterministic fallback + LLM via `XPST_KB_LLM_*`).
- **Analytics dashboard** — cross-platform views/likes/comments/shares, best-time-to-post.

## Honest gaps vs. Content360 (their "trust" moats)

- **24/7 live-chat + private community** — they sell support + social proof ("20,000+ creators"). OSS counters with: GitHub issues/discussions + transparent source. A "trusted by" line is marketing; test counts and open code are verifiable.
- **"Zero technical knowledge" onboarding** — their whole pitch. Our counter: the desktop app + `xpst connect` wizards + quickstart docs. This is where UX polish matters most.
- **Unlimited platforms/accounts** — we support 6-7 natively; theirs "15+" is mostly shallow integrations. Breadth vs. depth: ours are deep (official APIs + re-encode + analytics).

## Positioning line for README

> "The open-source, self-hosted, agent-native content engine. Post to YouTube, X,
> and Instagram with official OAuth, source video from TikTok (TikTok publishing
> awaits external app review — see the INSTALL.md capability truth table); let AI
> agents drive it over MCP; keep your tokens encrypted on your own machine.
> Free forever — or self-host it on hardware you already own."
