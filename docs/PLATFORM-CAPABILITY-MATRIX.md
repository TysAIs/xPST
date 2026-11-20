# Platform capability matrix

**Status**: reference — vendor rows checked 2026-11-04 (live-ingest rows 2026-11-16);
the "shipped" rows are generated from `xpst capabilities` (contract v1, engine
`a624057`), the live-ingest rows from `xpst.stream.destinations`, and both are
asserted by `tests/test_platform_capability_matrix.py`.
**Purpose**: decide, per platform, what xPST is allowed to promise. Every routing
verdict, readiness badge, refusal reason and error message derives from this
document. A platform xPST has not proved against its documented API is *not*
"supported".

This matrix is deliberately pessimistic. It records what the vendor documented on
the check date, not what a blog post claimed or what the engine happened to make
work once.

Two questions are answered separately, because conflating them is how products end
up lying:

1. **What does the platform allow?** (§2) — vendor-documented write access and the
   gate in front of it.
2. **What does xPST ship today?** (§3) — the declared-vs-implemented content
   contract the engine actually serves.

A destination is only "supported" when both answers are non-empty *and* the
gate in front of it is satisfied for that account. Everything else is a named
refusal, a browser-assisted route, or a manual handoff.

---

## 1. Access classes

| Class | Meaning | What xPST may claim |
|---|---|---|
| **A — public posting API** | Documented endpoint, self-serve credentials, no audit for public visibility. | "Post from xPST." |
| **B — restricted / partner API** | Documented endpoint, but visibility or eligibility is gated by review, audit, business verification or account type. | "Post from xPST **if** the stated gate is satisfied." |
| **C — browser-assisted** | No usable posting API for the account type; xPST drives a logged-in web session. | "xPST publishes through your browser session (unofficial)." |
| **D — manual handoff** | No write API at all. xPST prepares the post and hands it to a person or a share sheet. | "xPST prepares this; you finish it in the app." |

A platform can occupy two classes at once (TikTok is A for inbox drafts, B for
public direct post, C as the practical fallback). The matrix records the split
instead of picking a flattering average.

## 2. Vendor access classification and gates

The rows marked as a live-ingest surface (`YouTube Live`, `Twitch`, `Kick`,
`Facebook Page Live`, `Instagram Live`, `TikTok LIVE`, `Rumble Live`) classify
**ingest**, not posting. The letters keep their §1 meaning: **A** = a documented
endpoint with self-serve credentials, **B** = a documented endpoint whose access is
gated by review or account eligibility, **D** = no endpoint at all, so a human
copies a per-session key out of a console. Posting classes and ingest classes are
different questions even on one platform — Instagram posts through a Class-B Graph
API and only streams through a Class-D console.

