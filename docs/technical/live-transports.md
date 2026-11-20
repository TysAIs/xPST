# Live transports

Status: implemented for four destinations, unexercised against the real
platforms. This document records what the code does, what is still a fake, and
what a reader needs to know to extend it.

Nothing in this document authorises a live publish. Posting stays paused; a live
transport is proven with fixtures and a dry run until the pipeline ships, and the
slice that actually calls the platforms is a separate change.

## What exists

Every destination crate publishes through a `Transport` trait, and each now has a
real implementation of it:

| Crate | Trait | Live type | Exchange |
|---|---|---|---|
| `xpst-provider-youtube` | `transport::Transport` (async) | `http::HttpTransport` | async `ReqwestAdapter` |
| `xpst-provider-x` | `transport::Transport` (blocking) | `http::HttpTransport` | blocking `ReqwestBlockingAdapter` |
| `xpst-provider-instagram` | `transport::Transport` (blocking) | `http::HttpTransport` | blocking `ReqwestBlockingAdapter` |
| `xpst-provider-bluesky` | `transport::BlueskyTransport` (async) | `atrium::AtriumTransport` | async `ReqwestAdapter` |

The policy they all share — timeouts, redirects, proxy behaviour, TLS, user
agent, `Retry-After` parsing, credential redaction — lives once in
`xpst_provider::http`. Only the mapping between each crate's request type and the
wire is per crate.

### Why the policy is shared and the mapping is not

The four request types are not variations on each other. YouTube's is a method,
a URL, headers and raw bytes for a resumable `PUT`. X's is a tagged body —
empty, JSON, form or multipart — whose boundary only the transport can generate.
Instagram's is query pairs, form pairs and an optional binary body. Bluesky's is
an XRPC call expressed by a generated lexicon type. A single transport could only
implement all four by depending on all four provider crates, inverting the
dependency the workspace is laid out around.

What *is* identical is everything around the request: how long the client waits,
whether it follows a redirect, whether it may use an environment proxy, whether
it retries, what `User-Agent` it sends, how a `Retry-After` is parsed, and what an
error is allowed to say. That is the part where four copies would drift, so it
is the part that lives in `crates/xpst-provider/src/http.rs`.

## Client configuration

`ClientConfig::for_destination()` is the only configuration production uses, and
it is a value rather than a builder chain so a test can assert it without opening
a socket (`the_default_policy_is_the_safe_one`).

| Knob | Value | Why |
|---|---|---|
| connect timeout | 10 s | A dead host fails fast instead of holding the pipeline. |
| request timeout | 300 s | One request's ceiling, body included. Media arrives in 5 MiB (X) and 8 MiB (YouTube) chunks, so 300 s is roughly 220 KiB/s for the largest chunk: slow enough for a domestic uplink, short enough that a wedged connection cannot stall a run for an hour. |
| redirects | **none** | Google's resumable upload answers `308 Resume Incomplete` with the `Range` it holds, and X reads `Location` after `INIT`. A client that follows those turns progress information into a second request against a URL nobody asked for, and the driver loses the offset. A 3xx is therefore always surfaced to the provider. |
| proxy | **disabled** | `reqwest` otherwise honours `HTTP_PROXY`/`HTTPS_PROXY` from the environment, which would route a bearer token through an unvetted intermediary. `ClientConfig::with_proxy(Proxy::FromEnvironment)` is the explicit opt-in. |
| TLS | verify (the only variant) | `Tls` has exactly one variant, so "do not verify" cannot be expressed. `reqwest` is built with rustls and the platform verifier; no OpenSSL is in the graph. |
| retries | **never** | `reqwest` 0.13 retries protocol NACKs by default, which is a second request nobody asked for. `ClientConfig::retries` is applied as `reqwest::retry::never()`. |
| user agent | `xPST/<workspace version>` | Platforms attribute traffic by it, and a version is the difference between "xPST is misbehaving" and "xPST 1.3.0 is misbehaving". Not configurable: a caller that can forge it will be blamed for someone else's traffic. |
| pool idle | 90 s | A socket idle for minutes is more likely dead than fresh; recycling early stops a stale connection being charged against the request timeout. |

The live adapters in `xpst_provider::http` are behind the `live-http` feature,
which each provider crate turns on by default
(`[features] default = ["live-http"]`). Building any of them with
`--no-default-features` keeps the protocol and its fakes and links no HTTP
stack — which is the property the crate docs promised before this slice.

## Retry ownership

**The transport never retries.** Three things enforce that rather than merely
state it:

1. `Retries::Never` is the only variant, and both adapters apply it to the
   underlying client, whose own default is to retry protocol NACKs.
