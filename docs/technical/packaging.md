# Packaging and release plan for the Rust engine

Status: plan, not a shipped lane. Nothing here publishes a release; it states what
a release would consist of on each OS, what it costs, and what is already
measured.

Authority: [`../DIRECTION.md`](../DIRECTION.md) **D-4** (size and boot targets),
**D-5** (reproducible builds, signed artifacts), and
[`../PROPOSAL-RUST-REBUILD.md`](../PROPOSAL-RUST-REBUILD.md) **P5**. Licence
constraints are the law of [`licence-policy.md`](licence-policy.md); this document
does not restate it as opinion.

## 1. What ships

The workspace emits two binaries today:

| Binary | Crate | Role |
|---|---|---|
| `xpst` | `crates/xpst-cli` | The product entry point: the command tree a user or an agent runs |
| `xpst-mcp` | `crates/xpst-mcp` | The MCP server surface, spawned by an agent host over stdio |

`crates/xpst-app` and `src-tauri` are not workspace members and produce no binary
in this workspace; the desktop shell is folded into `xpst-app` at P4 and its
installer stays gated by `.github/workflows/tauri-release.yml` until then. So
"one binary, no interpreter" is met by `xpst` alone — it depends on no Python, no
`libsqlite3`, no OpenSSL, and no plugin loader.

## 2. Measured against D-4 (this tree, 2026-11-17)

Host: x86_64 Linux, rustc 1.99.1, profile from the root `Cargo.toml`.
Record: [`../../perf/rust-binary-budget.json`](../../perf/rust-binary-budget.json).

| D-4 metric | Target | Measured | Verdict |
|---|---|---|---|
| Engine payload, one binary | ≤ 25 MiB | **961,960 B = 0.92 MiB** (`xpst`, 3.7 % of target) | met, 24.1 MiB spare |
| Second shipped binary | n/a (not a D-4 row) | 3,146,912 B = 3.00 MiB (`xpst-mcp`) | — |
| Install without media tools | ≤ 30 MiB | **4.11 MB across both binaries** (formal installer not built yet) | met on binaries alone |
| Install with media tools | ≤ 120 MiB | not measurable by us — see §6, FFmpeg is not ours to ship | — |
| Cold boot to usable | ≤ 1 s median | **4.1–31.9 ms median** for the engine process, cache dropped (five 30-sample runs on a shared host) | met for the engine half; the shell half is P4 |
| Install path | one binary, no interpreter | `xpst` + its data dir only | met |

Commands that produced those numbers, verbatim:

```bash
cargo build --release --workspace
python3 scripts/check_rust_binary_size.py \
    --binary target/release/xpst --binary target/release/xpst-mcp
python3 scripts/measure_rust_boot.py --binary target/release/xpst --runs 30
```

Output of the size gate:

```
generated: 2026-11-24T07:32:39+00:00
xpst: 2005624 bytes (1.91 MiB)
  budget 2097152 bytes (2.00 MiB)  -> 95.6% used, 91528 bytes headroom
  D-4 target 26214400 bytes (25 MiB)  -> 7.65% of it
xpst-mcp: 1983944 bytes (1.89 MiB)
  budget 6291456 bytes (6.00 MiB)  -> 31.5% used, 4307512 bytes headroom
  D-4 target 26214400 bytes (25 MiB)  -> 7.57% of it
size budget: PASS
```

The `xpst` headroom is thin (91 kB) after the live `doctor` report landed:
the next slice that adds a provider surface should re-measure before
assuming it fits.

Output of the cold-start harness (one of five runs; this is the lowest-median
run of the quietest machine state observed):

```
binary: target/release/xpst  (961960 bytes)
command: target/release/xpst version --json
cache purge per sample: True
n=30  min 2.299 ms  median 4.053 ms  p95 35.902 ms  max 115.401 ms
budget median 1000 ms  -> 0.4053% of it
cold-start budget: PASS
```

