# Changelog

All notable changes to xPST will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **The provider transports can reach the platforms (`xpst-provider::http`).**
  Every destination published through a `Transport` trait with no live
  implementation, so no part of the Rust engine had ever been proven against a
  real endpoint. YouTube, X, Instagram and Bluesky now each have one: a thin
  mapping from the crate's own request type onto method, URL, headers and body,
  and back into its response type, over `reqwest` with rustls (no OpenSSL). The
  policy around that request is shared in `xpst_provider::http` rather than
  copied four times, because the request types genuinely differ while the client
  configuration must not: 10 s connect and 300 s total timeouts, redirects
  **never** followed (Google's `308 Resume Incomplete` and X's upload `Location`
  are information, not redirects), environment proxies ignored unless opted in,
  TLS verification the only expressible policy, `reqwest`'s own protocol-NACK
  retry disabled so no transport ever re-sends a non-idempotent POST, and
  `xPST/<version>` as the user agent. Failures carry a kind and a detail that has
  been scrubbed of URLs' credentials (`access_token` is a query parameter on the
  Graph API) and are structurally incapable of carrying a header or a body —
  `PreparedRequest`'s `Debug` is hand-written and redacts `Authorization`,
  `Cookie` and the body. The exchange sits behind an adapter trait, so each
  provider's own tests drive the real publishing path with a recording adapter
  and assert the request that was produced without opening a socket; no test
  constructs the live client. `Retry-After` parsing moved into the shared
  vocabulary and YouTube's copy was deleted. What remains is stated in
  `docs/technical/live-transports.md`: no platform has been called yet, bluesky's
  `retry_after_s` is still `None` because `atrium` discards response headers, and
  the two blocking transports must be called off the async executor.
- **The licence policy is enforced, not stated.** `docs/technical/licence-policy.md`
  promised that `cargo-deny` and `cargo-about` ran in CI and failed the build on
  an unacceptable licence; neither tool was configured or invoked, so a copyleft
  dependency could land with every gate green. There is now a `deny.toml` with
  the policy's accept list, a `Licence policy` CI job that runs `cargo deny
  check` over the engine workspace **and** the Tauri shell (whose manifest had
  no `license` field at all), and `scripts/check_licences.py`, which needs only
  Cargo: it reads `Cargo.lock`, resolves each locked package's licence from the
  registry metadata Cargo has cached, evaluates the SPDX expression and fails on
  anything outside the list — including `MIT AND GPL-3.0` while accepting
  `MIT OR GPL-3.0`. `crates/xpst-core/tests/licence_gate.rs` fails if the
  document, `deny.toml` and the script stop agreeing, or if CI loses either
  mechanism. `cargo-about` NOTICE generation is the one part still pending, and
  the document now says so instead of implying otherwise.
- **The Rust engine has a publishing pipeline (`xpst-pipeline`).** One API —
  plan → confirm → publish — over a frozen per-destination intent, with the only
  safe execution order enforced in code: limits decision, then provider call,
  then honest outcome, then state write. A destination is attempted only under
  an attempt lease held by one owner, retries are bounded, and an unknown
  outcome keeps its lease until that owner reconciles it, so a retry cannot
  double-post. Outcomes are counted one bucket each: `published`,
  `skipped_duplicate`, `deferred`, `refused`, `failed_retryable`,
  `failed_permanent`, `unconfirmed` (plus pending), so a dedupe no-op is never
  counted as a post, a deferral is never a failure, and a limits refusal never
  reaches a provider. Re-running a partially completed plan publishes only the
  destinations that did not complete.
- **The Rust engine publishes to Instagram (`xpst-provider-instagram`).**
  Container → status → publish for Reels, feed images and carousels, over a
  transport seam: a local video uses Meta's resumable upload endpoint, an image
  takes a public URL because that is the only image path the Graph API has, and
  every answer is classified (throttle, `FeedbackRequired`,
  `PleaseWaitFewMinutes`, `ChallengeRequired`, the HTML login wall served with
  HTTP 200, a stale-query body) instead of collapsing into "failed". Limits are
  enforced locally, and conservatively where Meta contradicts itself: ten
  carousel children (not twenty) and fifty posts per 24 hours (not a hundred),
  each constant citing the conflicting documents. Status polling is bounded by
  both an attempt count and a wall-clock cap.
- **Caption variation is opt-in (`anti_bot.caption_variation`, default
  false).** xPST appended engagement-bait suffixes ("🎬 Subscribe for more!",
  "#Reels #ReelsInstagram", "Retweet if you enjoyed! 🔄") to every video and
  image upload, with no config gate and no consent, so it published copy the
  operator never wrote. The gate now sits at the single caption-resolution
  point; when it is enabled the final caption is what state and every post
  result record (the existing `caption_sent` path), so reports still match
  what went live.
- **The schedule queue has a first-class pause (`xpst schedule pause` /
  `xpst schedule resume`, plus the `xpst_schedule_pause` /
  `xpst_schedule_resume` MCP tools).** Stopping a queue used to mean renaming
  `~/.xpst/schedule.json` by hand: nothing read that name, `schedule list`
  reported an empty queue, and renaming the file back released every entry
  with no warning. Pause is now a store-backed marker enforced at the claim
  boundary, so no surface (cron, daemon or an explicit run) can publish from a
  paused queue; it survives a restart, fails closed on an unreadable marker,
  and is reported by `schedule list` (CLI and MCP) with its reason.
  `xpst schedule run` refuses with `paused: true` instead of reporting
  "nothing due". Resuming re-enables publishing, so the MCP tool is
  consent-gated; pausing deliberately is not.

### Fixed
- **The config parser and the time rendering no longer depend on unmaintained
  crates (`xpst-config`, `xpst-mcp`, `xpst-verify`, `xpst-provider-bluesky`).**
  Three RustSec notices could not be cleared by any upgrade because they affect
  every released version. `chrono-tz` and `unsafe-libyaml` are now out of the
  graph, and this workspace no longer calls `chrono` at all — what remains is
  the edge `atrium-api`/`schemars`/`rmcp` hold, so one argued ignore stays and
  the other two are deleted. YAML is parsed by `serde-saphyr` (safe Rust,
  `#![forbid(unsafe_code)]`, no C dependency) behind this repository's own
  ordered `Value`/`Mapping` model, with the scalar rules pinned to the previous
  parser's rather than the new one's defaults: `on`/`yes`/`off`/`no` remain
  strings, `007` and `1_000` remain strings, duplicate keys are refused, `<<:`
  stays a literal key, document key order is preserved. Timekeeping moved to
  `jiff`, which reads the platform's IANA database instead of freezing a zone
  table into the binary. Both are held by captured corpora rather than by
  inspection: `crates/xpst-config/tests/yaml_compat.rs` fails if the JSON
  rendering of 21 config documents changes, and
  `crates/xpst-mcp/tests/schedule_local_time.rs` pins 25
  `scheduled_time_local` renderings captured from the shipped Python engine.
  That capture also surfaced a real defect the migration fixes: the rendered
  stamp carried the offset's sign twice (`2026-11-20T04:00:00+-05:00`) because
  the format string prepended `+` to an offset that already contains its own
  sign; it now matches `datetime.isoformat()`, which is what Python emits and
  what an agent parsing the field expects. One documented widening: a UTF-8 BOM
  before the config document is accepted (Windows editors write one; the old
  parser read it as the start of a second document and refused the file).
- **The advisory gate is green, and what stays ignored is argued rather than
  silenced (`Cargo.lock`, `src-tauri/Cargo.lock`, `deny.toml`).** The
  `Licence policy` job had been red on advisories since 2026-11-17: seven
  findings on the engine workspace and twenty-two on the Tauri shell, including
  real vulnerabilities in `time`, `libsqlite3-sys`, `rustls`, `rustls-webpki`,
  `webpki-root-certs` and six in `zip`. Fixed by upgrade, not by ignore:
  `time` 0.3.45 → 0.3.55, `rusqlite` 0.40.2 → 0.41.1 (`libsqlite3-sys` 0.39.1,
  bundled SQLite 3.54.1), and the shell's Tauri family moved to 2.12.7
  (`tauri-plugin-updater` 2.14.1 pulls `zip` 9.7.0; `tauri-utils` 2.10.4 drops
  the whole `rust-unic` family). The two `time`/`rusqlite` fixes needed a
  toolchain floor that could already be measured: the committed tree did not
  resolve on the declared `rust-version = 1.85` at all (`encoding_rs` requires
  1.88, `uuid` requires 1.89), so the engine now declares 1.89 — verified with
  `cargo +1.89.0 check --workspace --all-targets` — and the shell 1.90. What
  cannot be cleared is recorded instead: every remaining entry in
  `deny.toml`'s `[advisories] ignore` carries a reason and a review date, the
  long form is `docs/technical/security-advisories.md` (crate, reachability,
  removal trigger), and `crates/xpst-core/tests/advisory_policy.rs` fails the
  build if the two disagree or if `xpst-mcp` ever enables one of the rmcp
  transports the two rmcp ignores depend on being absent. Two follow-ups are
  cards, not bumps, because each changes the shipped surface: moving the MCP SDK
  off rmcp 0.11.0 (every fix crosses the MCP protocol generation) and replacing
  `chrono`/`chrono-tz`/`serde_yaml` in the scheduling and config paths.
- **The MCP SDK moved off rmcp 0.11.0, and the last two vulnerability ignores
  are gone (`crates/xpst-mcp`, `deny.toml`, `.cargo/audit.toml`).** rmcp 0.11.0
  carried RUSTSEC-2026-0189 (DNS rebinding in the Streamable HTTP *server*
  transport, fixed in 1.4.0) and RUSTSEC-2026-0462 (unbounded response buffering
  in the *client* transports, fixed in 3.9.0). Neither transport was ever
  compiled in, but only an ignore kept the gate quiet about them; the SDK now
  sits at 3.11.0, which carries both fixes, so both ignores are deleted rather
  than re-argued. The protocol generation is a decision and is treated as one:
  the server declares `SUPPORTED_PROTOCOL_VERSIONS` — the four
  `initialize`-handshake revisions — instead of inheriting the SDK's default set,
  so a later bump cannot silently widen what xPST claims to speak. Client-visible
  behaviour is pinned by `crates/xpst-mcp/tests/stdio_smoke.rs`: 2024-11-05,
  2025-03-26, 2025-06-18 and 2025-11-25 are echoed back (0.11.0 downgraded the
  last two to 2025-03-26), a client that opens a handshake asking for the
  handshake-less 2026-07-28 is served 2025-11-25, a client that skips the
  handshake entirely is refused with `-32022` and the list of supported
  revisions (0.11.0 answered nothing at all in that case), and all 46 tool
  schemas plus the tool result shape are byte-identical under every one of those
  negotiations. The four stale API items were mechanical
  (`CallToolRequestParams`, `PaginatedRequestParams`, `ContentBlock`,
  `ServerConfig`/`CallToolResponse`); no tool, schema or guard changed.