| Platform | Class | Documented write path | Gate | Evidence |
|---|---|---|---|---|
| YouTube | **A** | `videos.insert` (Data API v3, OAuth) | Own Google Cloud project + OAuth client; app verification affects audience breadth | Google quota calculator, updated 2026-10-15 |
| X | **A (paid)** | `POST /2/tweets` + media upload | Developer app and purchased credits; no free tier to rely on | X API pricing, fetched 2026-11-04 |
| Bluesky | **A (open)** | `com.atproto.repo.createRecord` (`app.bsky.feed.post`) | App password or OAuth session; no review, no fee | Bluesky protocol docs, fetched 2026-11-04 |
| Mastodon | **A (open)** | `POST /api/v1/statuses` per instance | OAuth app on the target instance; instance rules and limits | Mastodon API docs, updated 2026-07-29 |
| Telegram | **A** | Bot API `sendMessage` / `sendPhoto` / `sendVideo` | A bot, added to the channel/group with posting rights. **Cannot post to a personal profile** | Telegram Bot API (Bot API 10.3, 2026-08-24) |
| Discord | **A** | Webhook execute or bot `POST /channels/{id}/messages` | A webhook or bot in a server channel. **No direct messages to arbitrary users** | Discord webhook resource, fetched 2026-11-04 |
| Reddit | **A (rate-limited)** | `POST /api/submit` (OAuth) | OAuth client; non-commercial rate tier; subreddit rules are the real gate | reddit.com/dev/api, fetched 2026-11-04 |
| LinkedIn (member) | **A** | `POST /rest/posts`, `w_member_social` | "Share on LinkedIn" product, self-serve, no review | LinkedIn Posts API, updated 2026-05-13 |
| LinkedIn (company page) | **B** | Same endpoint, `w_organization_social` | Community Management API approval, weeks-long review | LinkedIn Posts API permissions table |
| Instagram | **B** | Container → `media_publish` (Graph) | Instagram professional account + Meta app; advanced access for users outside app roles | Meta content-publishing docs, fetched 2026-11-04 |
| Threads | **B** | Container → `threads_publish` | Meta app + `threads_content_publish`; same review burden | Meta Threads overview, updated 2025-12-22 |
| Facebook Page | **B** | `POST /{page-id}/feed`, `/photos`, video upload | Page only (never a personal profile), `pages_manage_posts`, app review | Meta Pages API, updated 2026-04-17 |
| Messenger (Page DM) | **A** | `POST /me/messages` (Graph) | Page access token + messaging permission; the unit is a **conversation**, not a post | Meta Messenger Platform docs |
| TikTok (direct post) | **B** | `post/publish/video/init` with `DIRECT_POST` | `video.publish` scope **and a compliance audit**; unaudited clients publish private-only | TikTok Content Posting API guide, updated 2026-08-04 |
| TikTok (inbox/draft) | **A** | Same API, inbox endpoint | No audit; the creator finishes in the app | Same guide |
| TikTok (browser) | **C** | Playwright against TikTok Studio | Logged-in browser profile; unofficial | Shipped in this repo (`publish_mode: browser`) |
| Pinterest | **B** | `POST /pins` | Trial access = pins visible only to the creator; Standard access needs review + demo video | Pinterest access tiers, fetched 2026-11-04 |
| Snapchat (Public Profile) | **B** | Marketing API Public Profile Content Management | Business Manager, Public Profile, partner access | Snap developer docs, fetched 2026-11-04 |
| Snapchat (regular account) | **D** | Creative Kit share sheet | No server-side post API for normal accounts | Snap developer docs |
| Rumble | **D, shipped as handoff** (a **B/C** partner path exists when a key is configured) | Partner upload API, or the engine's manual-handoff path (rendition + caption file written for you) | Partner-gated key with no self-serve developer console; the engine's own verdict is `integration_uncertain` until one live upload succeeds | `docs/INSTALL.md`, `xpst.media.specs`, `xpst capabilities` §3 |
| Substack | **D** | No documented public write API | Manual handoff | *Unverified* |
| Nostr | **A (open)** | Signed events to relays | Keypair per identity | *Candidate, not yet verified* |
| Google Business Profile | **B** | Posts API | Business account + approval | *Candidate, not yet verified* |
| YouTube Live | **A** | `liveStreams.insert` (Live Streaming API; `youtube` or `youtube.force-ssl`) | Google Cloud project + OAuth client on the destination channel. The channel must additionally be enabled for live streaming — the authenticated call made on 2026-11-16 was answered `403 The user is not enabled for live streaming` | liveStreams.insert reference, *last updated 2026-10-15*; checked 2026-11-16 |
| Twitch | **A** | `GET helix/streams/key` | Twitch app + a **user** access token carrying `channel:read:stream_key`, whose user id must equal the `broadcaster_id`; an app access token cannot read a stream key | Twitch API reference, *Get Stream Key*; checked 2026-11-16 |
| Kick | **A** | `GET public/v1/channels` → `data[].stream.key` / `.url` | Kick app + a user token carrying `streamkey:read`. Kick publishes **no** standalone stream-key endpoint; the key is a field on the channel resource | Kick Dev *Scopes* and *Channels*; checked 2026-11-16 |
| Facebook Page Live | **B** | `POST /{page-id}/live_videos` (Live Video API) | **App Review for the Live Video API feature** plus `pages_manage_posts` / `pages_read_engagement`; the Facebook account must be ≥ 60 days old and the Page or professional-mode profile ≥ 100 followers; the app must produce an RTMPS stream | Meta *Live Video API*, updated 2026-07-02; checked 2026-11-16 |
| Instagram Live | **D** | No endpoint. Live Producer on instagram.com issues a per-session RTMP URL + key | Public account with ≥ 1,000 followers; the key refreshes on every use, and Live Producer is announced as limited access and instagram.com-only | Instagram Help Center, *Start a live video on Instagram*; About Instagram, *Instagram Live Producer* (2022-11-02); checked 2026-11-16 |
| TikTok LIVE | **D** | No endpoint. The creator's LIVE Center / Live Studio issues a per-session RTMP URL + key | 18+ and 1,000 followers to go LIVE (TikTok says this may vary by region). Third-party RTMP keys are not documented on the public help center — the surface is behind the creator login | TikTok Support, *What is TikTok LIVE?*; checked 2026-11-16 |
| Rumble Live | **D** | No endpoint. *Get the Streamer Configuration* in the live setup shows the RTMP URL + key | Account with live access; each new stream gets its own key unless a static key was configured | Rumble FAQ, *How to Setup a Livestream*, updated 2025-12-05; checked 2026-11-16 |