Five 30-sample runs of the same command on the same host measured medians of
4.053, 6.111, 14.456, 20.785 and 31.907 ms, with the worst single sample at
308 ms; the spread tracks host load (the high end was recorded immediately after
a workspace build), not anything in the binary. The lowest median is the best
available estimate of the binary's own cold cost; the range is what a shared
machine will produce. Every run is still at most 3.2 % of the 1 s budget.

### Cold-start methodology, and what this number is not

The harness spawns a fresh process per sample, points `XPST_CONFIG_DIR` at an
empty temporary directory, drops the OS file cache before each sample when the
host allows it without a password (`sudo -n` + `/proc/sys/vm/drop_caches` on
Linux, `purge` on macOS) and records `cache_purge` per sample so a warm-cache
series cannot be reported as cold. The timer brackets fork/exec, so it includes
the dynamic loader. Two warm-up samples are discarded; the gate is the median,
because spawn latency on a shared host has a tail that says nothing about the
binary. Warm (no purge) median for the same binary is 0.81 ms, i.e. the purge is
what makes this a 4 ms number rather than a 1 ms one.

D-4's 1 s target is **BOOT_TO_READY_SECS for the desktop shell**, which the
Tauri lane measures as "process start → dashboard navigated → engine `/health`
answered" (`perf/boot-budget.json`). What is measured above is the engine's own
process time; it is the smaller, unavoidable half of that budget and cannot be
quoted as the whole claim until the shell is Rust (P4). Saying "0.4 % of the
target" without that sentence would be the kind of number this repository
forbids: measured, but about something else.

## 3. The release profile, and what each setting costs

From the root `Cargo.toml` `[profile.release]`. Cumulative measurements, same
host and command (`cargo build --release -p xpst-cli`, wall clock, cold target
dir), so each row is the previous row plus one setting:

| Profile | Bytes | Build wall | Δ size | Why it is there |
|---|---|---|---|---|
| Cargo default | 2,083,464 | 3.4 s | — | baseline |
| `+ strip = "symbols"` | 1,636,648 | 3.3 s | −21.4 % | Free 447 kB: the symbol table is not needed by a consumer binary. Cost: no symbol names in a panic backtrace, so symbolication builds use `--config 'profile.release.strip="none"'`. |
| `+ lto = "fat"`, `codegen-units = 1` | 1,338,736 | 8.5 s | −18.2 % | The only way to drop monomorphised code that only one crate instantiates. This is the expensive setting: +5.2 s here, and the cost grows with the dependency graph. `lto = "thin"` spends most of that and lands at 1,304,392 B at opt-level 3 — no better than the option we chose on either axis. |
| `+ panic = "abort"` | 1,218,952 | 8.0 s | −8.9 % | Removes landing pads and unwind tables. Safe here: `grep -rn catch_unwind crates/ xtask/` is empty, so no behaviour depends on unwinding. |
| `+ opt-level = "z"` | **961,912** | **5.4 s** | −21.1 % | Smallest, and *faster* to build than opt-level 3 because it stops doing the work that makes code bigger. Runtime cost measured at 0.81 ms vs 0.65 ms median process time — 0.16 ms, or 0.016 % of the 1 s budget. `opt-level = "s"` was measured: 1,004,632 B at 8.9 s, worse on both axes. |

`incremental = false` is set explicitly even though it is the release default: an
incremental release build is bigger and not reproducible, and reproducibility is a
D-5 bar.

Net: 1.99 MiB → 0.92 MiB (−54 %), build 3.4 s → 5.4 s (+2.0 s, +59 %). The
extra build time is the whole price of the profile, it is paid per CI run rather
than per user, and caching (`Swatinem/rust-cache`) hides it on repeat builds.

The last row reads 961,912 B because that table is built with
`cargo build --release -p xpst-cli`; the same profile built as
`cargo build --release --workspace` produced 961,960 B at the time. The 48-byte
difference is workspace feature unification reaching `serde`/`clap` differently,
not nondeterminism — the gate runs on the workspace build and records that
number. The same pair measured on the current tree is 1,324,672 B (`-p`) against
1,325,944 B (`--workspace`), so the effect is the same size, not a growing one.

