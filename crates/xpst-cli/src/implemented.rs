//! The one list of commands whose behaviour has moved to the Rust engine.
//!
//! `dispatch` consults this table instead of matching on strings, and
//! `docs/technical/rust-parity-ledger.md` is generated from it. That makes the
//! ledger impossible to drift by construction: a command is either in this
//! table (and therefore implemented, with a handler the compiler requires) or
//! it is answered by [`crate::not_implemented`] and the ledger says stub.
//!
//! Adding an entry is the whole act of implementing a command. Adding a *row*
//! to the ledger without an entry does nothing, and `tests/parity_ledger.rs`
//! fails if the doc and this table disagree in either direction.

use std::io::Write;

use clap::ArgMatches;

use crate::{commands, GlobalOptions};

/// A command handler: global options, the leaf's parsed arguments, and the two
/// output streams.
pub type Handler = fn(&GlobalOptions, Option<&ArgMatches>, &mut dyn Write, &mut dyn Write) -> i32;

/// One implemented command.
pub struct ImplementedCommand {
    /// Space-joined command path, as in the parity manifest.
    pub path: &'static str,
    /// The crate that owns the behaviour (the CLI entry is always `xpst-cli`).
    pub owner: &'static str,
    /// What the implementer verified, in one sentence — the ledger's "notes".
    pub note: &'static str,
    pub handler: Handler,
}