## 3. What xPST ships today

The **destination table** is generated from the same document the CLI
(`xpst capabilities`), the MCP tool (`xpst_capabilities`) and
`GET /api/capabilities` return. It is **test-enforced**: if the engine's contract
changes, this section fails CI until it is updated. Do not edit it by hand without
re-running `xpst capabilities --json`.

The **live-ingest tables** further down are the same kind of artifact for the
stream pillar: they are asserted against `xpst.stream.destinations`
(`STREAM_INGEST_PATHS`, `SIMULCAST_REFUSALS`) by the same test, so a platform
gaining, losing or reclassifying an ingest path fails CI until this document says
so. Their gate columns quote §2; their evidence is the vendor URL list in §8.

This is the maintained table. [PROPOSAL-PRODUCT-RESET.md](PROPOSAL-PRODUCT-RESET.md)
§5.1 quotes a per-format snapshot of the same catalog at engine `fc2eadd`; where the
two differ, this section wins because it is generated and pinned.

| Destination | Shipped content types | Media transport | Account gate in the shipped engine | Notes |
|---|---|---|---|---|
| YouTube | video | local upload | OAuth client + token; `enabled` | Two explicit modes, never guessed: short-form (Shorts, ≤ 60 s, square/vertical, `#shorts`) and long-form (resumable `videos.insert`, watch URL, ≤ 12 h / 256 GB) |
| X | video, image, carousel, text | local upload | `auth_mode: cookies` (twikit session) or API v2 | Carousel = one media item per tweet, in order; text capped at 280 chars |
| Instagram | video, image, carousel | local upload | Graph token + professional account; `enabled` | The image path needs a public image URL and refuses a local file explicitly |
| Threads | text, video | **public URL** | Graph token; `enabled` | Video must already be hosted at a public URL; a local file is refused before any network call |
| Facebook Page | video | local upload | Page token; `enabled` | Page only |
| TikTok | video | local upload | Content Posting API app **or** browser profile (`publish_mode: browser`) | No carousel path; unaudited apps publish privately |
| Messenger | text (messaging role, not a public post) | local upload | Page token; `enabled=false` by default | Sends DMs, does not publish |
| Rumble | video | local upload | Partner upload API key when configured; **manual handoff otherwise** (`integration_uncertain`) | Long-form only. The handoff writes the rendition plus a caption file to a chosen directory and reports `handoff`, never a fake upload. Declared, but not advertised as ready until one live upload succeeds |
| Bluesky | text, image, carousel | local upload | Handle + **app password** stored encrypted (AT Protocol `createSession`); `enabled` off until connected | Open posting API: no review, no fee, no developer console. Text is capped at 300 characters and links are faceted so they are clickable; a carousel is a native 2–4 image embed in one post (never stitched into a video). **No video**: that path is a separate service with an asynchronous job, and xPST does not claim it until a live upload proves it |

Not in the catalog at all: Mastodon, Telegram, Discord, Reddit, LinkedIn,
Pinterest, Snapchat, Substack, Nostr, Google Business Profile. Their rows in
§2 are the *design*; nothing in the product may imply they exist until they appear
in this section.

**Known inconsistency to fix, not to paper over**: the Threads row declares
`media_transport: public_url` while Instagram's image path additionally requires a
public URL without declaring it. The routing preview must compute transport per
*destination and content type*, not per destination, or the Instagram image route
will look publishable on a phone that cannot serve a public URL yet.

### Live stream relay — the ingest pillar

What ships is a **local relay**, not a hosting service. An encoder pushes one
RTMP/RTMPS stream to the local MediaMTX process (the relay URL the engine hands
out is `rtmp://127.0.0.1:<port>/<path>`), and MediaMTX forwards the identical
bytes to every configured destination. Nothing is re-encoded, nothing is proxied
through a server xPST operates, no video is uploaded, and no request reaches a
platform unless a key is being fetched. MediaMTX is MIT-licensed and runs as an
**external child process**, on the same footing as `ffmpeg` and `yt-dlp`
(DIRECTION D-3): not vendored, not linked, not bundled.