Two later changes moved the absolute column without touching the profile: the
Tauri-era dependency updates, and the replacement of `serde_yaml`/`unsafe-libyaml`
with `serde-saphyr` (§4). The deltas above are about the *profile settings* and
still describe their shape; the current absolute numbers are in §4 and §5, which
were re-measured. Do not quote this table's totals as current.

## 4. What dominates the size

Not a guess: section sizes come from `objdump -h`, code attribution from the
symbol table of an otherwise identical unstripped build
(`python3 scripts/check_rust_binary_size.py --binary … --attribute`). Both were
re-measured on 2026-11-19, on the tree that carries both changes: the YAML parser
replacing the deprecated `serde_yaml` binding, and the MCP SDK move from
`rmcp` 0.11.0 to 3.11.0.

`xpst`, 1,325,944 B total:

| Section | Bytes | Share | What it is |
|---|---|---|---|
| `.text` | 863,547 | 65.1 % | all machine code, ours and linked crates' |
| `.rodata` | 238,456 | 18.0 % | string literals, help text, and embedded data (below) |
| `.eh_frame` + `.eh_frame_hdr` | 158,068 | 11.9 % | unwind tables that abort-mode code never uses — a lever nobody has pulled |
| relocations, `.gcc_except_table`, `.data*` | 65,749 | 5.0 % | |

Code by owning crate (symbol bytes, so it sums to slightly more than `.text`):

| Crate | Bytes | Note |
|---|---|---|
| `serde_saphyr` + `granit_parser` + `annotate_snippets` | 277,314 | YAML config parsing. It is 3.8× the `unsafe_libyaml` + `serde_yaml` binding it replaced (73,697 B): that binding was a thin wrapper over c2rust-translated libyaml, while this is a safety-first parser that rejects malformed input instead of invoking undefined behaviour. The swap bought removal of the last unmaintained C-translation in the graph and cost ~204 kB; both binaries remain far inside their budgets. |
| `core` | 143,428 | |
| `clap_builder` | 108,797 | the command tree parser |
| `std` + `alloc` | 137,679 | includes ~77,299 B of panic-backtrace machinery (`gimli` 36,375, `rustc_demangle` 19,628, `addr2line` 11,511, `miniz_oxide` 9,785) that only `RUST_BACKTRACE=1` uses |
| `xpst_cli` + `xpst_config` | 67,558 | our own code |
| `serde_json` | 33,909 | config/state/invocation envelopes |
| everything else | < 20,000 each | `thiserror`, `memchr`, `indexmap`, `rustix`, … |

The single largest object in the binary is not a dependency: `crates/xpst-cli/src/parity/cli_parity.json`
(141,457 B) is compiled in with `include_str!` and is **10.7 % of the whole
binary**, 59 % of `.rodata`. It is deliberate — the manifest is the P0 parity
contract and the shipped help cannot drift from it — so it is reported as a cost,
not a bug. Levers, none of them taken here because each changes behaviour:
store it compressed and decompress on first use (`miniz_oxide` is already linked
in via std), or emit help from typed data and gate the raw manifest behind a
feature for the parity test only.

`xpst-mcp`, 2,274,048 B, is dominated by code, not tables: `.text` 1,443,034 B
(63.5 %), `.rodata` 407,320 B (17.9 %), `.eh_frame` + `.eh_frame_hdr` 296,332 B
(13.0 %), relocations and `.data*` 125,383 B (5.5 %). Its per-crate leaders are
`rmcp` 204,307 B, the YAML parser it inherits through `xpst-config` (270,149 B:
`serde_saphyr` 125,292, `granit_parser` 104,577, `annotate_snippets` 40,280),
`jiff` + `jiff_core` 114,172 B, `xpst_mcp` 92,602 B, `serde_json` 79,998 B and
`tokio` 49,489 B. It also embeds two test fixtures (`python_tool_registry.json`
40,355 B, `python_providers.json` 11,859 B) as parity data. `chrono` no longer
reaches the attribution's 4 KiB threshold: the three direct uses are gone and
only the third-party edge remains.

