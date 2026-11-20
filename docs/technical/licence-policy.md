# Licence policy and what we may reuse

xPST is distributed as a compiled binary under `MIT OR Apache-2.0`. That decides
what code may enter the tree. This policy exists because a wrong licence call is
a legal problem that no amount of code review catches later.

## Accept

The accepted list, one identifier per line, is the machine-readable half of this
policy: `deny.toml` allows exactly these, `scripts/check_licences.py` enforces
exactly these, and `crates/xpst-core/tests/licence_gate.rs` fails if this list,
that file and that script disagree.

- `MIT`
- `Apache-2.0`
- `Apache-2.0 WITH LLVM-exception`
- `BSD-2-Clause`
- `BSD-3-Clause`
- `ISC`
- `Zlib`
- `Unlicense`
- `CC0-1.0` — public-domain dedication, strictly less restrictive than MIT (tiny-keccak, through the Tauri stack)
- `Unicode-3.0` — permissive, MIT-like; unavoidable transitively through `unicode-ident`, which `proc-macro2`/`syn` require
- `CDLA-Permissive-2.0` — certificate **data** only; it must be named in `NOTICE`

`MPL-2.0` only as an unmodified separate crate; if we patch it, we publish the
patch and we never vendor MPL code into our own modules. Today's users are
`cssparser`, `cssparser-macros`, `dtoa-short`, `option-ext` and `selectors`,
reached through the Tauri/WebKit stack in `src-tauri/`.

An SPDX expression is satisfied when it can be met using only allowed licences:
`MIT OR GPL-3.0` is acceptable (we take MIT), `MIT AND GPL-3.0` is not.

## Reject

`GPL-2.0*`, `GPL-3.0*`, `AGPL-3.0*`, statically-linked `LGPL-*`, commercial
`LicenseRef-*`, and any crate whose licence field says `non-standard` until a
human has read the file.

## Enforcement

What runs, today:

1. **`deny.toml` + `cargo-deny`, in CI.** The `Licence policy` job in
   `.github/workflows/ci.yml` runs `cargo deny check licenses bans sources
   advisories` over the engine workspace **and** over `src-tauri/Cargo.toml`
   (the shell has its own lockfile), and fails the build on anything outside the
   accept list, an unknown registry or git source, or a yanked crate.
2. **`scripts/check_licences.py`, on every machine that has Cargo.** Same
   accept list, no extra binary: it reads the committed `Cargo.lock`, takes each
   locked package's licence from the metadata Cargo already cached
   (`cargo metadata --offline --locked`), evaluates the SPDX expression and
   exits non-zero on a violation. The same CI job runs it for both lockfiles, so
   the policy is testable even where `cargo-deny` is not installed.
3. **A drift guard.** `crates/xpst-core/tests/licence_gate.rs` fails when this
   document's accept list, `deny.toml`'s `allow` list and the script's accepted
   set stop agreeing, and when the CI job loses either mechanism. Three lists
   that must agree are three lists that will disagree; the test is what makes
   the disagreement visible at review time instead of at licence-audit time.
4. **The advisory half of the same job: a decision per ignore.**
   `deny.toml`'s `[advisories] ignore` list is where a RustSec advisory that
   cannot be cleared is recorded, and every entry there carries a reason and a
   review date. `docs/technical/security-advisories.md` is the long form — the
   crate, whether the advisory is reachable from what this repository builds, the
   condition that removes the ignore. `crates/xpst-core/tests/advisory_policy.rs`
   fails when the two disagree, when an ignore loses its reason or date, or when
   `xpst-mcp` enables a transport an ignored advisory assumes is absent. A gate
   that only counts licences says nothing about the code that carries them.

What is **pending**: `cargo-about` NOTICE generation. There is no `NOTICE` file
in the tree yet, so the promise "CDLA-Permissive-2.0 must be named in NOTICE"
is not yet backed by a generated artifact. Until it is, that licence's use is a
manual review item, not a gated one. Nothing else in this section is pending.

A direct dependency being clean says nothing about what its next minor release
pulls in — which is why the gate is on the lockfile and runs on every pull
request, not on a release checklist.

## FFmpeg — the one that bites

The crates are permissive; **FFmpeg itself is not**. It is
`LGPL-2.1-or-later`, and becomes `GPL-2.0-or-later` when built with
`--enable-gpl` — which is what nearly every prebuilt binary is, because it
includes x264.

Therefore:
- We never ship FFmpeg inside our artifacts.
- We never auto-download it at runtime (that would distribute or induce a GPL
  binary and introduce an unpinned supply chain).
- We invoke a user-provided or separately installed `ffmpeg`/`ffprobe` as child
  processes. Process boundary, not linkage.
- If we ever ship one ourselves, it is an LGPL-only build (no x264, no
  `--enable-gpl`) with the source offer, as a separate download.

## What may be taken, and what may only be read

Verified from each repository's LICENSE file, 2026-11-17.

**Depend on (permissive):**

| Project | Licence | Use |
|---|---|---|
| `robotty/twitch-irc-rs` (`twitch-irc` 6.1.3) | MIT | Twitch IRC + IRCv3 tags, as a dependency |
| `aoyagikouhei/twapi-v2-rs` | MIT | X chunked v2 media upload + OAuth 1.0a signing |
| `AnisAbdellatif/kicksdk` | MIT OR Apache-2.0 | Kick realtime (**Centrifugo**, the current transport) |
| `PirateTok/live-rs` | ISC-style | TikTok live protobuf decoding |
| `Chatterino/chatterino2` | MIT | Emote/badge provider endpoints and ID mapping (port, don't transliterate) |
| `lemolatoon/youtube_chat_rs` | MIT | YouTube live chat transport |
| `atrium-rs/atrium` | MIT | AT Protocol / Bluesky |

Every other crate we add follows the inventory recorded alongside this policy,
with its licence verified from the published crate metadata rather than assumed.

**Read for design only (copyleft — never copy code):** Postiz (AGPL-3.0),
Social Stream Ninja (GPL-3.0), TikTokLive Python (AGPL-3.0),
TikTok-Live-Connector (declared AGPL-3.0-only), all-chat (AGPL-3.0),
streamlink (BSD-2, legally copyable but a Python plugin runtime we do not want).

**Dead or unusable despite a permissive licence:**
- `Landy-Dev/kick-api` — MIT, but its `live_chat.rs` targets a Pusher app key
  Kick **decommissioned on 2026-10-13** when it moved to Centrifugo. Do not
  build on it.
- `jwdeveloper/TikTokLiveRust` — archived 2024-08, pre-v3 webcast schema.
- `taizan-hokuto/pytchat` — MIT but archived 2021; YouTube continuation params
  moved on.
- `it-coder/streamget-rs`, `museun/twitchchat` — no LICENSE file in the repo; a
  registry declaration is not a licence grant.

## The gap worth knowing

There is **no permissively-licensed Rust implementation of Instagram Graph
publishing**. The existing crate covers reading only. That path is written from
the Graph documentation — create container, poll status, publish — which is
small enough to be a day's work, and it is ours to keep correct.
