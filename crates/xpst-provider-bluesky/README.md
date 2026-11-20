# xpst-provider-bluesky

The Bluesky destination for the Rust engine, over the AT Protocol.

Bluesky is the first real provider in this rebuild because it is the only
destination a user can configure without a human on the other side: no developer
app, no review, no redirect URI, no hosted media. That makes it the reference
implementation every other adapter is measured against.

Scope of this crate: authenticate, publish text, publish images, read back a
post, delete it — against a transport trait, so the whole contract is testable
without a socket. Video is deliberately *not* published here (see below).

## The credential model: an app password, and nothing else

```rust
BlueskyCredentials::new("handle.example", "abcd-efgh-ijkl-mnop")
```

Those two strings are the entire configuration:

* the **handle** (or the account's DID) — not a secret, and the only part that
  may live in `config.yaml`;
* an **app password** — created in Bluesky's own settings (*Settings → App
  Passwords*), revocable on its own, and never the account password.

At runtime they are exchanged once for a session:

```
POST https://bsky.social/xrpc/com.atproto.server.createSession
     {"identifier": "<handle>", "password": "<app password>"}
  -> {"accessJwt", "refreshJwt", "did", "handle"}
```

The session is cached inside `BlueskyProvider` because `createSession` is the
rate-limited call (roughly 30 per five minutes, 300 per day) — a queue drain must
not create one session per post. `AppPassword` redacts itself in `Debug` and
`Session` redacts its access token, so a log line cannot leak either.

### Why this is the only zero-friction platform

Every other destination in the catalog asks the user (or us) for something that
takes a human, a review queue, or a hosting decision first:

| Destination | What stands between a user and publishing |
|---|---|
| X | a developer app, OAuth 1.0a/2.0 keys, per-plan write quotas |
| Instagram / Threads / Facebook | a Meta developer app, App Review, a business account, a public `https://` media URL |
| TikTok | developer app approval, domain verification, a content-posting audit |
| YouTube | Google Cloud project, OAuth client, quota request |
| **Bluesky** | **an app password typed into a settings screen** |

No developer console, no redirect URI, no app review, no media host: the PDS
accepts the blob directly, so there is not even a "public URL" rule to satisfy.
That is why the reference adapter lives here — if the provider abstraction cannot
express *this* path cleanly, it will not express the others.

## What the adapter refuses

Every refusal is decided **before the first network call**, so a rejected request
leaves no uploaded blob and no half-written record, and no request is ever
silently made smaller. Each refusal carries the machine code, the measured
number, the limit, and a message quoting both.

| Refusal | Code | When |
|---|---|---|
| `TextTooManyGraphemes` | `text_too_many_graphemes` | more than 300 grapheme clusters |
| `TextTooManyBytes` | `text_too_many_bytes` | more than 3,000 UTF-8 bytes |
| `EmptyText` | `empty_text` | a text-only post with no content |
| `TooManyImages` | `too_many_images` | more than 4 images in one post |
| `NoImages` | `no_images` | an image post with no images |
| `ImageTooLarge` | `image_too_large` | an image above 2,000,000 bytes |
| `ImageUnsupportedType` | `image_unsupported_type` | anything but JPG/PNG/WEBP/GIF |
| `VideoNeedsVideoService` | `video_needs_video_service` | any video request |

Both text caps are enforced, because both are real: a post may be under 300
graphemes (150 family emoji, say) and still be refused for being 3,750 bytes. The
message says which cap tripped, what was measured and what is allowed — e.g.
`the text is 3,750 bytes of UTF-8; Bluesky accepts at most 3,000 bytes per post`.

Nothing is truncated, downscaled, re-encoded or dropped, and the extras are never
quietly discarded: an eight-image request is refused with "this request carries 8
images; Bluesky attaches at most 4 images per post" and the advice to split it.

### Video