/// Every implemented command, in manifest order.
pub const IMPLEMENTED: &[ImplementedCommand] = &[
    implemented(
        "config show",
        "xpst-config",
        "Masked YAML/JSON document; byte-compared with the Python output.",
        config_show,
    ),
    implemented(
        "byo clear",
        "xpst-auth",
        "Deletes the stored app pair and clears the config mirror; payload byte-compared with the Python CLI (the config file is edited in place, not rewritten from the model).",
        byo_clear,
    ),
    implemented(
        "byo set",
        "xpst-auth",
        "Validates the app id/secret with Python's two patterns, stores the pair in the Fernet credential store, mirrors the id into `accounts.<platform>` and reports the masked `byo status` view; payload byte-compared with the Python CLI (the config file is edited in place, not rewritten from the model).",
        byo_set,
    ),
    implemented(
        "byo status",
        "xpst-auth",
        "Reads the Fernet credential store the Python engine writes (scrypt + AES-128-CBC + HMAC-SHA256) and the config/env fallbacks; payload byte-compared with the Python CLI.",
        byo_status,
    ),
    implemented(
        "capabilities",
        "xpst-provider",
        "Serves the capability document captured from `xpst.content.capability_document()`; the payload is diffed against a live `python -m xpst capabilities --json` by the local-surface check.",
        capabilities,
    ),
    implemented(
        "chat platforms",
        "xpst-provider",
        "Readiness rows from the captured chat read-path registry plus the config channels and the YouTube token check; payload byte-compared with the Python CLI (checked_at masked).",
        chat_platforms,
    ),
    implemented(
        "config export",
        "xpst-config",
        "Writes the masked (or `--raw`) document with a PyYAML-identical emitter; payload and file byte-compared with the Python CLI.",
        config_export,
    ),
    implemented(
        "config fix",
        "xpst-config",
        "Detects the same five defects and reports `fixes_applied`/`details`; payload byte-compared with the Python CLI.",
        config_fix,
    ),
    implemented(
        "config import",
        "xpst-config",
        "Merge/replace with the same deep-merge, diff and validation rules; payload byte-compared with the Python CLI.",
        config_import,
    ),
    implemented(
        "config set",
        "xpst-config",
        "Dotted-key set with Python's bool/int/float/str parsing; payload and file byte-compared with the Python CLI.",
        config_set,
    ),
    implemented(
        "config validate",
        "xpst-config",
        "`{valid, checks}` payload with the schema checks; exit 2 on a blocker. Python also checks the download/log directories, credential files and rate limits, so the check list differs by design (see the deviations).",
        config_validate,
    ),
    implemented(
        "media status",
        "xpst-media",
        "Resolution order (env → PATH → system dirs → fetched) and the `env`/`system`/`fetched`/`missing` vocabulary of `media_binary_status()`; payload byte-compared with the Python CLI.",
        media_status,
    ),
    implemented(
        "messenger status",
        "xpst-provider",
        "Config + credential-presence payload (masked token preview, reply-rule count, comment platforms) read from the encrypted store with the config fallback, including the `XPST_MESSENGER_REPLY_RULES`/`XPST_MESSENGER_COMMENT_PLATFORMS` overrides and the raw `page_id`/`webhook_path`/`comment_platforms` values `config set` writes as numbers; payload byte-compared with the Python CLI.",
        messenger_status,
    ),
    implemented(
        "providers",
        "xpst-provider",
        "Captured registry manifests plus the ported canonical role catalog and per-destination `provider_mode`/`visible`, with Python's legacy `.crosspstr` path fix, config-directory rebase and `str()` coercion of numeric `provider_mode`/`auth_mode`; payload byte-compared with the Python CLI on pristine, configured, community-mode, legacy-path, `--config` and `config set`-shaped profiles.",
        providers,
    ),
    implemented(
        "doctor",
        "xpst-cli",
        "Local-only diagnostics; live probes are P1 (the report says so), so the payload differs from Python's live report by design (see the deviations).",
        doctor,
    ),
    implemented(
        "version",
        "xpst-cli",
        "Rust engine identity (`version`, `engine`, `phase`, `commit`); `--version` matches Click's shape. Python's dependency list has no Rust equivalent, so the payload differs by design (see the deviations).",
        version,
    ),
    implemented(
        "status",
        "xpst-state",
        "Statistics payload byte-compared with `get_statistics()` on captured fixtures — Python's `json.dumps` separators and insertion order, including the state file's own platform order.",
        status,
    ),
    implemented(
        "schedule add",
        "xpst-state",
        "The add-time route resolution (`build_route_plan` with readiness off) and the persisted entry: `--text`/FILE/`--caption-for`/caption-cap/date validation in Python's order, the resolved destinations stored verbatim, the UTC `scheduled_time` plus the entry's own zone, and the `json.dump(indent=2, ensure_ascii=False)` store write under the same `.schedule.lock`; payload and `schedule.json` byte-compared with the Python CLI.",
        schedule_add,
    ),
    implemented(
        "schedule list",
        "xpst-state",
        "Entries, pause marker and paused snapshots, byte-compared with `schedule_list --json` (store key order preserved, `scheduled_time_local` appended last).",
        schedule_list,
    ),
    implemented(
        "schedule pause",
        "xpst-state",
        "Writes the same `schedule.paused.json` marker (0600, `json.dumps(indent=2)`) under the same `.schedule.lock`; payload byte-compared with the Python CLI.",
        schedule_pause,
    ),
    implemented(
        "schedule resume",
        "xpst-state",
        "Removes only the pause marker (snapshots survive) and reports `was_paused`; payload byte-compared with the Python CLI.",
        schedule_resume,
    ),
    implemented(
        "failures list",
        "xpst-state",
        "Dead-letter rows with status/resolved/age_hours, byte-compared with `failures_list --json`.",
        failures_list,
    ),
    implemented(
        "generate caption",
        "xpst-core",
        "The deterministic caption generator (`generate_caption_deterministic`): sentence split, keyword hashtags, three variants and Python's per-platform truncation; payload byte-compared with the Python CLI. The LLM path is not ported, so a configured `XPST_KB_LLM_ENABLED` endpoint is a named divergence.",
        generate_caption,
    ),
    implemented(
        "generate ideas",
        "xpst-core",
        "The deterministic idea templates (`generate_ideas_deterministic`) with the shipped rotation, and the payload echoing the raw `--topic`; payload byte-compared with the Python CLI. The LLM path is not ported (named divergence).",
        generate_ideas,
    ),
    implemented(
        "failures clear",
        "xpst-state",
        "Clears the dead-letter queue through the same tolerant loader `status` uses (v1 migration included) and writes Python's `json.dump` separators, keeping `errors: {}` for the per-video form; payload byte-compared with the Python CLI (written keys keep the read order).",
        failures_clear,
    ),
    implemented(
        "state export",
        "xpst-state",
        "Validates `state.json` as JSON and copies it byte-for-byte (mode preserved); payload and exported file byte-compared with the Python CLI.",
        state_export,
    ),
    implemented(
        "state import",
        "xpst-state",
        "Validates `posted_videos`, backs the current file up as `state.json.pre-import-<stamp>` and copies the source in; payload and resulting `state.json` byte-compared with the Python CLI. An explicit `n` at the prompt exits 0 with empty stderr (Click's `confirm()` returns False); only EOF aborts with `Aborted!` and exit 1.",
        state_import,
    ),
    implemented(
        "state backup",
        "xpst-state",
        "Snapshots `state.json` into `backups/state-<stamp>.json` with `--keep` rotation and the same `retained` count; payload byte-compared with the Python CLI. `--keep` is Click's int conversion, so a non-integer is a usage error (exit 2, empty stdout) rather than a silent default.",
        state_backup,
    ),
    implemented(
        "quota",
        "xpst-state",
        "Per-platform usage ledger with config-derived limits, midnight/hourly resets, the YouTube `videos.insert` bucket block and `auth_mode`; payload byte-compared with the Python CLI on default, seeded and config-limited profiles.",
        quota,
    ),
    implemented(
        "logs",
        "xpst-cli",
        "Last 50 lines of `monitoring.log_file` (expanded the way `_expand_paths` expands it) and the `No log file found` payload; byte-compared with the Python CLI.",
        logs,
    ),
    implemented(
        "devices",
        "xpst-auth",
        "Paired-device rows newest-first with the token hash removed, and `--revoke` writing the same `devices.json` (sorted keys, indent 2, 0600); payload byte-compared with the Python CLI.",
        devices,
    ),
    implemented(
        "diagnostics",
        "xpst-cli",
        "The redacted support bundle: `diagnostics.json` + `README.txt` in a ZIP, with the shipped path/secret redaction, the config snapshot, the readiness report, the credential key list, the state summary and the redacted log tail byte-compared against the Python bundle. `system`, `dependencies`, `updates` and `providers` are named divergences (a compiled engine has no Python runtime, no pip packages and no `posting_truth` projection).",
        diagnostics,
    ),
    implemented(
        "pair",
        "xpst-auth",
        "Issues a one-time pairing code (or the `--code` one) with the same alphabet, hashing, TTL floor and `devices.json` shape; payload byte-compared with the Python CLI on a fixed code. `--ttl 0` is falsy in Python and means the 600 s default (not the 30 s floor), and a non-integer `--ttl` is a usage error (exit 2).",
        pair,
    ),
    implemented(
        "stream destinations",
        "xpst-provider",
        "The ingest-path registry plus per-platform key presence and fetch readiness (YouTube token, Twitch/Kick app credentials, Facebook page token) read from the encrypted store; payload byte-compared with the Python CLI.",
        stream_destinations,
    ),
    implemented(
        "stream app-credentials",
        "xpst-provider",
        "Stored app-credential state (masked) and the registration steps for Twitch/Kick, with `--clear`; payload byte-compared with the Python CLI.",
        stream_app_credentials,
    ),
    implemented(
        "stream set-key",
        "xpst-provider",
        "Validates the destination and writes it to the Fernet credential store under `stream_destination_<platform>`; payload byte-compared and the stored entry read back by the Python engine.",
        stream_set_key,
    ),
    implemented(
        "stream clear-key",
        "xpst-provider",
        "Deletes `stream_destination_<platform>` from the credential store and reports `removed`; payload byte-compared with the Python CLI.",
        stream_clear_key,
    ),
    implemented(
        "stream relay-config",
        "xpst-provider",
        "Renders the MediaMTX relay YAML from the stored destinations (keys included on stdout, key-free summary with `--output`); payload and written file byte-compared with the Python CLI. `--rtmp-port` is Click's int conversion, so a non-integer is a usage error (exit 2) instead of a fallback to 1935.",
        stream_relay_config,
    ),
    implemented(
        "bio",
        "xpst-cli",
        "Public and editor URLs with the dashboard API token read from the credential store and masked unless `--reveal`; payload byte-compared with the Python CLI on a seeded token. `--port` is Click's int conversion, so `0900` renders as `900` and a non-integer is a usage error (exit 2).",
        bio,
    ),
    implemented(
        "security-audit",
        "xpst-cli",
        "The six live checks (credential file modes, dashboard bind, MCP readonly, provider mode, ffmpeg, encrypted storage) with `indent=2` ASCII-escaped JSON; payload byte-compared with the Python CLI.",
        security_audit,
    ),
    implemented(
        "mcp list",
        "xpst-mcp",
        "The captured Python tool registry in Python's order, rendered with `json.dumps(indent=2)`; payload byte-compared with the Python CLI.",
        mcp_list,
    ),
    implemented(
        "mcp start",
        "xpst-mcp",
        "Serves the 46-tool surface over stdio in-process (`xpst_mcp::serve_stdio`, the same loop the `xpst-mcp` binary runs); a bare `xpst mcp` starts it too, as the shipped Click group does. 35 of the 46 tools are ported (`xpst_status`, `xpst_config_show`, `xpst_providers`, `xpst_capabilities`, `xpst_schedule_list`, `xpst_schedule_add`, `xpst_schedule_cancel`, `xpst_schedule_pause`, `xpst_schedule_resume`, `xpst_rate_limit_status`, `xpst_readiness`, `xpst_security_audit`, `xpst_best_time`, `xpst_followers`, `xpst_activity`, `kb_query`, `xpst_search`, `kb_areas`, `xpst_transcript`, `xpst_bio_get`, `xpst_byo_app`, `xpst_chat`, `xpst_stream`, `xpst_preflight`, `xpst_cross_post_analytics`, `xpst_analytics`, `xpst_suggest_caption`, `xpst_generate_ideas`, `xpst_setup_start`, `xpst_setup_status`, `xpst_setup_resume`, `xpst_setup_reset`, `xpst_auth_start`, `messenger_set_rules`, `xpst_disconnect`); the rest answer a typed `not_implemented`. Pinned by a protocol capture from the Python server (`tests/mcp_stdio.rs`) instead of a local-surface row, because the process never exits: initialize, tools/list, 140 byte-compared `tools/call` bodies across six scenarios (the schedule writers and the two config writers need `XPST_MCP_ALLOW_MUTATIONS=1`, so they live in their own; `xpst_analytics` needs its own seeded ledger and publish records; the `writers` scenario also byte-compares the `config.yaml` the calls leave behind) and the consent/unknown-tool refusals. The server also ports **both** halves of `_argument_shape_block` (`guard::argument_shape_block`): a media path outside the config dir, the user's media folders, the system temp dir or `XPST_MEDIA_ROOTS` is refused before dispatch, and so is a URL-shaped argument (`url`, `webhook_url`, `callback_url`, `source_url`, `media_url`, `source`) that resolves to loopback, a private, link-local, reserved, multicast or metadata address or embeds credentials — with Python's sentence. The URL half is `xpst-surface::net_guard`, a port of `net_guard.validate_public_url` held to a 235-row capture (`crates/xpst-surface/tests/fixtures/python_net_guard.json`) that also pins the blocklist tables and the exact sequence of hosts the guard resolves.",
        mcp_start,
    ),
    implemented(
        "app",
        "xpst-app",
        "Locates the installed desktop shell in the same preference order and prints the same not-found guidance (exit 1); byte-compared with the Python CLI.",
        app,
    ),
    implemented(
        "best-time",
        "xpst-store",
        "Hour-of-week engagement buckets from `analytics.db` (`BestTimeAnalyzer.analyze`), ranked by Python's rounded average with the first snapshot per post and `collection_status: failed` rows excluded; payload byte-compared with the Python CLI on empty, seeded and platform-filtered ledgers.",
        best_time,
    ),
    implemented(
        "kb areas",
        "xpst-store",
        "Discovered knowledge areas in course order (`order_index`, label, id) with their nugget counts, read from the workspace's `areas.json`; payload byte-compared with the Python CLI. The reader reproduces `JsonKnowledgeStore._load_file`: a decode failure, an unknown key or a missing required key renames the file to `<name>.corrupt` and starts empty, while a non-object document escapes as Python's `AttributeError`. An invalid `--workspace` reproduces Python's uncaught `ValueError` contract — empty stdout, exit 1 — with the exception line on stderr instead of the traceback frames.",
        kb_areas,
    ),
    implemented(
        "schedule remove",
        "xpst-state",
        "Cancels one entry through the same local-store contract (`operation`/`scope`/`found`/`cancelled`/`entry`, `POST_NOT_FOUND` on an unknown id) with `--dry-run`; payload and resulting `schedule.json` byte-compared with the Python CLI. Control characters in a caption are escaped (`\\u0001`) in the written file, so both engines can read it back.",
        schedule_remove,
    ),
    implemented(
        "kb course",
        "xpst-store",
        "The assembled course outline (`assemble_course`): areas in course order, each with its nuggets in beginner->advanced order and a trailing `Unassigned` module for unclaimed nuggets; payload byte-compared with the Python CLI. The store reader is strict like `Nugget.from_dict`/`Area.from_dict`, so an unknown or missing key quarantines the file exactly as Python does.",
        kb_course,
    ),
    implemented(
        "kb doctor",
        "xpst-store",
        "The read-only health report (`doctor.diagnose`): dependency probe, store integrity, embedding widths, orphaned areas/nuggets, queue counts and the manifest source count, with the same severities, messages and exit code; payload byte-compared with the Python CLI. The optional-dependency probe asks an interpreter out-of-process (`XPST_PYTHON`, else `python3`), because a Rust binary has no `sys.path`.",
        kb_doctor,
    ),
    implemented(
        "kb migrate-store",
        "xpst-store",
        "The JSON-store probe and the honest LanceDB refusal (`{\"ok\": false, \"error\": \"lancedb is not installed\"}`, exit 1) plus the `no JSON store found` answer; payload byte-compared with the Python CLI. The vector store is not ported, so a machine with the knowledge extra would migrate in Python (named divergence).",
        kb_migrate_store,
    ),
    implemented(
        "kb query",
        "xpst-store",
        "Substring search over the JSON store (`query_nuggets`): `workspace`/`query`/`mode`/`count`/`nuggets` with citation, timestamps, a null score and the area id; payload byte-compared with the Python CLI. The embedding stack is not ported, so the semantic path is not taken — the same answer Python gives without the knowledge extra or on an unembedded store.",
        kb_query,
    ),
    implemented(
        "search",
        "xpst-store",
        "`kb query` on the `default` workspace rendered with `json.dumps(indent=2)` (ASCII-escaped), including Python's text-mode quirk of reading a `hits` key the payload never carries; payload byte-compared with the Python CLI.",
        search,
    ),
    implemented(
        "readiness",
        "xpst-cli",
        "The first-run report (`build_readiness_report`): the canonical provider status shared with `providers`, the ban-risk registry, the local folder set and the yt-dlp/FFmpeg helper probes, with `--fix` creating the same folders; payload byte-compared with the Python CLI (the `python` row reports the Rust engine, and `--fix` does not rewrite `config.yaml`).",
        readiness,
    ),
    implemented(
        "verify-media",
        "xpst-media",
        "The per-destination ingest verdict (`PostPreflightService.plan` with readiness off): `-p all` resolved through `enabled_destinations`, the `media_spec` check list and the raw ffprobe document in ffprobe's own key order, the `--plan` post plan and transform plans, and the exit-1-on-error contract; payload byte-compared with the Python CLI.",
        verify_media,
    ),
    implemented(
        "preflight",
        "xpst-core",
        "The config-aware route plan (`build_route_plan`): target resolution from the config's enabled flags, the per-destination state/reason/envelope/constraints/blockers/warnings/transform/visibility payload, the ban-risk advisory and the read-only quota gate; payload byte-compared with the Python CLI. `verify_media`'s loudness check is ported but the route plan pins `check_loudness=False`, so `preflight` never spawns the extra ffmpeg pass.",
        preflight,
    ),
    implemented(
        "analytics export",
        "xpst-store",
        "The local export: post-id discovery from `state.json` (canonical `id`, legacy `post_id`, unknown platforms appended in state order), the `--platforms` filter, the totals and per-platform totals, the `collection_status` block, the `json.dump(indent=2, ensure_ascii=False)` document and the CSV BOM + `\\r\\n` header; payload and written file byte-compared with the Python CLI (`exported_at` masked). The per-platform metric collectors are not ported, so the export carries the answer a credential-less profile gets (empty metrics, `collection_status.status: ok`).",
        analytics_export,
    ),
    implemented(
        "plugins list",
        "xpst-cli",
        "The installed-plugin inventory: `~/.xpst/plugins` resolved from HOME (not `XPST_CONFIG_DIR`, as `DEFAULT_PLUGIN_DIR` does), the directory created on a fresh profile, and the `{plugins, count}` payload with `list_plugins()`'s five keys in order; payload byte-compared with the Python CLI. A plugin is Python code, so the inventory is asked of an interpreter (`XPST_PYTHON`, else `python3`, else `python`) running the shipped `PluginManager` — the same out-of-process shape `kb doctor` uses.",
        plugins_list,
    ),
    implemented(
        "plugins docs",
        "xpst-cli",
        "The generated markdown: the header, the `Auto-generated on` stamp, the plugin count, and per plugin its name, version, description, capabilities and `config_options` rows, written to `--output` or carried in the payload; payload and written file byte-compared with the Python CLI (the stamp masked). Same interpreter delegation as `plugins list`.",
        plugins_docs,
    ),
    implemented(
        "schedule install",
        "xpst-state",
        "The Linux scheduler entry: `crontab -l` read, the prior xPST marker/command pair filtered out (including Python's `skip_next` quirk), the `*/N * * * * <bin> serve >> <log> 2>&1` line appended and written back with `crontab -`, plus `--remove`'s `crontab -`/`crontab -r` split and the `No crontab entries found` answer; payload and the resulting crontab byte-compared with the Python CLI against a stand-in `crontab` on PATH. The macOS LaunchAgent and Windows scheduled-task branches are not ported (named divergence).",
        schedule_install,
    ),
    implemented(
        "disconnect",
        "xpst-auth",
        "`disconnect_platform()` (the encrypted store's keys, the library-native plaintext files, `sessions/*<platform>*`, then `enabled: false` and the config save) behind the CLI's own destructive-action guard: a non-interactive or `--json` call without `--yes` answers `CONFIRMATION_REQUIRED` and exits 1 before anything is touched. Payload and the resulting `config.yaml` byte-compared with the Python CLI; the human view is plain text (standing divergence).",
        disconnect,
    ),
    implemented(
        "transcript",
        "xpst-store",
        "The three lookups in `Workspace.get_transcript()` order (`<id>.json`, `content:<id>.json`, then the first `transcripts/` file whose name contains the id) with `create=False`; a hit is `json.dumps(indent=2)` and a miss is the compact `{\"error\", \"video_id\"}` document with exit 1. Payload and exit code byte-compared with the Python CLI.",
        transcript,
    ),
    implemented(
        "wizard",
        "xpst-auth",
        "The deprecated alias of `setup`: the same resumable transaction (`start`/`status`/`resume`/`reset` with `alias: wizard`), the agent-mode envelope (`mode`, `interactive`, `all_pass`, `completed`, `next_action`), the `INTERACTIVE_REQUIRED` document with exit 3, the legacy checklist built from a captured `PLATFORM_GUIDES` table, and `--export-md`'s markdown (byte-compared with the shipped `export_markdown()`). Payload byte-compared with the Python CLI (transaction id and timestamps masked). The interactive executor (`run_wizard`, browser + live checks) is not ported — named divergence.",
        wizard,
    ),
];