- **A confirmed plan is durable, so a restart cannot re-publish it
  (`xpst-pipeline`, `xpst-state`).** The plan record lived only in memory:
  `confirm()` rebuilt every destination with `outcome: None, attempts: 0,
  lease: None`, so the pipeline's "re-running publishes only what is left" claim
  held inside one process and nowhere else — after a crash the next run
  republished every destination. `PlanJournal` now freezes the plan into
  `plans/<plan-id>.json` under the config directory through
  `xpst_state::JsonDocument` (temp file + `fsync` + `rename` under the same
  exclusive advisory lock the state file takes), `JournalSink` is a real
  `StateSink` that writes every settle *and* every attempt lease, and
  re-confirming an existing plan returns the stored record instead of resetting
  it. Leases now carry an expiry, so an attempt owned by a killed process blocks
  other owners while it is live and is reclaimed after it lapses. Proven across
  processes: a child process resumes a half-published plan and publishes only
  the destination the parent left, with the persisted counts equal to the
  reported counts.
- **The capability table no longer claims destinations this engine cannot
  publish to (`xpst-core`).** TikTok, Threads, Facebook, Rumble and Messenger
  were listed as *implemented* — their provider crates are doc-comment stubs or
  do not exist — and `plan()` gated acceptance on that set, so a plan accepted
  destinations nothing could publish to. They are now declared-but-unimplemented
  (`implemented: &[]`), `DESTINATION_PROVIDER_CRATES` names the one crate that
  actually publishes to each destination, and `plan()` refuses an unimplemented
  destination with `DESTINATION_NOT_IMPLEMENTED` instead of accepting it.
  `tests/capability_truth.rs` fails if the table claims a destination whose
  provider crate is absent, not a workspace member, or a stub. Envelope and
  modality messages keep describing what the *destination* accepts, so their
  Python-parity text is unchanged.
- **A bodyless provider success is a success, not a parse failure — and the
  fakes can no longer hide the difference.** X answers the media-upload
  `APPEND` step with `204 No Content`; the handler parsed every 2xx body as
  JSON, so live media posts failed with "unreadable response body" while every
  test passed, because the fake transport replayed `200 {"media_id": …}`. A
  2xx with no body now reads as a success with nothing to parse (the caller
  that needs a field reports what was missing), and providers declare their
  response shapes as data (`xpst_provider::response_shape`): the X fake
  validates every canned response against the documented shape and refuses to
  answer an undocumented endpoint, YouTube's bodyless `Location`/`308` answers
  are pinned, and Instagram's "every success carries JSON" is asserted rather
  than assumed.
- **Parity tests can no longer pass vacuously.** Sixteen fixture loops in
  `content_parity.rs` and `plan_parity.rs` iterated arrays with no length
  assertion, so emptying a fixture turned the test into a no-op that still
  passed. Every loop now states the row count it expects, and each file carries
  one test listing every collection — a capture that writes an empty or
  truncated collection fails with the collection's name on it.
- **Live analytics fails visibly instead of reporting an unread zero.** A
  provider diagnostic envelope from X's `TweetDetail` query (HTTP 200 with
  `{"errors": [...]}` — rate limit, stale query id, rejected session) used to be
  re-raised as twikit's `TweetNotAvailable`, so `xpst analytics --live` reported
  *"every recorded post no longer exists upstream"* for posts that were never
  read. It is now a `TwikitApiError` failure carrying X's own message and code,
  reported with `reason: twikit_api_error`, and it stops after one sample
  instead of repeating per post. Every rejected `TweetDetail` payload (error
  envelope, HTML body, missing entries/focal entry) now carries a bounded,
  redacted response-shape sample into the log, and a post the provider itself
  marks `TweetTombstone`/`TweetUnavailable` reports that reason. On the
  Instagram side, a disabled platform, a missing session export and an HTML
  login page served with HTTP 200 each used to return an empty list and let the
  platform row read `status: ok` with zero posts; they are now classified
  (`platform_disabled`, `instagram_session_missing`, `instagram_login_wall`,
  `instagram_query_stale`, `instagram_rate_limited`) with the count of posts
  that were not read, an account-wide wall stops the loop after one sample, and
  analytics resolves `accounts.instagram.session_file` instead of assuming
  `<config>/credentials/instagram_session.json`. Collection reports carry a
  `remediation` string per reason so the fix is on the surface, not in a log.
- **A dedup no-op is no longer reported as a publish.** `upload_to_platform`
  answers an already-posted content hash with `success=True` and no post id;
  the fire path folded any success into `posted`, so day 2..N of a recurring
  entry over a static file reported "published" while nothing went live. The
  result counts carry a distinct `skipped_duplicate` outcome (and `xpst serve`
  logs it), and only real publishes count as `posted`.
- **`xpst_delete` keeps the local record when the platform delete is not
  confirmed.** The MCP tool removed the record regardless of the platform
  outcome, wiping the `delete_pending` marker `engine.delete_post` had just
  written: a live post with zero local trace, never retried, while the caller
  was told it succeeded. The record is now removed only for `deleted` /
  `soft_hidden`; `pending` / `unsupported` keep it (so `xpst failures` can
  retry), and the payload returns `ok: false` with the share URL instead of a
  fabricated success.
- **A scheduler worker killed mid-post no longer buries the entry.** A claim
  writes `claimed_at` and nothing read it, so a crash between claim and
  completion left the entry in `processing` forever — invisible to the due
  scan and to every failure surface. Claims are now leases:
  `ScheduleManager.reclaim_stale()` returns an expired claim to `pending`
  (recording why) and the scheduling engine runs it before each claim, so the
  next tick retries the post instead of silently losing it.
- **The Rust YouTube adapter refuses instead of trimming, and never invents a
  response.** An over-length title, description or tag set was silently cut to
  fit — and a long title lost the ` #Shorts` marker that makes it a Short, so
  the upload was published as long-form. Length is now refused with the measured
  count and the limit, and the marker's ten characters are reserved out of the
  title budget *before* measuring, so no title can lose it. A transport error on
  the finalise request used to be reported as a permanent *"answered 308"* — a
  status that request never received; it is now `YOUTUBE_UPLOAD_UNCONFIRMED`
  (the video may be live: read the channel back before retrying), and a 404/410
  no longer re-sends the file blindly — it restarts only when the adapter can
  prove nothing was published, otherwise it asks a read-back seam and reports
  unconfirmed when that cannot answer.
- **`--file` selects the config it names, and the flags that were being ignored
  are read again (`crates/xpst-cli`).** Handlers looked their flags up by the
  Click *parameter* name (`--file` is declared as `config_file`, `--json` as
  `as_json`), so the lookup found nothing and fell through: `xpst config show
  --file other.yaml` printed the default profile's document with exit 0, and a
  missing path never produced the documented `CONFIG_FILE_NOT_FOUND`. Argument
  ids are now derived from the `--long` form every call site already writes,
  which also restores `--json` on a terminal (it only worked when stdout was
  piped, where JSON is automatic anyway). Two guards keep the class from
  returning: one asserts the registered id of every manifest option is its flag,
  the other fails if any `opt:` literal in the crate names an argument the tree
  never registered. The `config` leaves that still document `--file` without
  implementing it refuse with `NOT_IMPLEMENTED` rather than silently reading the
  default profile.
- **An operator can ask for a private publish on TikTok — and a request that
  cannot be honoured is refused out loud instead of posting publicly.**
  `PlatformUploader.upload()` never took a visibility argument, so
  `--visibility private` reached YouTube's `status.privacyStatus` and nothing
  else: TikTok's Direct Post API has carried `SELF_ONLY` all along, and the
  `creator_info/query` answer was only ever used to *react* to what the client
  was allowed. `xpst post -p tiktok --visibility private` now asks for
  `SELF_ONLY` (`public` → `PUBLIC_TO_EVERYONE`, `unlisted` refused by name),
  validates the request against `creator_info/query` before any upload, and
  **refuses** with `TIKTOK_VISIBILITY_REFUSED` — zero publish attempts — when
  the client may not use that level. The historical downgrade for an
  *unrequested* post is unchanged (that fallback exists because an unaudited
  client gets `403 unaudited_client_can_only_post_to_private_accounts`), and the
  reactive 403 path no longer demotes a private request to the browser
  publisher, which publishes publicly.
  One registry owns the answer — new `xpst.visibility` (the rule
  `xpst.content.VIDEO_ENVELOPES` follows): which level each destination route
  honours, what it maps to, and whether a request it cannot honour is REFUSED
  (TikTok's browser route, an inbox draft, or a requested-but-unavailable
  level) or ADVISED (X, Instagram, Threads, Facebook, Bluesky — no per-post
  private mode exists at any layer, so the post still publishes and the plan
  names it PUBLIC). The per-destination plan/dry-run payload carries the level
  that would be used (`visibility: {requested, level, honoured, route}`), the
  upload receipt carries the level that was used, and the refusal surfaces as a
  new canonical destination state `refused_visibility` + blocker
  `VISIBILITY_UNAVAILABLE`: a named blocker and exit 1 on `post --dry-run`, a
  non-zero exit on the live CLI, and `isError` on the MCP `xpst_post` tool
  (which now accepts `visibility` at all). `xpst preflight` gained the same
  flag. Evidence per case is in `tests/test_tiktok_visibility.py`.

## [1.3.0] - 2026-11-14

### Added
- **The ban risk of an unofficial auth path is now stated where the choice is
  made, and reported afterwards.** Instagram's `auth_mode: session` drives
  instagrapi's private mobile API, which Instagram's terms forbid; X's
  `auth_mode: cookies` replays the web session outside the official API.
  Neither was hidden in the code (`xpst providers` already said
  `is_official_api: false`) but nothing at the decision point said "this is the
  ban-risky path", and readiness called such an account simply "ready".
  `xpst connect instagram` now names the risk in a panel, refuses to continue
  without an explicit confirmation, and records the operator's choice in
  `~/.xpst/ban_risk_acknowledgements.json` (`0600`, bound to the auth mode it
  was given for); `xpst connect x` records the same for the cookie path.
  `xpst doctor` carries `ban_risk_advisories[]` plus a per-platform `ban_risk`,
  `xpst readiness` carries an `<platform>_ban_risk` row, and every
  `preflight`/`post --dry-run` destination on such a path carries
  `warnings[].code: UNOFFICIAL_PRIVATE_API` / `UNOFFICIAL_WEB_CLIENT`.
  `POST /api/connect/{platform}` returns `ban_risk` and accepts
  `acknowledge_ban_risk` to record the same choice. Advisory only for the
  surfaces that gate: `all_clear`, exit codes and publish behaviour are
  unchanged, and a platform **disabled** in config (the configured Meta pause)
  is never reported.