Keys live in the same encrypted credential store as cookies and tokens —
`stream_destination_<platform>` under the config dir's credential directory,
written `0600`, never in `config.yaml`, never in JSON output, never in `repr()`.
The relay generator writes a MediaMTX YAML whose only enabled surface is RTMP
ingest plus a `forward:` list; RTSP, HLS, WebRTC, SRT, MoQ and the MediaMTX HTTP
API are explicitly off, and the file is created `0600` before any content.

| Platform | Ingest class | Documented ingest path | Gate (from §2) | What the engine does today | Proved live ingest |
|---|---|---|---|---|---|
| YouTube Live | `api` | `POST https://www.googleapis.com/youtube/v3/liveStreams` | OAuth on the destination channel with `youtube` / `youtube.force-ssl`; channel enabled for live streaming | Stores the returned `cdn.ingestionInfo` address and stream name as a relay destination. Creating the resource does **not** start or announce a broadcast — that is a separate `liveBroadcasts` call this engine does not make | **no** — the API call authenticated and YouTube answered `403 not enabled for live streaming`; no ingest has been pushed |
| Twitch | `api` | `GET https://api.twitch.tv/helix/streams/key?broadcaster_id=<id>` | Twitch app + **user** access token with `channel:read:stream_key`; an app token cannot read a key | Fetches the key and pairs it with Twitch's fixed ingest endpoint (`rtmps://live.twitch.tv:443/app`), which is not something the operator has to paste | **no** — no Twitch credentials exist on the hosts this repository runs on |
| Kick | `api` | `GET https://api.kick.com/public/v1/channels?broadcaster_user_id=<id>` | Kick app + user token with `streamkey:read` | Reads `data[].stream.key` and `.url` from the channel resource. Kick has no standalone stream-key endpoint to call | **no** — documented, never exercised; no Kick credentials exist here |
| Facebook Page Live | `api` | `POST https://graph.facebook.com/<version>/{page-id}/live_videos` | **App Review** for Live Video API + `pages_manage_posts` / `pages_read_engagement`; account ≥ 60 days old, Page ≥ 100 followers; RTMPS only | Creates the live video with `published=false`, so fetching a key puts nothing on the Page, and takes the key from the returned secure stream URL | **no** — Facebook is disabled here by the configured Meta pause |
| Instagram Live | `manual` | none — Live Producer issues a per-session URL + key | Public account, ≥ 1,000 followers | Stores what the operator pasted, and says the key must be re-pasted before every stream | **not applicable** — no endpoint exists to prove |
| TikTok LIVE | `manual` | none — LIVE Center issues a per-session URL + key | 18+, 1,000 followers (may vary by region) | Same as Instagram: pasted per session, never assumed reusable | **not applicable** |
| Rumble Live | `manual` | none — studio shows the RTMP URL + key | Account with live access | Same as Instagram; a static key, if the operator created one, makes the destination reusable | **not applicable** |
| X, Threads, Bluesky | `unsupported` | none | — | Never offered as a destination; a stored row for one of these is a refusal, not a silent drop | **not applicable** |

**Pessimism rule, enforced:** no row above says "supported". A platform becomes a
supported live destination only when one real ingest has been pushed *to that
platform* and seen live. The relay proof that exists today is two MediaMTX
destinations receiving the same bytes — that proves the relay, not a platform.

The simulcast prohibitions are data the engine refuses on, not advice:

| Refusal key | Prohibition | Where it stops the product |
|---|---|---|
| `tiktok_shop` | TikTok Shop prohibits simulcasting the same live to other platforms | Unrepresentable as a relay destination: `RelayDestination` raises `RelayConfigError` for it before any config is written |
| `twitch_redirect` | Twitch forbids redirecting viewers to another platform from a stream | Returned by `refusal_reason()` for the combination, so no surface can offer a redirect as a relay step — a refusal, never a warning |

## 4. Capability promises — the only sentences the product may use