Bluesky does accept video, but not through the record API this adapter writes: it
goes to a separate service (`https://video.bsky.app`) as an asynchronous job under
a service-auth token. That path cannot be proven without live credentials, so it is
not implemented. The capability declares `VideoRoute::VideoService`, and a video
request gets a refusal naming the service rather than a post published without its
video. `BlueskyProvider::publish_video` exists so a caller that dispatches on
content type gets that answer from the adapter, instead of having to remember not
to call the text path.

## Outcomes are claims about the platform

| Operation | Success is only |
|---|---|
| publish | `PublishedPost` — the PDS returned a record URI |
| readback | `ReadbackState::Verified` — the PDS returned the record |
| delete | `DeleteOutcome::Deleted` — the PDS accepted the delete (or says the record is already gone) |

Everything else is honest about what was not learned:

* `PublishOutcome::NeedsReauth` — the credential was rejected or the session
  expired; retrying the same credential cannot help, so a human must re-connect.
  The cached session is dropped so the next attempt logs in again.
* `PublishOutcome::Failed { retryable }` — the PDS was asked and the attempt
  failed; `retryable` is `false` for a rejection and `true` for throttling, an
  upstream 5xx or a network failure (a rate limit's own `Retry-After` is carried
  into the message).
* `ReadbackState::NotFound` — the PDS answered `RecordNotFound`. Only that
  answer, interestingly, produces it.
* `ReadbackState::SubmittedUnconfirmed` — the platform was not successfully asked
  (bad URI, no credential, read failed). The post stays *submitted, unconfirmed*:
  neither upgraded to verified nor downgraded to missing.
* `DeleteOutcome::Pending` — the delete was not confirmed. The post may still be
  live, and the report says so.

## Layout

| Module | What it is |
|---|---|
| `transport` | `BlueskyTransport` — the five operations (login, upload blob, create record, get record, delete record) — plus the contract types and the failure classes |
| `capabilities` | the limits as data, and every refusal constructor |
| `provider` | `BlueskyProvider`: enforcement, publishing, readback, delete |
| `atrium` | the real transport: `atrium-api` (MIT, MSRV 1.85) typed lexicon calls over `reqwest` with rustls |
| `facets` | `http(s)` link detection with UTF-8 byte offsets |
| `fake` | an in-memory transport that records every call, for tests and for callers' own integration tests |

The trait speaks contract types (a post draft, a blob reference, an AT-URI), not
the AT Protocol crate's generated structs, so the fake cannot accidentally
"agree" with a real response by construction.

## Dependency and licence notes

* `atrium-api` 0.26 — MIT, MSRV 1.85 (below this workspace's `rust-version`
  floor, so it never constrains the build). Verified from the published crate
  metadata, per `docs/technical/licence-policy.md`, which already lists
  `atrium-rs/atrium` as an approved dependency.
* `reqwest` with `rustls` — no OpenSSL in the graph, per the rebuild proposal's
  "one runtime, no OpenSSL" rule. Be aware of what that costs: `reqwest` 0.13's
  `rustls` feature selects `aws-lc-rs`, which vendors C crypto code and needs a C
  toolchain (and cmake) at build time. It is permissively licensed and statically
  linked, so it does not violate the licence policy, but packaging must not assume
  a pure-Rust build.
* `unicode-segmentation`, `jiff`, `async-trait`, `thiserror`, `tokio (sync)` —
  all MIT/Apache-2.0 (jiff is Unlicense OR MIT). `jiff` formats the AT Protocol
  `createdAt`; `atrium-api` is what still pulls `chrono`, which is why that
  unmaintained-crate notice remains in `deny.toml` rather than being cleared.

## Tests

`cargo test -p xpst-provider-bluesky` runs 43 tests. None of them open a socket:
publish, readback and delete are exercised against `FakeTransport`, and the record
the real transport would send is asserted as encoded JSON (a `$type`-tagged
`app.bsky.feed.post` with facets and image embeds) without any HTTP.

The live path is intentionally unproven here. It is kept deliberately thin so a
live smoke test would exercise code that fits on one screen — and, per the
posting-paused rule, nothing in this crate publishes to a real account by itself.

Out of scope for this crate: live network tests, the CLI surface, and OAuth or
direct-mode variants of the session.