- **Read-only live chat, one merged timeline (`xpst chat`).** Twitch, YouTube and
  Kick chat in a single chronological feed — every message tagged with its
  platform, author, text, the platform's own timestamp, message id and channel —
  served identically by `xpst chat timeline`, the `xpst_chat` MCP tool and
  `GET /api/chat`. `xpst chat platforms` reports what can be read without any
  network call. There is **no send path**: Twitch is read over an anonymous
  `justinfan` IRC connection that cannot write, and the other two readers only
  list/subscribe. Every platform that cannot be read is reported with a code and
  a next step (`no_channel`, `credentials_missing`, `not_live`,
  `unverified_path`, `platform_error`, `network_error`, `invalid_response`) —
  never as an empty list pretending the chat was quiet.

  Evidence, stated plainly: **Twitch is proven live** (anonymous connection,
  channel join, real tagged `PRIVMSG` traffic). **YouTube is documented but
  unexercised** — the connected channel is not enabled for live streaming, so
  the read refuses with `live_not_enabled`. **Kick's public Pusher path is dead**
  (Pusher answers `4001 … not in this cluster` on every cluster); what kick.com
  itself does now is mint an anonymous guest token at
  `web.kick.com/api/v1/realtime/auth/connection` and subscribe to
  `chatrooms.<id>.v2` on its own realtime service. Connection, subscription and
  heartbeat are proven from this repository; no chat publication has been
  observed yet, so the Kick row stays `verified: false` and its message payload
  is parsed defensively. See [docs/CHAT-MIXING.md](docs/CHAT-MIXING.md).

  No new runtime dependency: IRC-over-TLS uses asyncio, YouTube reuses the
  existing Data API client, and Kick is spoken by a minimal RFC 6455 client
  (`src/xpst/chat/ws.py`) proven against RFC vectors and a loopback server.
  YouTube's `liveChatMessages.list` costs 5 quota units per poll against the
  10,000/day default, so the interval is floored at 2s and the payload reports
  the tokens spent.