| Situation | Allowed language | Forbidden |
|---|---|---|
| Class A, credentials verified by readback | "Posts to X" | "Posts to X reliably" before a real post exists |
| Class B, gate satisfied | "Posts to Instagram (Business/Creator account)" | "Posts to Instagram" for personal accounts |
| Class B, gate unsatisfied | "Needs app review" / "Needs a professional account" | Silent hiding, or a generic failure |
| Class A/B, not connected | "Not connected — connect it" (`skipped_unconnected`) | Counting it as a failure, or dropping it from the report |
| TikTok unaudited direct post | "Published privately (app not audited)" | "Posted" |
| TikTok inbox upload | "Draft delivered to TikTok — finish in the app" (inbox route, pending review) | "Posted" |
| Pinterest trial | "Saved to your board (sandbox)" | "Live on Pinterest" |
| Class C | "Published via your browser session (unofficial)" | "Official API" |
| Class D | "Prepared — finish in the app" | "Posted" |
| Readback unavailable | "Submitted; not yet confirmed" | "Success" |
| Live ingest, key fetched, no stream pushed yet | "Ready to relay — no live ingest proved yet" | "Live on YouTube/Twitch/Kick/Facebook", or "streaming supported" |
| Live ingest, per-session key (`manual`) | "Paste this session's key — it expires" | "Configured once", "connected" |

## 5. Limits that change routing decisions

| Platform | Hard limit | Consequence for xPST |
|---|---|---|
| YouTube | `videos.insert` has its own bucket: **100 calls/day** per project; other Data API calls 10,000 units/day | A 100-upload day is the ceiling per project; surface it in the routing preview, not in an error afterwards |
| YouTube (live) | `liveStreams.insert` costs **1 unit** and draws on that same 10,000-units/day pool — it is *not* part of the 100/day `videos.insert` bucket | Creating a stream resource is cheap; a whole live event (`liveBroadcasts.insert` / `bind` / `transition`, 1 unit each) stays inside the daily unit budget |
| X | Post create **$0.015/request**, but **$0.200/request when the post contains a URL**; reads $0.005/post | A link-heavy post costs ~13x a plain post. Show estimated cost before publish |
| Instagram | **100** API-published posts/24h (Instagram-Login API); **50**/24h on the Facebook-Login carousel path; carousels count as one; media container expires in 24h | Poll `content_publishing_limit`; containers that expire must be reported as expired, not failed |
| Threads | **250** posts/24h, **1,000** replies/24h, **100** deletes/24h; text 500 chars; video ≤ 5 min / ≤ 1 GB | Text longer than 500 chars must be split or refused with a reason |
| TikTok | Unaudited direct post is **private-only**; `creator_info` supplies privacy options and max duration | Never say "posted publicly" on an unaudited app — say "published privately pending audit" |
| Pinterest | Trial-access pins are **sandboxed to the creator** | Say "saved to your board (sandbox)" until Standard access lands |
| Bluesky | 300 graphemes / 3,000 UTF-8 bytes; 4 images per post, ≤ 2,000,000 bytes each | xPST enforces 300 **code points** (`TEXT_LIMITS`), which is never more permissive than 300 graphemes and can never approach 3,000 bytes, and it refuses rather than truncating. Images are refused above the byte cap instead of being silently re-encoded |
| Mastodon | Instance-defined character and media limits | Read instance metadata; never hardcode 500 |
| Reddit | ~100 queries/min per OAuth client; non-commercial tier | Budget readback calls; commercial use needs an agreement |
| Telegram | Bot must be in the chat with posting rights | "Connect" means picking a channel the bot can post in |
| Discord | Webhook/bot channel scope; per-route rate limits | "Connect" means picking a channel |

## 6. Media transport is a routing criterion, not an implementation detail

Two transports exist in the shipped catalog and they mean different things for a
phone-first product:

- **`local_upload`** — the engine pushes bytes. Works from any device that can hand
  the file to the engine host.
- **`public_url`** — the engine sends the platform a URL and the platform fetches
  it. This requires a publicly reachable HTTPS endpoint serving the media, with an
  unguessable path and a short expiry.

Consequences, recorded here because they drive the client architecture:

1. **A phone-first MVP needs a media-ingest path.** `POST /api/post` accepts
   `media_paths` resolved *on the engine host*; there is no upload endpoint, so a
   video in the phone's camera roll cannot be published today. The MVP adds
   `POST /api/media` (multipart, size/type capped, staging dir, idempotency key).
2. **`public_url` destinations need a media host even when everything else is
   local.** A loopback-only engine cannot publish Threads video. The relay/tunnel
   in `PROPOSAL-PRODUCT-RESET.md` is therefore not only about reaching the UI from
   a phone — it is required for a subset of destinations by protocol design.