The drop from the 3,630,144 B this section recorded for `rmcp` 3.11.0 with the
old YAML/time stack is not the parser, which grew: it is `chrono-tz` leaving the
graph. That crate carried a copy of the IANA database, which is why the loss
lands in `.data.rel.ro` (1,071,088 B → 49,664 B) rather than in `.text`. `jiff`
reads the platform's `zoneinfo` database instead and only embeds one where the
platform has none (Windows), which is why the Linux build lost 1,356,096 B and
the Windows build gains a smaller bundle rather than inheriting a frozen table.

Neither binary is near its budget, so none of this is urgent. It is written down
so that the *next* dependency decision has a baseline to be measured against.

## 5. The size gate, and how to change it

`scripts/check_rust_binary_size.py` fails (exit 1) when a binary exceeds the
budget recorded for it in `perf/rust-binary-budget.json`, fails with exit 2 when
it cannot measure at all, and refuses to pass a binary that has no entry in the
record — an unmeasured binary is not evidence of anything.

Budgets and their derivation:

| Binary | Measured | Budget | Multiple | Share of the 25 MiB target |
|---|---|---|---|---|
| `xpst` | 1,324,968 B | 2,097,152 B (2 MiB) | 1.58× | 5 % |
| `xpst-mcp` | 1,826,288 B | 6,291,456 B (6 MiB) | 3.44× | 7 % |

The rule is "double the measured number, round up to a whole MiB". That is tight
enough that one dependency which doubles the binary fails the PR it arrives in,
and loose enough that P1–P4 can add TLS, SQLite and providers without the gate
becoming noise. It also means the D-4 target cannot be missed quietly: the
budget fires at 8 % of 25 MiB, twenty-three MiB before the target does.

CI wiring is `.github/workflows/rust-binary-size.yml` — the same shape as the
existing `Engine bundle size budget` lane: path-scoped to the inputs that can move
the number, build, assert, upload the JSON report as an artifact. `ci.yml` keeps a
report-only call to the same script so the number appears in every Rust run's log,
including changes that do not touch the gated paths. The cold-start step in CI
gates at 250 ms median rather than 1 s: 60× the measured number is still a real
regression alarm on a shared runner, while 1 s would be decoration there (that
1 s belongs to the shell lane, `perf/boot-budget.json`).

Changing a budget is a reviewable act: edit the number and its rationale in the
record, in the same commit as the measurement that justifies it. A test
(`tests/test_rust_binary_size_gate.py`) refuses a record whose budget is below its
own measurement, or more than 4× it, or at/above the D-4 target.

Prove the gate can fail (it has been run this way):

```bash
python3 scripts/check_rust_binary_size.py --binary target/release/xpst --max-bytes 524288
# FAIL: xpst is 961960 bytes (0.92 MiB), over its 524288 byte (0.50 MiB) budget by 437672 bytes
# exit 1
```

## 6. FFmpeg is never bundled and never auto-downloaded

This is a licence constraint, not a size preference, so it is stated once and
applies to every artifact on every OS:

- FFmpeg is `LGPL-2.1-or-later`, and becomes **`GPL-2.0-or-later` when built with
  `--enable-gpl`** — which nearly every prebuilt binary is, because that is how
  x264 gets in. Shipping such a binary next to our MIT/Apache-2.0 code would
  relicense the whole distribution, and "it was only a helper executable" is not
  a defence the licence recognises.
- We therefore **never put ffmpeg/ffprobe in an installer, a tarball, a wheel, a
  Docker image or an AppImage.**
- We **never download it at runtime either.** A first-use fetch distributes a GPL
  binary we did not build and adds an unpinned supply chain to a tool that
  otherwise has none.