- **The live-chat panel (`#/chat`) — the "one screen" half of the chat pillar.**
  Twitch, YouTube and Kick chat in one ordered feed, built entirely from the
  canonical `GET /api/chat` payload: each row shows the platform's own
  timestamp (in local time), the platform badge, the author and the text, and
  consecutive bounded reads are merged into one de-duplicated, 500-row-bounded
  view. A platform strip reports each platform's engine verdict with its code
  and next step, and the channel list is settable from the screen —
  `POST /api/chat/channels` writes `chat.twitch_channels`, `chat.kick_channels`
  and `chat.youtube_video_id` through the ordinary config path (token-guarded),
  so the panel, the CLI and MCP read one persisted truth. Read-only by
  construction: the only write is that channel list, and there is no send,
  reply or moderation control anywhere on the page.

  The empty states are separate sentences because the payload distinguishes
  them: nothing configured, every platform refused (with the engine's code),
  read-but-quiet ("a quiet chat, not a missing channel or a failed read"), and
  a read stopped before its window ended. Measured in `browser_exec` on a
  390 px viewport against a real local engine: `scrollWidth` 375, every visible
  control 44 px tall (`tooSmall: []`), rows 63–114 px. No new npm dependency.
  See [docs/CHAT-MIXING.md](docs/CHAT-MIXING.md).
- **Ingest keys fetched from the platform APIs (`xpst stream fetch-key`).** Slice
  1 asked the operator to paste a key; this asks the vendor. YouTube
  `liveStreams.insert`, Twitch `GET helix/streams/key`, Kick
  `GET public/v1/channels` (`data[].stream.key`) and Facebook Page
  `POST /{page-id}/live_videos` (`published=false`, so nothing appears on the
  Page) are each driven through their documented endpoint, with the transport
  injectable (`httpx.MockTransport` in tests, a fake service object for the
  Google client). Every refusal carries a machine-readable code — 
  `credentials_missing` (with the exact registration step and scopes),
  `manual_required`, `not_supported`, `platform_error`, `invalid_response`,
  `network_error`, and `live_not_enabled` for the account-level verdict a live
  YouTube call actually produced. `xpst stream app-credentials <platform>`
  stores the free provider-app credentials (client id + user access token +
  broadcaster id) encrypted and masked; `xpst stream live` reports who is live
  on Twitch/Kick, and says `unavailable` with the registration step where
  credentials are absent instead of rendering "offline".
- **One relay payload for agents and the UI.** MCP `xpst_stream` and
  `GET /api/stream` return the same registry + key-presence + fetch-readiness
  document as `xpst stream destinations` (built once in
  `xpst.stream.ingest.stream_report`), so an agent no longer has to shell out
  for a capability that exists in the CLI. Read-only: no key is ever
  serialised.
- **Live-stream relay foundation (`xpst stream`).** The stream pillar did not
  exist: nothing in the engine could name a platform's live-ingest path, and no
  code could fan one encoder feed out to several destinations. New
  `xpst.stream` package: an ingest-path registry that separates platforms which
  document a key endpoint (`api`: YouTube `liveStreams.insert`, Twitch
  `helix/streams/key`, Kick `streamkey:read`, Facebook Page `live_videos`) from
  the ones that issue per-session keys in a console (`manual`: Instagram Live
  Producer, TikTok LIVE Center, Rumble), plus the simulcast prohibitions as
  data; stream keys stored in the encrypted credential store (never in
  `config.yaml`); and a generator that writes the MediaMTX forward-list config
  (mode 0600, keys never printed). `xpst stream {destinations,set-key,
  clear-key,relay-config}` is the surface. MediaMTX is an external child
  process like ffmpeg — no re-encode, no server, no spend. See
  `docs/STREAM-RELAY.md`. Proven end-to-end on this host: one ffmpeg ingest into
  the generated relay config arrived simultaneously at two separate MediaMTX
  destinations with identical byte counts and both H.264 + MP4A tracks.

### Fixed
- **`xpst connect <platform> --config <file>` writes to the file you named.**
  The wizard called `XPSTConfig.load()` with no path, so a connect run against
  any non-default config wrote the session file, its encrypted mirror and any
  other connect artifact into the default `~/.xpst` while reading settings from
  the named one — one decision split across two directories, and a silent
  mutation of an install the operator did not name. `run_connect` now takes the
  caller's config path (`connect` and `onboard` pass the one they loaded).
- **The YouTube upload limit is the documented bucket, not the retired
  1,600-unit arithmetic.** Google moved `videos.insert` into its own quota
  bucket on 2026-06-01 — 100 calls/day at 1 unit per call, separate from the
  10,000 units other endpoints share
  ([Quota Calculator](https://developers.google.com/youtube/v3/determine_quota_cost),
  last updated 2026-11-12). `utils/quota.py` still modelled 1,600 units inside
  the shared pool and labelled 5/day "Platform max: 6"; `xpst quota` printed
  `0/10,000 units` and the quota error hint repeated "~6 uploads". The
  documented platform ceiling (100/day) and xPST's own operator cap (5/day,
  configurable through `rate_limits.youtube`) are now separate, cited
  constants, and every surface prints both.
- **Threads analytics is no longer declared impossible.** `analytics.py` said
  Threads had no metrics API, so every metric was advertised as missing and
  `_collect_threads` returned `[]`. Meta documents
  `GET /{threads-media-id}/insights` with views, likes, replies, reposts,
  quotes and shares behind `threads_manage_insights`
  ([Threads Insights API](https://developers.facebook.com/documentation/threads/insights),
  updated 2026-01-30). The collector now reads that endpoint with the same
  encrypted credential the uploader uses, maps `replies` → `comments`, leaves
  `saves` genuinely missing, and reports an API error as a failure instead of
  a zero. Proven against a mocked transport — Threads stays disabled on this
  host, so no live claim is made.
- **X analytics says "these posts are gone" instead of "collection failed".**
  `xpst analytics --json` flagged X as a failed collection for 21 recorded
  posts, each raising `TweetNotAvailable: Tweet … not found in detail
  response`. Measured 2026-11-16: the twikit detail path is fine (a live post
  from the same account returned view_count 5853 through it) and all 21 ids
  were deleted upstream (public oEmbed 404 for every one). The collector now
  records that answer per post and reports the platform as
  `unavailable: post_not_found_upstream` with a count, kept out of
  `collection_failed_platforms` and out of the failure exit code; a payload
  that is genuinely not a detail response raises `TwikitDetailShapeError` and
  fails with the machine reason `twikit_detail_shape` after one sample
  instead of 21.
- **`--json` on the stream commands emitted nothing when the call failed.** The
  slice-1 commands passed a success flag where `json_output(data, as_json)`
  expects the `--json` value, so a failed `xpst stream destinations --json`,
  `set-key` or `relay-config` printed only prose and a script reading stdout got
  no JSON at all; the new failure paths would have inherited it. All stream
  commands now emit exactly one JSON object on stdout in JSON mode and keep the
  human text for the non-JSON path.
- **Two platform rows named the wrong credential gate.** The Twitch row said an
  app access token could read a stream key (it cannot — the endpoint requires a
  user token with `channel:read:stream_key`), and the Kick row named
  `GET /public/v1/stream/key`, which is not in Kick's published API index; the
  key arrives on `/public/v1/channels`. Both rows now match the call the code
  makes, and a test compares them.
- **A credential key that cannot be a file name is refused at the store, not at
  the rename.** The encrypted file store writes keys verbatim as `<key>.enc`;
  on Windows a key containing any reserved character fails at the atomic rename
  with an opaque `OSError [WinError 87]`. Keys are now validated up front with a
  message that names the key, and read/delete treat an unusable key as "not
  stored". Those keys are all underscore-namespaced, including the new
  `stream_destination_<platform>` stream credentials — found by CI on
  windows-latest, not by the Linux suite, which is why the guard exists.
- **`xpst preflight --transform` emits the rendition plan it was asked for.**
  The flag reached `build_route_plan(include_transform=…)` and the service
  computed the per-destination decision, but `RouteDestination` never
  serialised it, so the JSON carried nothing and only `verify-media --plan`
  showed it. Each destination now carries `transform` (or `transforms`, one
  entry per carousel item), and `preflight`/`post --dry-run` print it.
  `None` means "not computed", never "passthrough".
- **Advisories that used to be JSON-only are printed, and they say what will
  happen.** A soft ceiling (4K dimensions, 40 Mbps) and a sample rate that
  differs from a destination's native rate are warnings, not refusals — but
  nobody reading the human output ever saw them, so "would publish" looked
  lossless. They now print under each destination, the dimension/bitrate text
  says the destination re-encodes rather than rejects, and the audio-rate text
  says it is advisory only (YouTube is 48 kHz, X/TikTok 44.1 kHz, so no single
  file satisfies both and no re-encode is needed).
- **`--visibility` no longer reads as a capability every destination has.**
  Only YouTube forwards a visibility at upload time; X and TikTok take
  `visibility` on the *delete* path only and publish publicly. The plan
  accepted the flag for the whole request and still reported `ready: true`,
  so "nothing gets posted publicly" looked like a flag instead of a scope
  decision. `post` (dry run and live, JSON and human output) now names the
  destinations that will ignore it. The key is emitted only when there is
  something to warn about, so the default (public) payload shape is unchanged.
- **`xpst status` says its `platform_health` is historical, not current.** The
  counters come from local post outcomes — TikTok carried 195 "failures" from
  a retired API path while `xpst doctor` reported it ready. Each entry now
  carries `historical: true` / `source: state_history`, the payload carries a
  `platform_health_note` pointing at `xpst doctor`, and the human output's
  heading says which surface is the live authority.
- **A paused deployment can no longer queue a post behind the operator.** A
  manual post fired outside the posting window (08:00–23:00) is deferred to the
  schedule store — which *creates* `~/.xpst/schedule.json`, a schedule change
  nobody asked for. `schedule.posting_paused` (or `XPST_POSTING_PAUSED=1`)
  makes the deferral refuse instead: nothing is queued, nothing is uploaded, and
  the CLI exits non-zero with `{"paused": true, …}` while the MCP tool returns
  an error result. The guard sits in the deferral itself, so every surface that
  defers inherits it; an explicit in-window post or `--now` still publishes.
- **The post dry run reports deferral instead of silently implying it will
  post.** The notice lived after the dry-run block's `return`, so it could never
  print. The payload now carries a `deferral` object — queued (window closed) or
  refused (deployment paused), with the reason.
- **A pip/wheel install can serve the web UI, so `#/phone` is reachable from
  PyPI.** The wheel shipped no UI at all: `/` served the string placeholder and
  only the JSON API worked, so `xpst pair` handed a phone a code for a screen
  that did not exist. `packaging/hatch_ui.py` is now a hatch build hook that maps
  a built `ui/dist` (or `XPST_UI_DIST`) into the wheel as
  `xpst/dashboard/webui`, and `xpst.dashboard.server._ui_dist_dir()` finds it
  before the checkout path. The sdist carries the same build (`artifacts =
  ["ui/dist/**"]`), because pip and `python -m build` build the wheel *from* the
  sdist. The release, PyPI, public-safety and Docker lanes build the UI and set
  `XPST_REQUIRE_UI=1`, so a released artifact can never ship UI-less;
  `scripts/clean_install_smoke.py` fails a wheel with no bundle. When no bundle
  exists the placeholder page says so, `xpst ui` prints the fix, and the `--json`
  readiness line reports `"ui_built": false`.

### Changed
- **Every Meta Graph API call now uses one pinned, supported version (v26.0).**
  Instagram (container create/publish, media + container status, permalink,
  delete/reconcile, followers), the account-type probe, the connect/verify
  helpers, the Messenger send/health path and the Facebook Page client all build
  their URLs through `xpst.utils.graph_api`, so the pin is a single constant
  instead of per-module literals. v19.0 (removed 2026-05-21) and
  v20.0 (removed 2026-09-24) were still being called directly, and v21.0/v22.0
  (v21 removed 2027-01-21) were split across the other modules; the Instagram
  resumable-upload host and the OAuth consent dialog follow the same pin.
  `XPST_GRAPH_API_BASE` (whole base) and `XPST_GRAPH_API_VERSION` (version only)
  retarget the family; `tests/test_graph_api_version.py` fails if a new
  `graph.facebook.com/vNN` literal appears in `src/`.
- **Platforms are opt-in targets, never an implied "all" (R3).** `--platforms`
  no longer defaults to every enabled platform: an explicit set wins, `--all`
  is the explicit "every connected account that accepts this format", and the
  default is the format route ∩ connected accounts. `xpst post --json`, the MCP
  `xpst_post` result, `POST /api/post` and the schedule store now report and
  persist that resolved set; the scheduler persists the resolved destinations at
  add time instead of re-targeting an entry when a new account is connected.
- **Distinct destination states, including non-failures (R3 rule 4).**
  `posted`, `skipped_unconnected`, `unsupported_format`,
  `refused_by_community` and `failed` are one vocabulary across the CLI, MCP,
  the HTTP API and batch outcomes. A requested-but-unconnected destination is
  `skipped_unconnected` — visible, exit 0, not a failure — and zero connected
  accounts is one actionable error naming `xpst connect`.
- **Refuse rather than truncate, with the envelope in the message (R4 rule 4).**
  Over-limit captions and out-of-envelope video are refused with the
  destination's duration / container / codec / byte / caption limits named.

### Added
- **Bluesky is a destination: text, image and multi-image posts.** The AT
  Protocol is an open posting API — `com.atproto.repo.createRecord` writing an
  `app.bsky.feed.post` record, with no review, no fee and no developer console —
  so it is the cheapest real breadth addition to the catalog. Connecting is a
  handle plus an **app password** (`xpst auth bluesky`, or Settings), stored
  encrypted in the credential store and never in `config.yaml`; the returned
  session JWTs are cached and refreshed, so a queue drain does not burn
  `createSession`'s budget of 30 per 5 minutes. Posts are refused rather than
  truncated past 300 characters, links are emitted as richtext facets (so they
  are clickable, with UTF-8 byte offsets), and a carousel is a native 2–4 image
  embed — over four images is refused with the number, never dropped silently.
  **Video is not implemented** and not declared: Bluesky serves video from a
  separate service with an asynchronous upload job, and the capability table
  only claims what a live upload has proven. `PlatformUploader.upload` is no
  longer abstract — it refuses by name — so a text/image-only destination is a
  legitimate adapter instead of a class that cannot be instantiated.
- **The desktop app checks for updates on launch, and always shows what it
  found.** The updater used to run only when `XPST_UPDATER_CHECK=1` was set —
  an E2E harness variable — so an installed copy was never told a newer release
  existed. The UI now calls `updater_check` once per launch, shows a banner when
  a release is available, and About → Updates carries "Check for updates" and
  "Download and install". Installing is always a user action; the unattended
  check→install→relaunch behaviour and the `/private/tmp/xpst-updater-e2e`
  boot markers now live behind that same variable, and
  `XPST_UPDATER_CHECK=0` disables the network check entirely (the boot gate
  pins it).
- **Both macOS architectures are release builds, so both updater keys can carry
  a real payload.** The release matrix gains a native `x86_64-apple-darwin` leg
  on GitHub's `macos-15-intel` runner (PyInstaller cannot cross-build the engine
  sidecar), and each lane renames its updater archive to
  `darwin-aarch64-…`/`darwin-x86_64-…` instead of copying it and leaving the
  bare name behind.
- **`updates/latest.json` lost the `darwin-x86_64` entry that could not
  verify.** Both darwin keys carried the arm64 `sha512`/signature while the
  Intel key pointed at a different file (sha512 `a4ffe4a1…` against the arm64
  `2e969c3d…`), so an Intel Mac downloaded an update Tauri always refused. The
  manifest was regenerated with the repository's own generator against the
  published arm64 bytes; the Intel key returns when a real Intel build ships.
- **Manifest generation refuses the two shapes that produced that entry.**
  `scripts/select-updater-artifacts.py` reports a macOS bundle with no
  architecture in its name as `unattributable` instead of guessing "Intel", and
  picks NSIS over MSI deterministically instead of calling two signed Windows
  payloads `ambiguous`. `scripts/gen-updater-manifest.py` rejects two platform
  keys that describe different files with one `sha512`, and rejects a payload
  whose file type cannot install on that platform.
- **The release lane stopped dropping the Windows and Linux updater signatures.**
  `createUpdaterArtifacts: true` reuses the NSIS `.exe`, the `.msi` and the
  Linux `.AppImage` as the updater payloads and signs each in place, but the
  manifest job's flatten step copied the installers without `.exe.sig` /
  `.AppImage.sig`. Both platforms were therefore reported "unsigned, sidecar
  missing" while their signatures sat in the same build artifact, which is why
  no Windows or Linux key ever reached `latest.json`. The selector also
  prefers a signed payload over a better-ranked unsigned one, so a missing
  sidecar cannot mask a usable installer.
- **`scripts/publish-updater.sh --assets-dir` re-hashes the released bytes.**
  Every manifest URL must exist among the release assets, its `sha512` must
  match those bytes, and its signature must equal the published `.sig`; both
  publishing pipelines pass the directory they just downloaded.
- **A version tag can no longer ship unsigned installers.** `tauri-release.yml`
  fails a `v*` tag before building when the Apple or Azure signing credentials
  are absent, asserts the macOS bundle is Developer ID signed and the DMG is
  stapled, and asserts every Windows installer has a valid Authenticode
  signature. `dryrun-tauri-*` tags stay open for credential-free rehearsals.
  Missing accounts: Apple Developer Program ($99/yr) and Azure Trusted Signing
  ($9.99/mo) — see `docs/tauri-updater-production.md`.
- **M0: a phone can upload, preview, confirm, publish and read back.** `POST
  /api/media` accepts a multipart upload from a camera roll (size cap
  `XPST_MAX_UPLOAD_BYTES`, container sniffed from the bytes rather than the
  filename, ffprobe probe that reports `probed: false` with a reason instead of
  inventing a duration, staging under `<config dir>/media/uploads`,
  content-addressed `med_<sha256>` ids and idempotency keys). Device pairing
  (`xpst pair`, `xpst devices --revoke`, `POST /api/pair`) issues a single-use
  expiring code for a hashed per-device token, and a non-loopback bind now
  requires a credential for reads as well as writes — the static app shell stays
  public so an unpaired phone can reach the pairing screen. Frozen plans
  (`/api/plans*`) capture the media digest, caption, destinations and envelopes;
  `confirm_stale` returns what changed, publish is idempotent per destination,
  and a deferred destination keeps the plan retryable. Readback asks the
  platform (YouTube `videos.list`, X twikit) and reports `verified`,
  `submitted_unconfirmed`, `not_found`, `deferred` or `refused` — never a
  guessed success. The `#/phone` screen runs the whole flow at a 390 px
  viewport with 44 px targets and resumes from the plan id in the URL.
- **`deferred` is a canonical destination state.** A cooldown or anti-bot
  deferral used to classify as `failed`; it is now excluded from failure maths
  and carries its `resume_after` window.
- **The public-safety gate now catches LAN/tailnet IP addresses.** The
  scanner gained a `private_ip` category covering RFC1918 (`10/8`,
  `172.16/12`, `192.168/16`) and CGNAT/tailnet (`100.64.0.0/10`) host
  addresses. It gates the tree and built-artifact scans like every other
  category and is report-only in the history scan; loopback, `0.0.0.0`, CIDR
  network literals and public addresses (including the Alibaba metadata
  endpoint that sits inside the CGNAT block) are not findings. Fixtures whose
  subject really is a private address opt out per line with
  `# public-safety: allow-private-ip`, so the exemption is reviewable instead
  of exempting a whole directory.
- **A counted dry run is now the exit condition for the rate-limit pause.**
  `python -m xpst.rate_limit_dry_run` drives the real upload pipeline
  (`UploadService` → `retry_operation` → the real adapters → `CooldownStore`)
  across every destination and fault shape — healthy, 429 with no hint, 429
  with `Retry-After: 900`, a 403 rate-limit body, a 500 — plus a restart with
  persisted cooldown state, a 24-hour hard pause and cross-destination
  isolation, with the provider wire replaced by scripted fakes under a socket
  guard. It emits a machine-readable summary (posts that would have gone out,
  rate-limit responses, every cooldown decision with the `Retry-After` value
  used, dropped requests, unaccounted posts) and runs the matrix twice to prove
  the counts repeat. Every scenario runs in a fresh isolated config dir, so a
  persisted `rate_cooldowns.json` cannot be measured as the next run's own
  result. No credentials, no network, no real post. The failure-injection test
  doubles moved into `xpst.rate_limit_dry_run` so the suite and the command
  exercise one definition. See `docs/RATE-LIMIT-DRY-RUN.md`.
- **`xpst preflight --format auto` and the capability-driven route resolver.**
  One plan object (`xpst.services.route_plan`) resolves format, destination
  state and envelope from the single registry `xpst capabilities` emits —
  `format_routes` and per-destination `envelopes` are now part of that document
  (contract version 2), so the CLI, MCP, HTTP API, UI and resolver cannot drift.
  The MCP `xpst_preflight` / `xpst_post` tools accept `format` and `all`;
  `POST /api/preflight` returns the same `route_plan`. Everything is resolved
  before any network call, and `skipped_unconnected` is reported per
  destination instead of a silent drop.
- **Text posts survive the schedule store.** The store now has a no-media
  representation — a `content_type=text` entry with an empty media path — and
  every queue surface can create one: `xpst schedule add --text` (CLI),
  `xpst_schedule_add` without `video_path` (MCP), and window-gated manual
  text posts (which now defer like media instead of posting at midnight).
  Both fire paths (daemon pass and `xpst schedule run`) route entries
  through one shared resolver (`entry_fire_route`): a stored route wins, a
  legacy entry is classified from its media file, and a media file that is
  neither video nor image — the placeholder `.txt` older queue surfaces
  parked text behind — fires as TEXT through `post_text` instead of dying in
  the video encoder. The 16 legacy `.txt` entries in a live store fire
  correctly with no migration.
- **Recurring entries keep their modality.** A daily/weekly/monthly entry's
  next occurrence now carries `content_type` and `media_paths`; previously a
  recurring image or carousel silently fired its second time through the
  video encoder.
- **TikTok-review favicon parity for the public site.** TikTok production review
  rejected the xPST dev app because the submitted app icon (solid green square,
  1024x1024, sRGB #1F6F3C) did not match the site's browser-tab icon. The Pages
  site now serves that exact square as favicon.ico, 16/32/48 PNGs, an
  apple-touch-icon and 192/512 Android icons on every page;
  `tests/test_site_favicon_parity.py` pins the green, dimensions and `<link>`
  tags so a page rewrite can't regress the reviewer-visible state.
- **Bring-your-own developer app path — no App Review anywhere.** Every
  platform whose official API needs a developer app (Instagram, Threads,
  TikTok; Messenger/Facebook Page status) can now run on an app **you own**:
  a per-platform setup screen (Connect page, `xpst byo set`, MCP
  `xpst_byo_app`, `POST /api/byo/{platform}`) validates and stores your App
  ID + Secret **only** in the encrypted local store, answers everything
  masked (at most a four-char id tail — never the secret), and the stored
  pair is what lights up the in-app **Sign in** control: the consent dialog,
  code exchange and token extension all run through your app
  (`xpst.byo.finalize_meta_oauth_code`). Meta Standard Access means the app
  owner publishing to their own accounts needs no App Review and no Business
  Verification; the canonical catalog now exposes a per-platform `byo_app`
  block so the UI, CLI and agents see the same truth. TikTok sign-in now
  resolves client key/secret through the same store-first path. Details in
  `docs/setup-byo-app.md`.
- **Credential-health watchdog — warned days ahead instead of at failure time.**
  The `expiring` badge used a single 24-hour window, which is right for tokens
  xPST renews by itself but useless for a credential only a human can replace
  (an Instagram session, a Meta long-lived token with no refresh path): by the
  time it warned, the next post had already failed. Such credentials now warn
  **7 days** out, and `xpst auth status` carries a new `credential_health`
  report — `needs_attention` with a per-platform deadline and the exact
  `xpst connect <platform>` fix, `unverifiable` for providers that report no
  expiry (stated, never guessed), and `not_applicable` for disabled,
  source-only and non-account surfaces. The same report rides the MCP tool and
  the dashboard health-status payload, so the app, an agent and the CLI all
  agree. Derived from the existing live badges: no extra probing, no invented
  deadlines.
- **Rate-limit cooldowns are visible on every surface that reports health.** An
  active cooldown used to exist only inside the upload path, so `xpst health`,
  `xpst doctor` and the MCP health tool could report a platform ready while a
  persisted cooldown was holding it. They now report the cooldown state, its
  `until`, and the source file, alongside the readiness verdict (#318).
- **A durable in-app scheduling engine with timezone-correct firing.** Schedule
  entries are persisted with the destination set resolved at add time, each entry
  carries its own zone, and `run-due` compares instants rather than naive local
  times — so a job scheduled across a DST transition fires when the user's wall
  clock reaches it (#259).
- **Per-platform caption overrides survive the schedule store.** A caption
  override set for one destination is persisted with the entry and applied when
  it fires, rather than being flattened back to the shared caption at queue time
  (#278).
- **Per-destination caption overrides and hard caption-limit blockers.** A post
  can carry a caption per destination, and a caption that exceeds a
  destination's limit is a blocker naming that limit instead of a silent
  truncation (#254).
- **TikTok has a browser-native publisher route.** Opt-in
  `publish_mode: browser` drives TikTok Studio with a persisted browser profile
  and reports typed stage codes; it either returns a receipt or fails, and an
  unconfirmed attempt is never re-published (#304).
- **An unaudited TikTok client falls back to an inbox draft.** When Direct Post
  is refused for an unaudited Content Posting API client, the uploader falls
  back to the inbox-draft path instead of failing the destination outright
  (#266, #300).
- **Instagram account type is detected at connect time.** `xpst connect
  instagram` reads the account type and walks the Creator-account switch when
  the API requires it, instead of failing the first publish on a personal
  account (#306).
- **A Rust engine scaffold with a checked-in CLI parity manifest.** `crates/*`
  carries the workspace, the clap tree generated from the Python CLI's own
  manifest, and config/state read paths, gated by a dedicated CI lane; an
  unimplemented Rust command exits with the documented `NOT_IMPLEMENTED` code
  rather than pretending to succeed (#329).
- **Long-form YouTube, X's real envelope, and Rumble as integration-uncertain.**
  YouTube gains explicit Shorts/long modes, X reads its configured maximum
  duration when one is set instead of assuming the 140 s standard envelope, and
  Rumble is declared opt-in with `integration_uncertain` and an honest
  manual-handoff path instead of a fabricated success (#339).

### Changed
- **Every Meta Graph API call now uses one pinned, supported version (v26.0).**
  Instagram (container create/publish, media + container status, permalink,
  delete/reconcile, followers), the account-type probe, the connect/verify
  helpers, the Messenger send/health path and the Facebook Page client all build
  their URLs through `xpst.utils.graph_api`, so the pin is a single constant
  instead of per-module literals. v19.0 (removed 2026-05-21) and
  v20.0 (removed 2026-09-24) were still being called directly, and v21.0/v22.0
  (v21 removed 2027-01-21) were split across the other modules; the Instagram
  resumable-upload host and the OAuth consent dialog follow the same pin.
  `XPST_GRAPH_API_BASE` (whole base) and `XPST_GRAPH_API_VERSION` (version only)
  retarget the family; `tests/test_graph_api_version.py` fails if a new
  `graph.facebook.com/vNN` literal appears in `src/`. (#349)
- **Platforms are opt-in targets, never an implied "all" (R3).** `--platforms`
  no longer defaults to every enabled platform: an explicit set wins, `--all`
  is the explicit "every connected account that accepts this format", and the
  default is the format route ∩ connected accounts. `xpst post --json`, the MCP
  `xpst_post` result, `POST /api/post` and the schedule store now report and
  persist that resolved set; the scheduler persists the resolved destinations at
  add time instead of re-targeting an entry when a new account is connected.
- **Distinct destination states, including non-failures (R3 rule 4).**
  `posted`, `skipped_unconnected`, `unsupported_format`,
  `refused_by_community` and `failed` are one vocabulary across the CLI, MCP,
  the HTTP API and batch outcomes. A requested-but-unconnected destination is
  `skipped_unconnected` — visible, exit 0, not a failure — and zero connected
  accounts is one actionable error naming `xpst connect`.
- **Refuse rather than truncate, with the envelope in the message (R4 rule 4).**
  Over-limit captions and out-of-envelope video are refused with the
  destination's duration / container / codec / byte / caption limits named.
- **The product direction is an all-Rust engine, recorded in the repo.**
  `docs/DIRECTION.md` (accepted 2026-11-03) makes the Rust engine the target,
  with zero personal data as a hard constraint and a lightweight set of
  destinations; the Python engine stays the reference implementation until
  parity is proven (#326, #337).
- **The lockfile is regenerated and gated.** `uv.lock` is committed, CI runs
  `uv lock --check` with uv pinned to 0.13.4, and the pin's setup action moved
  to v7; the ghost desktop extra was dropped in the same pass (#268, #321,
  #361).
- **PyPI publishing uses the credential path that actually exists.**
  `publish-pypi.yml` uploads with the stored token secret, `docs/PYPI.md`
  documents that path (and is drift-tested against the workflow), and the
  release lane reports which credential path a run took before it uploads
  instead of discovering it in the upload step (#327, #331, #335).

### Fixed
- **A credential key that cannot be a file name is refused at the store, not at
  the rename.** The encrypted file store writes keys verbatim as `<key>.enc`;
  on Windows a key containing any reserved character fails at the atomic rename
  with an opaque `OSError [WinError 87]`. Keys are now validated up front with a
  message that names the key, and read/delete treat an unusable key as "not
  stored". Those keys are all underscore-namespaced, including the new
  `stream_destination_<platform>` stream credentials — found by CI on
  windows-latest, not by the Linux suite, which is why the guard exists.
- **The phone screen's `Revoke` control is a 44 px tap target.** It rendered as
  inline text at 41×16 px; a measured 390 px run flagged it, and it now carries
  the same minimum target box as the readback URLs, pinned by
  `ui/tests/phone-flow.test.js`.
- **`xpst post --dry-run` can no longer report `"ready": true` while listing a
  hard blocker.** Readiness was derived from the destination plan alone, so a
  request the content contract refused — a text post to TikTok, which publishes
  video only — printed `ready: true` next to the blocker that refused it (and
  next to a sibling destination that would post) while the same invocation
  exited 1. Any gate reading the field passed a request the command itself would
  not publish. Readiness now requires an empty blocker list, which is the answer
  the MCP dry run and the HTTP preflight already gave; the exit-status contract
  (a pre-upload content refusal exits 1, a per-destination blocker is reported
  per destination and does not abort the run) is unchanged. (#351)
- **A TikTok API throttle is believed for the window TikTok asked for, not the
  60 s floor.** `TikTokUploader._handle_http_error` mapped a 429 to a static
  `TIKTOK_RATE_LIMITED` string and never read the response, so the persistent
  cooldown — which takes a provider-declared wait only from that error text —
  engaged at the escalation floor while the provider had asked for 900 s, and
  the retry layer spent all four attempts inside a window it could not wait
  out. The 429 text now carries `Retry-After: <n>` (header, else a hint in the
  JSON envelope), which also lets the retry layer answer a beyond-budget wait
  exactly once. The prefix stays intact for diagnostics, and the browser route
  is untouched: Playwright sees no HTTP status at all.
- **One throttled Instagram (instagrapi session mode) post no longer costs 16
  provider requests.** The uploader retried a rate limit 4× internally and the
  service retried that whole upload 4× more, so a single throttled post made 16
  calls and slept ~96 s before giving up — on the mode
  `instagram.auth_mode: session` actually runs, on the platform that caused the
  2026-10-01 halt. The uploader now hands a throttle straight back and the
  service's retry layer owns the retry (4 attempts, ~14 s, one request each),
  which also matches the Instagram Graph path. A recovered throttle is now
  visible to the cooldown state machine instead of being swallowed. Ceilings are
  pinned in `tests/test_rate_limit_failure_injection.py`, and the retry economy
  is now executable policy: `xpst.utils.request_budget` derives the worst case
  from the live uploader/service constants and
  `tests/test_retry_economy_budget.py` fails loudly if a nested retry layer
  comes back.
- **A first TikTok publish from an unaudited dev app no longer dies with a
  403.** Direct Post hardcoded `privacy_level: PUBLIC_TO_EVERYONE`, but TikTok
  forces an unaudited Content Posting API client to private privacy — the
  first publish returned
  `403 unaudited_client_can_only_post_to_private_accounts`. The uploader now
  asks `creator_info/query` which levels the client may actually post with and
  uses the best permitted one (SELF_ONLY for unaudited apps), cached per
  process to respect the 6 req/min limit; the level is reported in result
  metadata. The pre-check is fail-open, so the reactive inbox-draft fallback
  still covers refusals.
- **The Messenger webhook intake accepted unsigned payloads.** With no
  `X-Hub-Signature-256` header and no `app_secret` configured (the default
  install), `POST /webhook/messenger` returned `200` — the dispatch was a no-op
  only for as long as Messenger stayed disabled, so an unauthenticated local
  caller (or anyone reaching a non-loopback bind) could drive the intake path.
  Meta always signs its deliveries, so an unsigned request is now refused with
  `403`, and a signature xPST cannot verify (no `app_secret`) is refused too.
  The `GET` subscription handshake also fails closed when no `verify_token` is
  configured, instead of completing the handshake for anybody who asks.
  Docs (`docs/DASHBOARD.md`, `docs/setup-messenger.md`) updated with the two new
  refusal reasons.
- **The MCP server runs on mcp 2.x as well as 1.x.** `mcp[cli]` is now allowed up
  to `<3.0.0`. mcp 2.0 removed `Server.list_tools`/`Server.call_tool` decorator
  registration (handlers are constructor keywords taking
  `(request_context, params)`) and renamed the model fields
  `isError`/`inputSchema`/`serverInfo` to snake_case. `src/xpst/mcp/server.py`
  now registers against whichever SDK line is installed and reads those fields
  through three accessors (`result_is_error`, `tool_input_schema`,
  `initialization_server_info`), so the same 41-tool registry, typed schemas and
  fail-closed guards serve either version. Proven end to end on 2.3.0 and
  1.30.0: `tools/list` returns all 41 tools and the refusal paths still refuse
  without `XPST_MCP_ALLOW_MUTATIONS`. `scripts/mcp_client_smoke.py` also stopped
  assuming the argument guard lets a missing media path through to preflight.
- **A retry can no longer duplicate a post whose outcome is unknown.**
  Reconciliation runs before any retry, so an attempt that may have reached the
  platform is read back rather than re-sent (#257).
- **The CLI exit verdict reaches MCP.** `run` and `backfill` returned an
  optimistic result over MCP while the CLI exited non-zero; the batch verdict is
  now part of the tool result, so an agent sees the same answer the shell does
  (#262).
- **A refused dry run carries the same top-level error as the real path.**
  `xpst_post` in dry-run mode returned the refusal only inside the payload
  (`isError` false), so a caller that checked the tool error treated a refused
  request as a success (#274).
- **Text is never gated by the video path, and `--now` is honoured on every
  route.** The deliberate-now bypass applied to media posts only, so an explicit
  text post could be deferred by the posting window while the media route
  published (#307).
- **Every platform's duration cap was silently dead.** The upload guard called
  `manifest()` on a property, so the call raised before any comparison and no
  cap was ever enforced; the guard now reads the registry value and refuses an
  out-of-envelope file with the destination's own numbers (#282).
- **Facebook had no encode branch.** It was declared a video destination but
  no encoder produced its rendition, so a Facebook-bound post fell through the
  encode step; the branch exists and is covered by the encode matrix (#283).
- **yt-dlp is resolved without relying on `PATH`, and a non-zero exit is no
  longer collapsed into success.** The TikTok source reported a source fetch it
  never completed; it now resolves the binary, distinguishes rc 0 from a real
  failure, and degrades health honestly (#285).
- **A failed source fetch is recorded in state.** Source-fetch failures were
  dropped, so `xpst health` and `xpst doctor` could report an all-clear channel
  whose sources had been failing; they now surface the recorded failure
  (#310).
- **`mark_video_posted` persists through the transactional seam.** The write
  bypassed the transaction, leaving a window where a crash after upload could
  lose the record and re-post the same file (#286).
- **Live analytics collect again, and a silent zero is reported as a failure.**
  The dashboard read `post_id` where the state stores `id`, so every live metrics
  query returned nothing; the collector now reads the platform id under either
  key, and a collector that returns zero records says so instead of reporting a
  healthy zero (#256, #276).
- **A hidden count stays missing end to end.** Platforms that suppress a
  metric returned `0`, which read as a real measurement; the field is now absent
  with the dashboard labelling the age of the last successful read (#313).
- **Analytics writes can no longer escape the configured config directory.**
  `AnalyticsStore()` and `BestTimeAnalyzer()` defaulted to `~/.xpst/analytics.db`,
  so tests whose conftest sandboxed `XPST_CONFIG_DIR` recorded rows into the
  operator's live ledger. The default now resolves from the config dir and the
  engine passes it explicitly (#330).
- **`XPST_CONFIG_DIR` owns its credential files.** A config-dir override still
  resolved credentials from the default location, so two configurations shared
  one credential store (#271).
- **Threads waits for a container to finish before publishing.** The adapter
  published as soon as the container existed, which failed for any real video;
  it now polls the container status to `FINISHED` and surfaces Meta's own
  `error_message` when it does not (#302).
- **The socials-QA defect wave is closed.** Deferral semantics, routing,
  configuration isolation, delete-by-id and caption truth were fixed together,
  along with the residual D2/D3/D4/D6/D7 findings and the MCP `serverInfo`
  identity (#277, #301).
- **The stranger-install friction found on v1.2.1 is fixed.** The documented
  install steps, the missing-config path and the credential prompts were
  corrected against a clean-machine proof (#279), and the failure counts quoted
  in the gap list were corrected to match what that proof actually produced
  (#264, #265).
- **The published-artifact E2E installs the installers instead of exec'ing
  them.** Exec'ing a `.exe`/`.AppImage` starts an installer (or dies in the
  loader), never the app, so the harness reported a fake engine-health timeout.
  The lanes now `apt-get install ./pkg.deb`, run the NSIS installer with `/S`,
  report the child's exit code and log tail, and the packaged-UI and sidecar
  probes retry over a bounded window instead of sampling once (#347, #353,
  #354).
- **The pause guard is asserted against the store the CLI actually writes.**
  The regression read the in-memory `config.config_dir`, which is not the file's
  directory, so the "nothing was queued" assertion was vacuously true; it now
  globs the sandbox for `schedule.json` (#360).
- **Host-dependent test assumptions no longer turn the suite red off CI.**
  Three cases assumed a host layout, and the night-text-post case asserted a
  force-publish that never happened; both are pinned to their real contracts
  (#314, #316).
- **Nine X/YouTube QA edge cases are pinned as dry-run regressions** so the
  behaviours found by hand cannot silently regress (#312).
- **The Rust CI lane builds on Intel macOS.** The lane installs protoc on the
  `macos-15-intel` runner, without which lancedb cannot build and the leg fails
  for a toolchain reason rather than a code one (#364).
- **`xpst preflight --transform` emits the rendition plan it was asked for.**
  The flag reached `build_route_plan(include_transform=…)` and the service
  computed the per-destination decision, but `RouteDestination` never
  serialised it, so the JSON carried nothing and only `verify-media --plan`
  showed it. Each destination now carries `transform` (or `transforms`, one
  entry per carousel item), and `preflight`/`post --dry-run` print it.
  `None` means "not computed", never "passthrough". (#357)
- **Advisories that used to be JSON-only are printed, and they say what will
  happen.** A soft ceiling (4K dimensions, 40 Mbps) and a sample rate that
  differs from a destination's native rate are warnings, not refusals — but
  nobody reading the human output ever saw them, so "would publish" looked
  lossless. They now print under each destination, the dimension/bitrate text
  says the destination re-encodes rather than rejects, and the audio-rate text
  says it is advisory only (YouTube is 48 kHz, X/TikTok 44.1 kHz, so no single
  file satisfies both and no re-encode is needed).
- **`--visibility` no longer reads as a capability every destination has.**
  Only YouTube forwards a visibility at upload time; X and TikTok take
  `visibility` on the *delete* path only and publish publicly. The plan
  accepted the flag for the whole request and still reported `ready: true`,
  so "nothing gets posted publicly" looked like a flag instead of a scope
  decision. `post` (dry run and live, JSON and human output) now names the
  destinations that will ignore it. The key is emitted only when there is
  something to warn about, so the default (public) payload shape is unchanged. (#358)
- **`xpst status` says its `platform_health` is historical, not current.** The
  counters come from local post outcomes — TikTok carried 195 "failures" from
  a retired API path while `xpst doctor` reported it ready. Each entry now
  carries `historical: true` / `source: state_history`, the payload carries a
  `platform_health_note` pointing at `xpst doctor`, and the human output's
  heading says which surface is the live authority. (#358)
- **A paused deployment can no longer queue a post behind the operator.** A
  manual post fired outside the posting window (08:00–23:00) is deferred to the
  schedule store — which *creates* `~/.xpst/schedule.json`, a schedule change
  nobody asked for. `schedule.posting_paused` (or `XPST_POSTING_PAUSED=1`)
  makes the deferral refuse instead: nothing is queued, nothing is uploaded, and
  the CLI exits non-zero with `{"paused": true, …}` while the MCP tool returns
  an error result. The guard sits in the deferral itself, so every surface that
  defers inherits it; an explicit in-window post or `--now` still publishes. (#359)
- **The post dry run reports deferral instead of silently implying it will
  post.** The notice lived after the dry-run block's `return`, so it could never
  print. The payload now carries a `deferral` object — queued (window closed) or
  refused (deployment paused), with the reason.
- **A pip/wheel install can serve the web UI, so `#/phone` is reachable from
  PyPI.** The wheel shipped no UI at all: `/` served the string placeholder and
  only the JSON API worked, so `xpst pair` handed a phone a code for a screen
  that did not exist. `packaging/hatch_ui.py` is now a hatch build hook that maps
  a built `ui/dist` (or `XPST_UI_DIST`) into the wheel as
  `xpst/dashboard/webui`, and `xpst.dashboard.server._ui_dist_dir()` finds it
  before the checkout path. The sdist carries the same build (`artifacts =
  ["ui/dist/**"]`), because pip and `python -m build` build the wheel *from* the
  sdist. The release, PyPI, public-safety and Docker lanes build the UI and set
  `XPST_REQUIRE_UI=1`, so a released artifact can never ship UI-less;
  `scripts/clean_install_smoke.py` fails a wheel with no bundle. When no bundle
  exists the placeholder page says so, `xpst ui` prints the fix, and the `--json`
  readiness line reports `"ui_built": false`.

### Security
- **The zero-personal-data gate is real and checked in.**
  `.public-safety-denylist` is the live deny list, `scripts/scan_public_safety.py`
  loads it in tree, artifact and history modes, and CI plus the release lanes run
  it; synthetic Instagram fixtures and the two deny-list hits that had landed on
  `main` were cleaned up in the same effort (#252, #328, #333).
- **The Messenger webhook intake fails closed.** An unsigned payload with no
  configured `app_secret` was accepted with `200`, and the `GET` subscription
  handshake completed for anyone who asked; both are now refused, and the docs
  name the two refusal reasons (#272).
- **`XPST_MCP_ALLOW_ANY_PATH` no longer disables URL validation.** The flag
  exists to relax local path checks, but it also switched off the scheme/host
  validation on remote URLs, so an agent granted path freedom could be pointed
  at an arbitrary endpoint (#287).
- **The security audit script checks its assertions.** It reported passes for
  conditions it never evaluated, which made the audit itself worthless as
  evidence (#284).

## [1.2.1] — 2026-09-25

### Fixed
- **Auto-updater actually works now.** The release updater signing key had no
  private half in any repo secret or on the signing machine, so every earlier
  release shipped unsigned updater artifacts and the update channel stayed
  404. Rotated to a fresh keypair whose private half is stored as a repo
  secret (#249); the next tagged build signs `latest.json` and the updater
  manifests, so installed apps can self-update from this release forward.
  (The old key never signed anything, so no trust anchor is lost.)
- One-off CI experiment workflow removed from the default branch.

## [1.2.0] — 2026-09-25

### Added
- **In-app post deletion**: the Library page can delete or unpublish any verified
  post through the engine's delete contract (`POST /api/posts/{id}/delete`);
  per-platform outcomes are explicit and a refused deletion is never reported
  as removed. YouTube supports reversible soft-unpublish.
- **Appearance control**: light / dark / auto theme in Settings, remembered
  across launches and applied before first paint (no flash).
- **Grouped navigation**: the sidebar is organised Post / Plan / Review / System
  so the daily loop reads first.
- **Working actions on formerly read-only pages**: cancel a schedule entry
  (Schedule), retry a recorded failure through one-shot recovery (Activity),
  return to Compose from any tracked post (Videos).
- Durable compose drafts, per-destination copy overrides, and in-app platform
  sign-in flows.
- Remote media door: post media by URL without pre-downloading.

### Changed
- **One desktop app.** The legacy PySide6/QML shell and its build lanes are
  removed; the Tauri shell + Python engine is the only desktop product
  (~108 MB bundle, ffmpeg no longer bundled).
- One `content_type` contract across CLI, MCP and HTTP; truthful exit codes
  for `run`, `backfill` and `run --due`; honest source-only posting badges.

### Fixed
- App icon now renders the real brand artwork in the dock (regenerated from
  `assets/icon.png`; was solid-green placeholders in every earlier Tauri build).
- About page reports the true running version; engine-starting state no longer
  flashes a raw error card on first paint; health probe caching boundary fixed;
  encode cache codec fix.

### Removed
- The legacy PySide6/QML desktop app (see [Unreleased] notes in previous
  versions for the full removal list).

### Removed
- **The legacy PySide6/QML desktop app is gone; there is exactly one desktop
  app.** `src/xpst/desktop_app/` (23 files), its `build_macos.spec` /
  `build_windows.spec` / `build_linux.spec` PyInstaller specs, `build.sh`,
  `scripts/verify_desktop_package.py`, `scripts/verify_qml_pages.py` and
  `scripts/verify_macos.sh` were deleted, the three PyInstaller lanes were
  removed from `.github/workflows/release.yml`, and the Qt desktop smoke steps
  were removed from `ci.yml`. The legacy build emitted a second `xPST.app`
  whose bundle identifier (`com.tysais.xpst`) collided exactly with the Tauri
  product, so a published installer could not be attributed to the app it came
  from. The desktop app is now only the Tauri 2 shell in `src-tauri/` plus the
  Python engine sidecar built by `scripts/build-engine.sh`, published by
  `.github/workflows/tauri-release.yml`.
- **`desktop` extra and the `xpst build` command.** `pip install xpst[desktop]`
  installed PySide6 and nothing else shipped that used it; `xpst build` existed
  only to run PyInstaller against the three deleted desktop specs. `full` is now
  `xpst[mcp,dashboard,windows,knowledge]`. `xpst app` still exists but now
  launches the installed Tauri app instead of importing a Python GUI module.
- **Qt/PySide6 license notices** (`NOTICES_QT_LGPL.md`) and the PySide6 rows in
  `NOTICES.md`, `NOTICE.md` and `LICENSING_REPORT.md`: nothing in the shipped
  product links Qt any more.

### Security
- **Mutating dashboard routes now require authentication by default.**
  `POST /api/post`, `POST /api/connect/{platform}`, `POST /api/onboarding*`,
  `POST /api/preflight` and the `/bio/edit` form save were only protected when
  `monitoring.dashboard_username` / `dashboard_password_hash` happened to be
  configured — loopback is not an authorisation boundary, so any process on the
  machine (or any page open in a browser) could trigger a real post or start a
  connect flow. Every mutating route now fails closed with `401`, accepting
  either the dashboard Basic login (when configured) or the new dashboard API
  token.
- **New dashboard API token** (`xpst auth api-token`, `--rotate`): generated on
  first run and stored in the encrypted credential store
  (`dashboard_api_token`) like the platform OAuth tokens — never in
  `config.yaml`, and no default value ships with the project. Operators, scripts
  and agents can send it as `Authorization: Bearer <token>` or
  `X-API-Token: <token>`, or supply `XPST_API_TOKEN`.
- **Desktop/UI token hand-off**: the Tauri shell mints a per-launch token
  (`XPST_UI_TOKEN`) and opens its webview at
  `http://127.0.0.1:<port>/#xpst_token=…`; `xpst ui` does the same for the
  browser it opens. The token is never embedded in served HTML, and the SPA
  strips the fragment from the address bar immediately. Read-only routes keep
  their previous behaviour so the UI is never locked out; `POST /oauth/callback`
  and the Messenger webhook stay public by design. See SECURITY.md and
  docs/DASHBOARD.md.
### Added
- **Composer media preview** — the Compose screen now shows the selected asset
  before it is posted: images render from a cached, ffmpeg-generated
  thumbnail, and videos render as a real playable `<video>` element with the
  generated frame as its poster. Two new engine routes back it:
  `GET /api/media/stream` (HTTP range support — real `206 Partial Content`
  answers — serving the file through a bounded 1 MiB chunk iterator) and
  `GET /api/media/thumb` (single-frame JPEG, cached under
  `~/.xpst/cache/previews`, keyed by path + size + mtime). Selecting or
  scrubbing a large video never reads the whole file: the engine holds at most
  one chunk and the webview pulls only the ranges it needs.
- **Native file picker and drag-and-drop in the composer** — the desktop shell
  opens the OS file picker (`tauri-plugin-dialog`) and forwards native drops
  into the page as paths, so a picked or dropped file becomes the selection
  immediately. The shell grants the dialog command to the loopback engine
  origin only (`src-tauri/capabilities/default.json`). Outside the app window
  the composer says the picker is unavailable instead of inventing a path.

### Fixed
- **A platform's download source was reported as a posting destination.**
  `xpst doctor --json` derived its per-platform `connected` boolean from the
  platform-level `authenticated` flag, which for TikTok (auth mode
  `source_only`) is the verdict of its *download* probe. A healthy source
  therefore read as `"connected": true, "problem": null` — a promise that the
  posting destination exists when the engine has none until TikTok approves a
  Content Posting API app. TikTok is now reported as `source_only` /
  `can_post: false` with the reason attached, on every surface: `xpst doctor`
  (as an informational `notes` entry, never an `issue`, so a healthy machine
  still exits 0), `xpst health`, `xpst readiness`, `xpst connect --test
  --json`, `xpst onboard`, `POST /api/connect/<platform>`, `/api/providers`,
  `/api/health-status`, the MCP status tools and the desktop app's
  Connect/Compose/Accounts screens. Posting truth is computed once, in
  `xpst.provider_truth.posting_truth`, and every surface consumes that result
  instead of inferring "connected" from whichever flag was at hand.

- **The Home readiness panel no longer shows three identical-looking rows.** A
  platform holds several roles (source, video destination, analytics), and the
  panel rendered one row per role while printing only the platform — so the
  captured Home screen showed `Instagram / degraded / Review` three times with
  nothing telling the rows apart. Each row now names its role
  (`Instagram · Destination · Degraded`) and a row with nothing to say is not
  rendered at all. The rows, the verdict and the copy come from one
  role-level list, `xpst.readiness.role_readiness`.
- **One readiness verdict instead of three.** The Home Readiness card said
  "Needs attention" while `GET /api/onboarding` answered `ready: true` /
  "Ready to post." — it built its report from the config alone, so a stored but
  server-rejected Instagram session counted as ready — and the "Engine health
  (last recorded)" pill said "Degraded" beside its own only row, "YouTube / OK".
  `/api/onboarding` now renders the same live probe verdict as
  `xpst doctor`/`xpst auth status` (PR #228's canonical status), both endpoints
  serve the SAME readiness document (including the `verdict` the UI prints),
  a destination whose session was rejected is never `destination_ready`, and
  the recorded-health pill reports the recorded platform block it sits next to.
- **Undefined design token** — `--xpst-color-primary-soft` was referenced by
  the selected/hover states but never defined in `tokens.css`, so those states
  silently rendered transparent in both themes. Defined for light, dark-theme
  and `prefers-color-scheme: dark`.
- **`xpst health` and `xpst doctor` can no longer disagree about who is
  connected.** `health` probed platforms through the engine while `doctor`
  rendered the canonical collector, so one machine could answer
  `instagram: session expired, re-run connect` and `instagram: ready` for the
  same account minutes apart (the old `doctor` only checked that a session
  *file existed*). Both now render one implementation —
  `xpst.auth_status.platform_health_entries` — and the platform block of
  `xpst_health` (MCP) does the same. TikTok's source-only state stops reading
  as "not authenticated" in `health` while every other surface says
  `source_only`.
- **A failed live probe is no longer reported as an expiry it never proved.**
  Every Instagram probe failure — transport errors, `429`/`5xx`, challenges and
  Instagram's anti-bot 302 redirect loop — used to print "Instagram session
  expired or invalid. Re-run: xpst connect instagram (username/password
  required for re-login)", a diagnosis nothing had observed and one that sends
  the user into the ban-risky password path. Probe failures are now classified
  (`xpst.utils.probe_errors`): a provider rejection (Instagram's
  `403 login_required` + "You've been logged out") keeps the re-login
  instruction and carries the provider's raw response; anything unproven is
  reported as an **unverified** probe (raw error + retry, `badge: unknown`, not
  `needs_reauth`) on every surface — `health`, `doctor`, `auth status`, MCP and
  the web UI. The raw error is no longer discarded at `logger.debug`.
### Added
- **Honest token state + truthful badges** — `xpst auth status --json` now
  reports, per platform, a `token_state` / `badge` (`connected`, `expiring`,
  `needs_reauth`, `source_only`, `disabled`, `unknown`), a `badge_reason`
  explaining it and the `checked_at` timestamp the live check was taken. A
  green `connected` badge requires a passing live check on a fresh probe:
  a stored credential, a stale check or an unchecked platform renders
  `unknown`/`needs_reauth`, and source-only (TikTok) or disabled platforms
  never render as connected. The same badge is what the web UI, the desktop
  app and the MCP `xpst_auth_status` tool render, so no surface can disagree.
- **`xpst refresh-tokens`** and **`xpst auth status --refresh`** — bounded
  automatic refresh of expiring/expired access tokens (attempt budget,
  exponential backoff, wall-clock deadline; no prompts; nothing token-shaped
  is ever printed or persisted). The desktop health tick, the web API's
  background probe and `POST /api/refresh-tokens` run the same job, and the
  outcome is recorded in `~/.xpst/token_refresh.json` (0600) so a failed
  refresh keeps the badge at `needs_reauth` instead of promising a silent
  retry.

## [1.1.0] - 2026-09-04

### Added
- **Max-fidelity video pipeline** (`xpst/media`) — per-platform ingest
  spec matrix + `verify_media` pre-flight; transcode decision tree that
  prefers passthrough, then ZERO-LOSS stream-copy remux (`-c copy` into
  MP4 `+faststart` when only the container is foreign), then the
  platform-profile transcode; two-pass EBU R128 loudness normalization
  per platform (YouTube/TikTok/Instagram −14 LUFS, X −16 LUFS, TP −1.5)
  in linear mode; two-pass x264 encoding for bitrate-targeted profiles
  (YouTube 8 Mbps, X 10 Mbps); pre-upload spec verification wired into
  every upload (hard errors block, warnings attach to quality metadata).
- **`xpst verify-media FILE [-p PLATFORM] [--plan] [--json]`** — check a
  media file against platform specs before upload, with an optional
  dry-run transformation plan; exit 1 on blocking errors.
- **`xpst serve` daemon supervisor** — a single supervised long-running
  process that acquires the engine pidfile (safely rejecting a live holder,
  overwriting stale pidfiles left by crashed processes, releasing on
  graceful shutdown), runs the configured scheduler loop (reusing the
  existing Scheduler/ScheduleManager; scheduled posts + new-video watch
  checks), and optionally serves the FastAPI dashboard. Handles
  SIGTERM/SIGINT (clean shutdown) and SIGHUP (continue) and emits
  launchctl/systemd-friendly start/health/stop log lines. Flags:
  `--no-dashboard`, `--port`, `--host`, `--interval`, `--source`.
- **Consistent pidfile handling across commands** — `run`, `watch`, `post`,
  and the desktop `app` all route through the shared pidfile helper
  (`xpst.utils.pidfile`). Automatic engine loops (`run`/`watch`/`serve`)
  hold the exclusive lock and release it on exit (a one-shot `run` no longer
  leaves a stale pidfile behind); manual actions (`post`, desktop) use
  advisory verify-and-warn semantics so they work alongside a running
  daemon.
- **`xpst schedule install` now launches `xpst serve`** — the macOS
  LaunchAgent runs the supervisor continuously (RunAtLoad + KeepAlive), the
  Linux crontab entry is a pidfile-guarded, idempotent keep-alive tick, and
  the Windows scheduled task runs `serve` at logon. Legacy `schedule run`
  cron/plist/task entries are cleaned up on install and uninstall.
- **`get_last_wake_check()` on state managers** — implements the accessor
  the scheduler's sleep/wake catch-up heuristic already depended on
  (previously a latent `AttributeError` silently swallowed by `watch`).

### Fixed
- A one-shot `xpst run` no longer leaves a stale `xpst.pid` behind (the
  pidfile is always released on exit).

## [1.1.0] - 2026-09-06

Desktop reliability and release metadata fixes: the macOS bundle now reports
its project version, embeds the exact source commit, restores off-screen windows
onto the primary display, and enables Qt Quick accessibility before QML loads.

## [1.0.0] - 2026-08-18

First public release. xPST is an open-source, cross-platform suite for posting
short-form video to every major platform from one place, with built-in
analytics, scheduling, a desktop app, and AI-agent integration.

### Added
- **Six-platform posting**: YouTube Shorts, X, Instagram Reels, TikTok,
  and Threads — supported across the engine, scheduler, web dashboard,
  desktop app, analytics, and connection flow.
- **Facebook Messenger platform** (opt-in, disabled by default) — a
  ManyChat-lite auto-reply/chatbot option. Static Page Access Token auth (no
  refresh), direct `httpx` against the Graph API (`v22.0/me/messages`),
  `appsecret_proof` on outbound calls, and `X-Hub-Signature-256` verification
  on inbound webhooks in the dashboard. Includes `auto_reply` + `reply_rules`
  keyword matching, an `xpst auth messenger` wizard, and
  `messenger_send` / `messenger_set_rules` MCP tools.
- **TikTok posting** via the official Content Posting API v2 (previously
  source-only).
- **Per-video analytics**: views, likes, comments, and shares broken out by
  platform, viewable per post in the desktop app's detail panel.
- **Cross-post analytics**: one video posted to several platforms is correlated
  into a single entry with combined metrics.
- **Follower tracking** and **best-time-to-post** analysis across all platforms.
- **28 MCP tools** for AI-agent control, including cross-post analytics,
  follower stats, best-time suggestions, security audit, caption suggestions,
  transcript retrieval, and content search.
- **Caption suggestions** with platform-specific character limits, via CLI and
  MCP.
- **Security audit** command (CLI and MCP) covering credential storage, file
  permissions, dashboard binding, and provider configuration.
- **Dark mode** in the desktop app, with the preference persisted across
  sessions.
- **Personal knowledge base** (optional extra): ingest local files or URLs,
  transcribe, extract cited knowledge with source provenance, and search
  locally — works with zero configuration via a deterministic extractor.

### Changed
- **Instagram** now uses the official Graph API as its primary authentication
  mode, falling back to session-based access when Graph API is unavailable.
- **Encoding** for TikTok and Threads uses a Reels-grade profile
  (1080×1920, CRF 20, 10 Mbps).
- **Desktop app** has consistent hover states across all ten pages and refreshed
  navigation (Library, Accounts, Automations).
- **Web dashboard** binds to `127.0.0.1` by default; remote access is opt-in via
  an explicit `--host` flag with a warning.
- **Credentials** are stored with Fernet encryption (scrypt key derivation) and
  `0600` permissions, with no plaintext fallback.
- **Scheduled tasks** on macOS use the modern `launchctl` bootstrap/bootout API,
  eliminating password prompts.
- The MCP server is production-hardened with audit logging, a retry policy, a
  tool registry, and a CI security gate.

### Fixed
- State store race condition that could corrupt persisted post history under
  concurrent writes.
- Analytics key mismatch that prevented per-platform metrics from being
  associated with their posts.
- Detail panel field-name mismatch that hid per-platform analytics in the
  desktop app.
- Backfill operating on stale state, which could skip or re-attempt posts
  incorrectly.

### Security
- Independent security review: 0 critical, 0 high findings, no known CVEs in
  dependencies, and no personal or customer data in the repository.
- 1555 tests passing (2 skipped).