2. No transport has a loop. `HttpTransport::send` and
   `AtriumTransport`'s five operations each issue exactly one request.
3. Retry is a decision that needs context the transport does not have:
   - YouTube: a chunk may be re-sent because the server answers `308` with what
     it already holds, but the final `Content-Range: bytes */N` request may
     already have published, so a lost answer is reported *unconfirmed* and never
     re-sent. That logic is in `upload.rs`.
   - X: `POST /2/tweets` and `media_publish` are not idempotent; the pipeline
     reconciles before retrying.
   - Instagram: `media_publish` has no idempotency key, so a lost answer is
     `PublishError::Unconfirmed` with the container id to read back.
   - Bluesky: the record key is derived from content, so a retry addresses the
     same record — which is what makes the retry safe, not anything the transport
     does.

`Retry-After` is parsed by `xpst_provider::http::RetryAfter` (both RFC 9110 forms)
and is honoured where the response is classified:

- YouTube: `upload.rs` sleeps for it inside `RetryPolicy`'s window, and reports a
  longer one as a retryable delay instead of parking a task.
- X: `error.rs` reads `x-rate-limit-reset` and `Retry-After` into
  `XError::RateLimited`.
- Instagram: `classify.rs` reads `Retry-After` and Meta's in-body wording.
- Bluesky: **`retry_after_s` is still `None`.** `atrium`'s
  `XrpcErrorType::XrpcResponse` keeps the status and the error body and drops the
  response headers, so this adapter cannot see the header. Closing that needs a
  lower-level XRPC path or an upstream change; it is listed under "what remains"
  rather than papered over with a shared-cell hack that would race under
  concurrency.

## Secret hygiene

Two rules, both enforced by types rather than by review:

- **A failure carries no credential and no body.** `HttpFailure` holds a kind and
  a detail, and its only constructor runs `redact_text` over the detail. Every
  provider error is built from that string. There is no logging in any transport
  module at all, so there is no log line to leak into.
- **`Debug` is hand-written.** `PreparedRequest` and `PreparedResponse` render
  the method, the redacted URL, header *names* (with `authorization`, `cookie`,
  `set-cookie`, `x-api-key` and friends replaced by `<redacted>`), and the body's
  length. A `{request:?}` cannot print a bearer token or a caption.
- URLs are redacted parameter by parameter: `access_token`, `token`, `api_key`,
  `sig`, `oauth_signature`, `client_secret`, `password`, `upload_id` and the rest
  are replaced with `REDACTED`, and so is any `user:pass@` userinfo. Everything
  else survives, because a redacted 404 still has to be debuggable. This matters
  most for Instagram, which sends the access token as a query parameter on `GET`,
  and for YouTube, whose resumable session URI is a capability that anyone
  holding it can write into.

The tests that hold this:

- `xpst-provider`: `a_query_parameter_that_is_a_credential_is_redacted`,
  `redact_text_rewrites_a_url_inside_a_sentence`,
  `a_request_never_renders_its_authorization_header_or_body`,
  `a_response_never_renders_its_body`,
  `a_failure_detail_has_its_urls_redacted_on_construction`.
- Each provider's `src/http.rs`: a scripted failure whose detail contains a token,
  asserted absent from the rendered error — YouTube's
  `the_transport_error_does_not_quote_the_bearer_token`, X's
  `the_oauth_signature_never_appears_in_a_rendered_error`, Instagram's
  `an_access_token_in_the_url_never_reaches_the_error` and
  `an_authorization_header_is_not_rendered_by_an_error_either`.
- `crates/xpst-provider-bluesky/tests/live_transport.rs`:
  `a_failed_exchange_never_echoes_the_access_jwt`, which drives the real provider
  and asserts the JWT is absent from the outcome.

## Tests: mapping without a network

`xpst_provider::http::fake::RecordingAdapter` records every `PreparedRequest` it
is handed and answers from a script. It implements both the async and the
blocking adapter traits, so it slots into all four transports.

`tests/live_transport.rs` in each provider crate drives the **real publishing
code** over that adapter, and asserts the request that was produced:

- **YouTube** — the resumable protocol end to end: `POST` with
  `uploadType=resumable`, `Authorization: Bearer …`, `X-Upload-Content-Length`,
  JSON metadata; then `PUT` with `Content-Range: bytes 0-1023/1024`. A scripted
  `308` with `Range: bytes=0-511` must reach the driver, which then resumes at
  byte 512 — the assertion that fails if a redirect policy ever starts following
  a 308.