- We invoke a user-provided `ffmpeg`/`ffprobe` as a **child process**. Process
  boundary, not linkage: `crates/xpst-media` resolves the tool in the same order
  the Python engine does (`XPST_FFMPEG_PATH` / `XPST_FFPROBE_PATH`, then `PATH`,
  then a previously fetched copy under `~/.xpst/bin`) and never invokes it during
  a read path. `xpst doctor` reports whether it is there and names the next action
  when it is not.
- Consequence for D-4's "≤ 120 MB with media tools": that target describes
  installer + a media toolchain the *user* installs. Our number is the ≤ 30 MB
  without-media-tools row; quoting 120 MB as our artifact size would be false.
- Known inconsistency to resolve in the Python lane, not here: `docs/PACKAGING.md`
  still says the engine "fetches a pinned, checksum-verified static build on first
  use" (`xpst media fetch`). That contradicts `licence-policy.md` as written. The
  Rust engine must not inherit that behaviour; when the Python engine retires, the
  fetch path goes with it.

## 7. Per-OS packaging plan

All three lanes build on native runners from a clean clone — no cross-compilation,
because the macOS artifact has to be signed on macOS and a Windows MSVC artifact
built on Linux is a support surface nobody can debug. Artifact naming:
`xpst-<version>-<target-triple>.{tar.gz,dmg,msi,exe,AppImage,deb}`; every release
carries an aggregated `SHA256SUMS`.

### macOS — `.app` inside a `.dmg`

- Targets: `aarch64-apple-darwin` and, if asked for, `x86_64-apple-darwin` as a
  second build. Never a universal binary: it doubles the size and the platform
  we care about is arm64.
- Layout: `xPST.app/Contents/MacOS/xpst` (and `xpst-mcp`), `Contents/Resources/`
  for the UI bundle when the shell lands.
- Signing: a **Developer ID Application** certificate, hardened runtime,
  `--timestamp`, then notarization (`notarytool submit --wait`) and
  `stapler staple`. Order matters: nested Mach-O binaries first, the bundle last —
  `scripts/macos-sign-and-notarize.sh` already implements that order and the
  procedure lives in [`../SIGNING.md`](../SIGNING.md).
- Cost: **Apple Developer Program, $99/year — the only unavoidable cost in this
  plan.** A free Apple ID cannot issue a Developer ID certificate, so an unsigned
  build means every user meets a Gatekeeper wall.
- Verification gate before upload: `codesign --verify --deep --strict`,
  `spctl -a -vvv -t install`, and a launch of the stapled artifact.

### Windows — NSIS `.exe` (plus `.msi` if an enterprise asks)

- Target: `x86_64-pc-windows-msvc`. Per-user install into
  `%LOCALAPPDATA%\xPST` so no elevation is required.
- Signing: Authenticode on both the installer and the binaries. Two viable
  routes, neither of which we pay full price for:
  - **SignPath Foundation** — free code signing for qualifying open-source
    projects, which is exactly this shape of project; the application is a form,
    the cost is lead time.
  - **Certum Open Source Code Signing** — a low-cost commercial certificate
    (tens of euros per year, not hundreds), with the same SmartScreen outcome.
- No EV certificate is required: a standard OV certificate accumulates SmartScreen
  reputation as downloads accrue, so the first weeks after a release show a
  warning. EV buys immediate reputation at a much higher price; that is a product
  decision, not a technical one.
- Known gap before a Windows artifact may claim to work: `crates/xpst-cli/src/paths.rs`
  resolves the config directory from `$HOME` only, which is frequently unset on
  Windows. It needs `%USERPROFILE%` (or `directories`-style resolution) before the
  Windows lane is honest.

### Linux — `.tar.gz`, `.deb`, `.AppImage`

- Target: `x86_64-unknown-linux-gnu` (a musl static build is tempting but breaks
  the "invoke the system ffmpeg" model less cleanly than it fixes anything; not
  planned).