const fn implemented(
    path: &'static str,
    owner: &'static str,
    note: &'static str,
    handler: Handler,
) -> ImplementedCommand {
    ImplementedCommand {
        path,
        owner,
        note,
        handler,
    }
}

/// Look a command up by its space-joined path.
pub fn lookup(key: &str) -> Option<&'static ImplementedCommand> {
    IMPLEMENTED.iter().find(|command| command.path == key)
}

// Thin shims: `dispatch` only has `&mut dyn Write` handles, and the older
// handlers are generic over `W: Write`. `&mut dyn Write` implements `Write`, so
// the shim costs one reborrow and no allocation.

fn config_show(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let mut out_ref: &mut dyn Write = out;
    let mut err_ref: &mut dyn Write = err;
    commands::config::show(global, leaf, &mut out_ref, &mut err_ref)
}

fn config_validate(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let mut out_ref: &mut dyn Write = out;
    let mut err_ref: &mut dyn Write = err;
    commands::config::validate(global, leaf, &mut out_ref, &mut err_ref)
}

fn media_status(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::media::status(global, leaf, out, err)
}

fn verify_media(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::media::verify_media(global, leaf, out, err)
}

fn messenger_status(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::messenger::status(global, leaf, out, err)
}

fn providers(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::providers::run(global, leaf, out, err)
}