3. **The routing preview must show transport per destination and content type**, so
   "will publish" never appears for a Threads video on an engine with no public URL.

## 7. Maintenance

- Re-verify a vendor row before changing what the product promises about it, and
  update the check date. Vendor pages move; a 12-month-old row is a guess.
- New provider code must land with (a) its matrix row, (b) the refusal reason for
  each gate, (c) a readback path, or it is not a supported provider.
- §3 is asserted in CI. Adding a destination to `capability_document()`, or a
  platform to `STREAM_INGEST_PATHS`, without updating this file fails
  `tests/test_platform_capability_matrix.py`.
- The Rust rebuild turns this document into data (`xpst-provider` capability
  descriptors) so a missing row fails a test instead of shipping as a silent gap.

## 8. References

Fetched or read 2026-11-04 unless the vendor page states its own update date.
Live-ingest rows (§2, §3) were fetched 2026-11-16; each carries its own check date
in the table.

1. Google, *Quota Calculator* — <https://developers.google.com/youtube/v3/determine_quota_cost> (updated 2026-11-12).
2. X, *About the X API* — <https://docs.x.com/x-api/getting-started/about-x-api>;
   *Pay-per-usage pricing* — <https://docs.x.com/x-api/getting-started/pricing>.
3. Meta, *Instagram content publishing* — <https://developers.facebook.com/docs/instagram-platform/content-publishing/>.
4. Meta, *Threads API overview* — <https://developers.facebook.com/docs/threads/overview> (updated 2025-12-22);
   *Threads posts* — <https://developers.facebook.com/docs/threads/posts>.
5. Meta, *Pages API posts* — <https://developers.facebook.com/docs/pages-api/posts> (updated 2026-04-17).
6. TikTok, *Content Posting API — get started* — <https://developers.tiktok.com/doc/content-posting-api-get-started/> (updated 2026-08-04).
7. Microsoft, *LinkedIn Posts API* — <https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api> (updated 2026-05-13).
8. Bluesky, *Build on Bluesky* — <https://bsky.network/docs/bluesky-api>; lexicon `app.bsky.feed.post` for the grapheme/byte caps.
9. Mastodon, *statuses API* — <https://docs.joinmastodon.org/methods/statuses/> (updated 2026-07-29).
10. Reddit, *API documentation* — <https://www.reddit.com/dev/api/>; rate-tier figures from Reddit's published OAuth limits (secondary sources agree; treat the number as approximate).
11. Pinterest, *Access tiers* — <https://developers.pinterest.com/docs/getting-started/access-tiers/>;
    *Create Pin* — <https://developers.pinterest.com/docs/api/v5/pins-create/>.
12. Telegram, *Bot API* — <https://core.telegram.org/bots/api> (Bot API 10.3, 2026-08-24).
13. Discord, *Webhook resource* — <https://discord.com/developers/docs/resources/webhook>.
14. Snap, *Public Profile API* — <https://developers.snap.com/marketing-api/Public-Profile-API/Introduction>.
15. Google, *LiveStreams: insert* — <https://developers.google.com/youtube/v3/live/docs/liveStreams/insert> (last updated 2026-10-15);
    *YouTube Live Streaming API overview* — <https://developers.google.com/youtube/v3/live/getting-started> (2026-10-15).
16. Twitch, *API reference — Get Stream Key* — <https://dev.twitch.tv/docs/api/reference/#get-stream-key>.
17. Kick, *Scopes* — <https://docs.kick.com/getting-started/scopes>; *Channels* — <https://docs.kick.com/apis/channels>.
18. Meta, *Live Video API* — <https://developers.facebook.com/docs/live-video-api> (updated 2026-07-02).
19. Instagram Help Center, *Start a live video on Instagram* — <https://help.instagram.com/292478487812558>;
    About Instagram, *Instagram Live Producer* — <https://about.instagram.com/blog/tips-and-tricks/instagram-live-producer> (2022-11-02).
20. TikTok Support, *What is TikTok LIVE?* — <https://support.tiktok.com/en/live-gifts-wallet/tiktok-live/what-is-tiktok-live>.
21. Rumble FAQ, *How to Setup a Livestream* — <https://rumble.support/help/how-to-setup-a-livestream> (updated 2025-12-05);
    *Livestreaming Related Questions* — <https://rumble.support/help/livestreaming>.

Rows marked *unverified* are placeholders with an owner (see the product reset
proposal, open questions). They are not promises.