- **X** — a signed `POST /2/tweets` with a JSON body; and the full media path
  (`INIT` form, multipart `APPEND` whose `Content-Type` boundary matches its
  body, `FINALIZE`, then the tweet), asserting a distinct OAuth nonce per request
  and that the `204` on `APPEND` is mapped as a response rather than an error.
- **Instagram** — container create, status poll and `media_publish` as
  form posts, with the query merge proven against a URL that already carries
  `?fields=status_code`, and the token present as a parameter.
- **Bluesky** — `createSession` without an `Authorization` header, then
  `createRecord` with `Bearer`, the NSID in the URL and a lexicon-shaped record
  in the body; a `401` reported as `NeedsReauth` with no second request; and a
  read-back as `GET getRecord`.

No test sends through `ReqwestAdapter` or `ReqwestBlockingAdapter`, so `cargo
test` cannot reach a host even if one were reachable. The one test that touches
them only constructs them (`the_live_adapters_build_from_the_default_policy`),
which dials nothing and proves `reqwest` accepts this combination of knobs. That
is the reason the exchange is behind an adapter trait rather than being the
transport itself.

## What remains fake or unimplemented

- **No platform has been called.** This slice ends at "the mapping is correct".
  Reaching a real endpoint is the next change, and it needs: credentials from
  `xpst-authflow`, a dry-run path in the pipeline, and an operator's explicit
  go-ahead.
- **OAuth token acquisition is out of scope.** `token::TokenSource`,
  `xpst-authflow` and the OAuth 1.0a signer supply credentials; the transports
  take them already minted.
- **Bluesky `Retry-After`** — see above. Status and error name are classified;
  the header is lost in `atrium`'s error type.
- **The blocking transports cannot be used inside an async executor.**
  `reqwest::blocking` owns a runtime on a background thread and panics if driven
  from one. The X and Instagram provider traits are synchronous (their publish
  logic is written that way, and 40+ tests depend on it), so the pipeline must
  call those two from a blocking context — `spawn_blocking` at the boundary, or
  an async provider adapter. The pipeline's provider trait is currently async, so
  this is a real integration task for the slice that wires them together, not a
  detail to discover at runtime.
- **Bodies are materialised in memory.** Fine at 5–8 MiB per media chunk and for
  JSON/form payloads; a streaming upload would be a separate adapter.
- **No certificate pinning, no HTTP/3, no custom root store.** The platform
  verifier is used deliberately: a pinned root we forget to rotate is an outage,
  and this product has no fleet to rotate it.
- **`xpst-provider-tiktok`, `-threads` and `-meta` are still stubs.** They have no
  publish logic to transport yet.

## How to extend it

**Add a live transport to a provider crate that has none** (tiktok, threads,
meta):

1. Implement `xpst_provider::http::HttpAdapter` for real? No — you do not need
   to. Take the existing one: for an async seam, hold a
   `ReqwestAdapter`; for a blocking seam, `ReqwestBlockingAdapter`.
2. Write a `prepare`-style function that turns the crate's request type into a
   `PreparedRequest` — method, URL with encoded query, headers, body bytes — and
   a function that turns a `PreparedResponse` back into the crate's response
   type. Keep both public: they are the readable half of the module.
3. Build the live constructor from `ClientConfig::for_destination()` and pass
   the config through `ReqwestAdapter::new` / `ReqwestBlockingAdapter::new`.
4. Map `HttpFailure` onto the crate's own error type. Decide retryability from
   `HttpFailure::is_retryable` only where the provider's error type carries it,
   and never loop.
5. Add a `tests/live_transport.rs` that drives the crate's real publish entry
   point with `RecordingAdapter::scripted(...)` and asserts method, URL, headers
   (including auth) and body — plus a scripted failure containing a token, so the
   redaction is proven for that crate too.
6. Declare the crate's `live-http` feature as `["xpst-provider/live-http"]` and
   make it default, so the workspace gate actually builds and tests it.

**Add a different exchange mechanism** (a test server, a proxy-aware client, a
recording cassette): implement `HttpAdapter` or `BlockingHttpAdapter`. Nothing
above the trait changes. Do not put retry logic in it.

**Change client policy** (a longer total timeout for one endpoint): build the
adapter with a modified `ClientConfig` — `HttpTransport::live_with` and
`AtriumTransport::with_config` take one. Do not construct a `reqwest::Client`
inline: the invariant that the client is configured in exactly one place is what
makes `the_default_policy_is_the_safe_one` meaningful.

**Do a manual live smoke test** (only with an explicit go-ahead, and never in
`cargo test`): construct the transport with `HttpTransport::live()`, point it at
a scratch account, and keep the response headers. There is deliberately no
`#[ignore]`-marked live test in the tree — an ignored test is a test that rots,
and the fixture path already covers the protocol.