fn byo_clear(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::byo::clear(global, leaf, out, err)
}

fn byo_status(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::byo::status(global, leaf, out, err)
}

fn capabilities(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::capabilities::run(global, leaf, out, err)
}

fn chat_platforms(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::chat::platforms(global, leaf, out, err)
}

fn config_export(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::config::export(global, leaf, out, err)
}

fn config_fix(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::config::fix(global, leaf, out, err)
}

fn config_import(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::config::import(global, leaf, out, err)
}

fn config_set(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::config::set(global, leaf, out, err)
}

fn doctor(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let mut out_ref: &mut dyn Write = out;
    let mut err_ref: &mut dyn Write = err;
    commands::doctor::run(global, leaf, &mut out_ref, &mut err_ref)
}

fn version(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let mut out_ref: &mut dyn Write = out;
    let mut err_ref: &mut dyn Write = err;
    commands::version::run(global, leaf, &mut out_ref, &mut err_ref)
}

fn status(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::status::run(global, leaf, out, err)
}

fn schedule_add(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::schedule::add(global, leaf, out, err)
}

fn schedule_list(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::schedule::run(global, leaf, out, err)
}

fn schedule_pause(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::schedule::pause(global, leaf, out, err)
}

fn schedule_resume(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::schedule::resume(global, leaf, out, err)
}

