# Changelog

All notable changes to xPST will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.0] - 2026-11-14

### Added
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
