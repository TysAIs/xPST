# CLI parity gates — why the 27 remaining stubs are not mechanical ports

Snapshot taken 2026-11-25T10:55Z at `83727ec` (`feat/rust-cli-tranche-10-2026-11-25`, PR #46).
This is a hand-written map, not a generated file: the generated source of truth for *which*
commands are implemented is `docs/technical/rust-parity-ledger.md` (from
`crates/xpst-cli/src/implemented.rs`). This document answers the next question — *why* each
remaining stub is not a copy of the local-surface pattern — with a file and line in `src/xpst/`
for every claim.

## The count, re-derived

```
$ git show 83727ec:docs/technical/rust-parity-ledger.md | grep -o '\*\*[0-9]* of 88'
**61 of 88
```

Cross-checked against the tree at the same commit, not against the prose:

| Source | Count |
|---|---|
| `crates/xpst-cli/src/parity/cli_parity.json` entries | 103 (1 root + 102 named) |
| … of which are group containers (a name that is a strict prefix of another) | 14 |
| … leaving leaf commands | **88** |
| `crates/xpst-cli/src/implemented.rs` `implemented(...)` entries | **61** |
| `docs/technical/rust-parity-ledger.md` rows marked `implemented` | 61 |
| `docs/technical/rust-parity-ledger.md` rows marked `stub` | **27** |

The three sets agree exactly: ledger rows = the 88 leaves, ledger `implemented` = the 61
`implemented.rs` entries, and the 27 stubs = 88 − 61. The 14 containers are `analytics`, `byo`,
`chat`, `config`, `failures`, `generate`, `kb`, `mcp`, `media`, `messenger`, `plugins`,
`schedule`, `state`, `stream`.

## Gates at a glance

`Gate` is why the command cannot be ported as a local slice today. `Ledger` is the
`Python network` column as it stands; `Code` is what the shipped Python actually does, with the
deciding call named. Where the two disagree the reason is in
[Where the network column disagrees with the code](#where-the-network-column-disagrees-with-the-code).

| Command | Gate | Ledger | Code | Deciding call |
|---|---|---|---|---|
| `auth` | A | yes | yes (local half: `api-token`) | `cli.py:3612` → `auth_status.py:642` → `auth_status.py:141` |
| `backfill` | A | yes | yes | `cli.py:1954` → `engine.py:1325` → `engine.py:1384` |
| `chat timeline` | A | yes | yes | `cli.py:8576` → `chat/aggregate.py:444` → `chat/twitch.py:146` |
| `connect` | A | yes | yes (local half: `--guide`, `--dry-run`) | `cli.py:2641`; `cli.py:2601`; `connect.py:349` |
| `delete` | A | yes | yes | `cli.py:4617` → `engine.py:1393` → `engine.py:1476` |
| `failures retry` | A | yes | yes | `cli.py:5786` → `services/recovery_service.py:265` |
| `followers` | A | yes | **possible** | `cli.py:7637` → `platforms/youtube.py:680` |
| `health` | A | yes | yes | `cli.py:2343`; `cli.py:2355` |
| `media fetch` | A | yes | **possible** | `cli.py:8057` → `media/binaries.py:515` → `media/binaries.py:244` |
| `messenger check-comments` | A | yes | yes | `cli.py:5147` → `platforms/messenger.py:528` → `:426` |
| `onboard` | A | yes | yes | `cli.py:2916` |
| `post` | A | yes | yes | `engine.py:736` |
| `refresh-tokens` | A | yes | yes (local half: nothing due) | `cli.py:4286` → `token_refresh.py:234` |
| `run` | A | yes | yes | `cli.py:658`; `cli.py:681`; `engine.py:588` |
| `schedule run` | A | yes | yes | `cli.py:6772` → `engine.py:736` |
| `serve` | A | yes | yes | `cli.py:4674` → `serve.py:174` → `scheduler.py:202` |
| `setup` | A | yes | **possible** | `cli.py:480` → `setup.py:403`; `setup.py:100` |
| `stream fetch-key` | A | yes | yes | `cli.py:8179` → `stream/ingest.py:926` → `:502` |
| `stream live` | A | yes | yes | `cli.py:8330` → `stream/ingest.py:1064` |
| `update` | A | yes | yes (local half: `--components`) | `cli.py:506` → `updater.py:212` |
| `watch` | A | yes | yes | `cli.py:830` → `scheduler.py:202` |
| `dashboard` | B | none | **possible** | `cli.py:4711` → `dashboard/server.py:933`; warm at `server.py:274` |
| `ui` | B | none | **possible** | `cli.py:4831` → `dashboard/server.py:999`; same warm |
| `kb add` | C | possible | possible | `knowledge/cli_kb.py:74` |
| `kb organize` | C | possible | possible | `knowledge/cli_kb.py:242` |
| `kb reembed` | C | possible | possible | `knowledge/cli_kb.py:163` |
| `suggest-caption` | D | possible | possible | `cli.py:7782`; `cli.py:7789` |

## Gate A — network (21 commands)

All 21 are blocked on the same three questions card `t_18fc9a6b` asks for the MCP surface: which
HTTP client, how a provider is fixtured, and which one goes first. The CLI does not need a second
answer — `xpst auth status` and the MCP `xpst_auth_status` tool are the same probe machinery
(`cli.py:2343` and `mcp/server.py:2236` both call `auth_status.collect_live_auth_status*`), so a
decision made for one surface settles the other.

What each command actually reaches:

- **`auth`** — `auth status` renders `collect_live_auth_status` (`auth_status.py:642`), whose
  Graph probe is `httpx.AsyncClient` at `auth_status.py:141`; `auth <platform>` runs the OAuth
  flows in `cli.py:3642-3661` (`_auth_youtube` … `_auth_messenger`). The one local branch is
  `auth api-token` (`cli.py:3615` → `cli.py:3802`), which only reads or rotates the dashboard
  token in the credential store.
- **`backfill`** — `engine.backfill` (`engine.py:1325`) re-posts each missing destination through
  `post_manual` (`engine.py:1384`). `--dry-run` (`cli.py:1934`) reads `state.json` only.
- **`chat timeline`** — `chat_timeline` (`chat/aggregate.py:444`) fans out to the platform
  readers; Twitch is a raw IRC socket (`chat/twitch.py:146` `_default_connect`), Kick and YouTube
  are HTTP readers in the same package.
- **`connect`** — `run_connect` (`cli.py:2641`) and `test_connections` (`cli.py:2601`); the
  Instagram Graph verification is `httpx.get` at `connect.py:349`. `--guide` (`cli.py:2541`) and
  `--dry-run` (`cli.py:2545`) render `wizard.PLATFORM_GUIDES` with no network at all.
- **`delete`** — `engine.delete_post` (`engine.py:1393`) calls the uploader's `delete`
  (`engine.py:1476`). `--dry-run` (`cli.py:4574`) is local.
- **`failures retry`** — `recovery_service.execute_failure_retry` (`cli.py:5786`) re-posts via
  `engine.post_manual` (`services/recovery_service.py:265`). The refusal path (unknown id) is
  local: `plan_failure_retry` runs before the engine exists (`cli.py:5775`).
- **`followers`** — `uploader.get_followers()` per platform (`cli.py:7637`), e.g.
  `platforms/youtube.py:680` (`channels().list`). Best-effort: see the disagreement section.
- **`health`** — `collect_live_auth_status` (`cli.py:2343`) plus `check_network`
  (`cli.py:2355`). `engine.check_health(include_platforms=False)` is the local half.
- **`media fetch`** — `ensure_media_binaries(fetch=True)` (`cli.py:8057`), which downloads a
  pinned static build through `urllib.request.urlopen` (`media/binaries.py:244`).
- **`messenger check-comments`** — `MessengerAdapter.auto_reply_to_comments`
  (`platforms/messenger.py:528`), Graph API calls at `:426`.
- **`onboard`** — `test_connections` is called unconditionally at `cli.py:2916`, before the
  `--dry-run` branch, so even the plan-only run probes every platform.
- **`post`** — the upload path is `upload_service.upload_to_platform` (`engine.py:736`); the
  `--dry-run` branch returns at `cli.py:1225`.
- **`refresh-tokens`** — `refresh_due_tokens` (`cli.py:4286`) → `uploader._refresh_access_token`
  (`token_refresh.py:234`). With nothing due it returns `{}` with no network
  (`token_refresh.py:326-327`).
- **`run`** — `CrossPostEngine` (`cli.py:658`), `check_network` (`cli.py:681`), then
  `source_service.fetch_new_videos` (`engine.py:588`) and the upload path.
- **`schedule run`** — claims due entries, then `CrossPostEngine` (`cli.py:6772`) and the same
  upload path. `nothing_due` and `--dry-run` (`cli.py:6739-6759`) are local.
- **`serve`** — `run_serve` (`cli.py:4674`); the loop is `scheduler._run_check` (`serve.py:174`)
  → `engine.check_and_post` (`scheduler.py:202`). It also starts the dashboard
  (`serve.py:209`), so it is gate A **and** gate B.
- **`setup`** — the transaction half (`cli.py:447-469`) is local; the interactive half calls
  `run_setup` (`cli.py:480` → `setup.py:403`), which pip-installs yt-dlp when it is missing
  (`setup.py:100`) and runs `test_connectivity` (`setup.py:346`).
- **`stream fetch-key`** — `fetch_and_store_key` (`stream/ingest.py:926`) over the shared
  `httpx.Client` (`stream/ingest.py:502`).
- **`stream live`** — `live_status` (`stream/ingest.py:1064`), Twitch Helix and Kick public API.
- **`update`** — `get_latest_version` (`cli.py:506`) reads the PyPI JSON API through
  `urllib.request.urlopen` (`updater.py:212`). `--components` without `--check` passes
  `include_network=False` (`cli.py:553`) and is local.
- **`watch`** — `Scheduler` (`cli.py:830`) → `engine.check_and_post` (`scheduler.py:202`), in a
  loop with no local mode.

## Gate B — the FastAPI dashboard port (2 commands)

`dashboard` (`cli.py:4711` → `dashboard/server.py:933`) and `ui` (`cli.py:4831` →
`dashboard/server.py:999`) both start the same application. That is the `xpst-api` project, not a
tranche item, and `ui --json`'s readiness line (`url`, `pid`, `ui_built`, `cli.py:4814`) is
only meaningful once the server exists.

Route count, measured rather than quoted: `_create_app` mounts a 47-route `APIRouter`
(`dashboard/api.py:312`) plus 10 routes declared in `dashboard/server.py` — **57 application
routes**, or 61 `app.routes` entries including FastAPI's four built-in docs routes. (The card
that commissioned this map said 59; the measured number is 57.)

## Gate C — the vector store (3 commands)

`kb add` (`knowledge/cli_kb.py:74`), `kb organize` (`:242`) and `kb reembed` (`:163`) all need the
embedding stack (`fastembed` + `sqlite-vec`/LanceDB), which is not ported. `kb add` additionally
fetches a URL source through yt-dlp, and `kb organize` takes an optional LLM client. The ledger
already records `kb query` and `kb migrate-store` as ported *without* the embedding path — the
same answer Python gives on an unembedded store — so these three are the slice that needs the
store, not a second copy of the reader.

## Gate D — deferred with a named blocker (1 command)

`suggest-caption` (`cli.py:7760`) seeds its transcript from `FasterWhisperTranscriber`
(`cli.py:7782`), which `.[full]` installs. A fixture built from it is machine-dependent and a
live harness would download a model, so the card is `t_727a10c0`. The generator itself
(`caption_gen.generate_caption`, `cli.py:7789`) is already ported for the MCP surface.

## Where the network column disagrees with the code

The `Python network` column is a reviewed judgement, not a runtime capture — `disconnect` is the
standing example: the ledger still classifies it `yes` (`rust-parity-ledger.md:41`) although the
ported command is fully local, which is how PR #46 got its cheapest command. The same reading
applied to the 27 finds two classes.

### The column is wrong: the command does its job offline

- **`followers` — `yes` should be `possible`.** The whole live fetch is wrapped in
  `try/except Exception: pass` (`cli.py:7632-7644`) and every uploader's `get_followers` swallows
  its own errors and returns `0` (`platforms/youtube.py:680-693`). The payload is built from
  `store.latest_followers()` (`cli.py:7628`), so a run with no network still reports counts and
  growth from the local ledger. The live number is an upgrade, not a precondition.
- **`media fetch` — `yes` should be `possible`.** `ensure_media_binaries` skips every binary that
  already resolves (`media/binaries.py:533-536`, `if info["ok"]: continue`), so on a host with
  ffmpeg/ffprobe the command downloads nothing and still does its job.
- **`setup` — `yes` should be `possible`.** The transaction half — `--status`, `--resume`,
  `--reset`, `--finish-later` and the whole `--json` path — is local (`cli.py:447-469`). The
  interactive half's connectivity test is documented as making no API calls
  (`setup.py:346-350`); the only network is `install_yt_dlp` when yt-dlp is absent
  (`setup.py:100`) and whatever an OAuth prompt does.
- **`dashboard` / `ui` — `none` should be `possible`.** `_create_app` warms the live-auth cache at
  startup (`dashboard/server.py:274` → `dashboard/api.py:193` → `dashboard/api.py:117`
  `collect_live_auth_status`), which probes platforms whenever credentials exist. It is a
  background side effect, disabled by `XPST_DISABLE_AUTH_WARM`, and it does not change the gate —
  but the process is not network-free.

### The column is right, and there is still a local half worth taking first

These do their job over the network, but a flag or subcommand does real work offline. Porting the
local half is a legitimate cheap slice as long as the ledger row keeps saying `yes`:

- **`auth`** — `auth api-token` (`cli.py:3615` → `cli.py:3802`) reads/rotates the dashboard token
  from the credential store.
- **`connect`** — `--guide` (`cli.py:2541`) and `--dry-run` (`cli.py:2545`) render
  `PLATFORM_GUIDES`; `--test` (`cli.py:2601`) is the network half.
- **`update`** — `--components` without `--check` (`cli.py:553`, `include_network=False`).
- **`refresh-tokens`** — nothing due returns `{}` with no network (`token_refresh.py:326-327`).
- **`post`, `run`, `backfill`, `delete`, `schedule run`** — each has a `--dry-run` that reads
  local state and returns before any upload (`cli.py:1225`, `cli.py:1934`, `cli.py:4574`,
  `cli.py:6739-6759`; `run` passes `dry_run` into `_run_check_and_post`).

## What the next tranche card should take

Ordered by cost, from the evidence above — not a commitment, a recommendation:

1. **`setup`'s transaction half** — the largest genuinely local surface left (start/status/resume/
   reset/finish-later over `setup_transaction`), and the MCP tools for it are already ported.
2. **`auth api-token`** — one credential-store read plus a rotate branch.
3. **`connect --guide` / `--dry-run`** — a captured guide table and a plan document.
4. **`update --components`** — the local component inventory (the `updates` section of
   `diagnostics` already ports the same rows).
5. **`followers`** — the stored-snapshot report, with the live fetch left to the network tranche
   and the ledger row corrected to `possible` in the same change.
6. **`media fetch`** — the resolve-and-report half; the download stays behind the network decision.

Everything else in gate A waits on `t_18fc9a6b`. Gate B waits on `xpst-api`. Gate C waits on the
vector store. Gate D waits on `t_727a10c0`.

## What this document does not claim

- **No harness run.** This is a docs-only change: no Rust code, no fixture, no `implemented.rs`
  entry. `cargo xtask ledger` is unchanged by construction (the ledger is generated from
  `implemented.rs` and `ledger.rs::DEVIATIONS`, neither of which this touches) and is run in
  check mode as the gate. No `capture_local_surface.py` run is claimed, because no command
  changed behaviour.
- **No network tranche work.** Nothing here contacts a platform, and posting stays paused.
- **The gates are about portability, not priority.** A gate-A command with a local half is still
  gate A; the recommendation section is where the cheap slices are named.