fn failures_list(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::failures::run(global, leaf, out, err)
}

fn failures_clear(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::failures::clear(global, leaf, out, err)
}

fn state_export(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::state::export(global, leaf, out, err)
}

fn state_import(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::state::import(global, leaf, out, err)
}

fn state_backup(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::state::backup(global, leaf, out, err)
}

fn quota(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::quota::run(global, leaf, out, err)
}

fn logs(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::logs::run(global, leaf, out, err)
}

fn devices(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::devices::list(global, leaf, out, err)
}

fn diagnostics(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::diagnostics::run(global, leaf, out, err)
}

fn pair(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::devices::pair(global, leaf, out, err)
}

fn stream_destinations(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::stream::destinations(global, leaf, out, err)
}

fn stream_app_credentials(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::stream::app_credentials(global, leaf, out, err)
}

fn stream_set_key(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::stream::set_key(global, leaf, out, err)
}

fn stream_clear_key(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::stream::clear_key(global, leaf, out, err)
}

fn stream_relay_config(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::stream::relay_config(global, leaf, out, err)
}

fn bio(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::bio::run(global, leaf, out, err)
}

fn security_audit(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::security::run(global, leaf, out, err)
}

fn mcp_list(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::mcp::list(global, leaf, out, err)
}

fn mcp_start(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::mcp::start(global, leaf, out, err)
}

