# xPST vs. the competition — why we win

_Last updated 2026-08-24. Purpose: position xPST against paid cross-posting SaaS so
the README and docs can speak to a "beat the incumbents" story with facts, not hype._

## The field

| Product | Price | Open source? | Platforms | Auth | Agent/API surface |
|---|---|---|---|---|---|
| **xPST** | **Free, MIT OR Apache-2.0** | **Yes** | YT, X, IG, TikTok, Threads, Messenger | **Official OAuth** (ban-safe) | **CLI + MCP (25 tools) + FastAPI dashboard + desktop (optional)** |
| **Content360** | $67 lifetime / "$47/mo soon" | No | ~15 (mostly via Zapier-style) | Closed SaaS OAuth | None (web app only) |
| **Postiz** | Free self-host / cloud | AGPL | 9-15 | Official OAuth | Web UI, API |
| **Repurpose.io** | $35/mo | No | 4-5 | Closed SaaS | None |
| **Buffer / Later** | $18-35/mo | No | 3-6 | Closed SaaS | API limited |
| **ManyChat** | ~$15-25/mo | No | IG/FB Messenger | Closed SaaS | None for agents |

## Where xPST wins outright

1. **Truly open + free.** Content360 is $67 lifetime (one-time price anchors the value of what we give away), Postiz is AGPL, Repurpose/Buffer are subscriptions. xPST is MIT OR Apache-2.0 — you own it, fork it, sell services around it.
2. **AI-agent-native.** 25 MCP tools (`xpst_post`, `xpst_suggest_caption`, `xpst_health`, `xpst_analytics`, scheduling, KB, transcripts, search). **No competitor ships an MCP server** — this is the "for AI agents" moat.
3. **Official OAuth by default** (X API v2, IG Graph API, TikTok Content Posting, YouTube, Threads). Ban-safe. Content360/Postiz rely on the same official APIs but as a black box; ours is auditable + self-hosted so tokens never leave your machine (encrypted CredentialStore).
4. **Encrypted-at-rest credentials + OS keychain.** Paid SaaS hold your tokens server-side; xPST keeps them in a Fernet/scrypt `.enc` (or macOS Keychain) on your own box.
5. **Cross-platform packaging** (macOS/Windows/Linux, Docker, PyInstaller) — runs anywhere, including on a local LLM box with no monthly fee.
6. **1498 tests, enterprise-hardened** — thread-safe state, circuit breakers, anti-bot jitter, rate-limit calendars, MCP security hardening.

## Features where we're now parity-or-better (built 2026-08-24)

- **OEM OAuth connect wizards** — `xpst connect x/instagram/tiktok` open the real authorize pages and auto-verify; no paste-token dance.
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
> Instagram, TikTok, and Threads with official OAuth; let AI agents drive it
> over MCP; keep your tokens encrypted on your own machine. Free forever — or
> self-host it on hardware you already own."