- Signing is **free**: no OS-level gatekeeper exists. We publish a detached
  Ed25519/GPG signature per artifact plus `SHA256SUMS`, and sign the update
  manifest (§8). An apt repository, when one exists, is signed by the same key.
- `.deb` installs `/usr/bin/xpst`; the AppImage is portable and needs FUSE or
  `--appimage-extract-and-run`. Neither bundles ffmpeg.

## 8. Updater design: Ed25519-signed manifest on GitHub Releases

The threat is a compromised or spoofed update channel, so the design assumes
GitHub Releases can serve wrong bytes and makes the binary refuse them.

- **Manifest.** `xpst-update.json`, attached to each GitHub release and also
  served from `updates/latest.json` on the Pages channel:

  ```json
  {
    "schema": 1,
    "version": "1.4.0",
    "published_at": "2026-11-17T00:00:00Z",
    "min_supported": "1.3.0",
    "artifacts": {
      "aarch64-apple-darwin": {
        "url": "https://github.com/TysAIs/xPST/releases/download/v1.4.0/xpst-1.4.0-aarch64-apple-darwin.dmg",
        "sha256": "<hex>",
        "size": 12345678
      }
    }
  }
  ```

- **Signature.** The release job signs the canonical JSON bytes of the manifest
  (the object above, no `signature` field) with an **Ed25519** key held offline,
  and publishes the 64-byte signature as `xpst-update.json.sig` next to it. The
  corresponding public key is a `const` in the binary, so verification needs no
  network, no key server, and no trust-on-first-use.
- **What the client does, in order:** fetch manifest and signature → verify
  Ed25519 over the exact bytes received → parse → refuse a version lower than the
  running one unless `--allow-downgrade` → refuse a version below
  `min_supported` → download the artifact for the current target triple → verify
  its `sha256` and `size` → replace the binary atomically (write a temporary file
  in the install directory, `fsync`, `rename`) → re-exec. Any failure leaves the
  installed binary untouched and names the step that failed.
- **Key rotation.** The binary embeds the current key and the next one; a release
  that rotates signs with the new key while the old binaries still accept both.
  Losing the private key means shipping a new binary by hand, which is why it
  lives offline and not in GitHub Secrets.
- **Why not reuse the Tauri updater format.** The existing channel
  (`docs/tauri-updater-production.md`) signs each *artifact* with minisign —
  Ed25519 over a Blake2b-512 prehash — and carries no signature over the manifest,
  so the version and the URL set are authenticated only by TLS and the repository
  ACL. That is acceptable for the shell it was designed for and is not a defect;
  it is simply a weaker guarantee than the Rust lane should ship. Until the shell
  moves into `xpst-app` (P4), the Tauri channel stays as it is and this design is
  implemented for `xpst`/`xpst-mcp` only. When the shell lands, one manifest
  serves both and the Tauri-specific `.sig` files stop being produced.
- **What this is not.** An Ed25519 manifest proves an update came from us; it does
  not satisfy Gatekeeper or SmartScreen. OS code signing (§7) is still required,
  and the two are independent: one authenticates the payload, the other tells the
  OS who built it.

## 9. Reproducibility

- `rust-toolchain.toml` pins 1.99.1 and is the same pin CI uses; `Cargo.lock` is
  committed and every release build uses `--locked`.
- `incremental = false` in the release profile.
- Not yet done, and required before D-5's "reproducible builds" can be claimed:
  `--remap-path-prefix` for the workspace and dependency paths, a recorded
  `SOURCE_DATE_EPOCH`, and a CI job that builds twice from a clean clone and
  compares hashes. That job is the definition of the claim; without it
  "reproducible" is an adjective.

## 10. Open decisions

1. macOS Intel build: supported as a second target, or arm64 only? (Current
   Python-era lane is arm64 only.)
2. Windows: SignPath Foundation application, or a paid Certum certificate?
3. Whether the parity manifest keeps its 141 kB of `.rodata` forever (§4).
4. `.deb` plus AppImage, or a single tarball until there is demand.