fn app(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::app::run(global, leaf, out, err)
}

fn kb_areas(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::kb::areas(global, leaf, out, err)
}

fn schedule_remove(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::schedule::remove(global, leaf, out, err)
}

fn kb_course(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::kb::course(global, leaf, out, err)
}

fn kb_doctor(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::kb::doctor(global, leaf, out, err)
}

fn kb_query(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::kb::query(global, leaf, out, err)
}

fn search(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::kb::search(global, leaf, out, err)
}

fn readiness(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::readiness::run(global, leaf, out, err)
}

fn best_time(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::best_time::run(global, leaf, out, err)
}

fn preflight(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::preflight::run(global, leaf, out, err)
}

fn byo_set(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::byo::set(global, leaf, out, err)
}

fn generate_caption(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::generate::caption(global, leaf, out, err)
}

fn generate_ideas(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::generate::ideas(global, leaf, out, err)
}

fn kb_migrate_store(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::kb::migrate_store(global, leaf, out, err)
}

fn analytics_export(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::analytics::export(global, leaf, out, err)
}

fn plugins_list(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::plugins::list(global, leaf, out, err)
}

fn plugins_docs(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::plugins::docs(global, leaf, out, err)
}

fn schedule_install(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::schedule::install(global, leaf, out, err)
}

fn disconnect(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::disconnect::run(global, leaf, out, err)
}

fn transcript(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::transcript::run(global, leaf, out, err)
}

fn wizard(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::wizard::run(global, leaf, out, err)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_entry_names_a_manifest_command() {
        for command in IMPLEMENTED {
            let path: Vec<String> = command.path.split(' ').map(str::to_string).collect();
            assert!(
                crate::parity::command_at(&path).is_some(),
                "`{}` is implemented but not in the parity manifest",
                command.path
            );
        }
    }

    #[test]
    fn paths_are_unique() {
        let mut seen = std::collections::BTreeSet::new();
        for command in IMPLEMENTED {
            assert!(
                seen.insert(command.path),
                "`{}` is listed twice",
                command.path
            );
        }
    }

    #[test]
    fn every_entry_is_fully_described() {
        for command in IMPLEMENTED {
            assert!(
                !command.owner.trim().is_empty(),
                "{} has no owner",
                command.path
            );
            assert!(
                !command.note.trim().is_empty(),
                "{} has no note",
                command.path
            );
        }
    }
}
