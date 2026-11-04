# Rust engine workspace (P0 scaffold)

This workspace is phase **P0** of [the Rust rebuild proposal](../docs/PROPOSAL-RUST-REBUILD.md):
the crate layout, the full CLI command tree, read-only config/state paths, and the CI
gates. It is deliberately cheap and reversible — it proves the toolchain before any
behaviour moves.

**Nothing here is the shipped surface yet.** The Python engine in `../src/` still runs
every post, and the published `xpst` on this machine is still the Python install. This
workspace builds a second binary, `target/release/xpst`, which answers read-only
questions and refuses everything else with
`not implemented in the Rust engine yet`.

## What actually works

| Command | Behaviour |
|---|---|
| `xpst config show` | Reads `~/.xpst/config.yaml` and prints it, secrets masked. Byte-identical to the Python engine's output, including `json.dumps` separators and the masking rule. `--raw` is the only way a secret reaches stdout. |
| `xpst config validate` | Read-only validation. Reports `{"valid", "checks"}`; exits `2` when a blocker is found. Old-but-supported schemas are reported as *migration pending*, not as errors — P0 has no write path to migrate with. |
| `xpst doctor` | Local-only diagnostics: config, `state.json`, schedule file, media tool discovery. `engine.live_checks` is `false` and says so; platform probes and auth verdicts land in P1. |
| `xpst version`, `xpst --version` | What this binary is. `--version` matches Click's `xPST, version <v>` shape. |

Everything else — all 82 other command paths — parses its arguments exactly as the
Python CLI does and then exits `1` with the message above. Argument errors (missing
required option, unknown flag) are exit `2`, matching Click.

Read-only is a hard property, not an aspiration: these paths open files for reading
only, never write, never upload, and never open a socket.

## Non-goals in P0

* No writes to `state.json`, `config.yaml` or `schedule.json`.
* No posting, no scheduling, no network of any kind.
* No migrations applied — only detected.
* `src-tauri/` is untouched and is **not** a workspace member; `xpst-app` is an empty
  placeholder until P4+ folds the shell in.

## Crates

`crates/` mirrors the crate map in the proposal. Most crates are empty-but-compiling
placeholders: no behaviour is declared in a crate until the phase that owns it lands a
test for it. `xtask` is the only place a Python interpreter is invoked, and only for
developer tooling — the shipped binary has no Python runtime dependency.

```
crates/xpst-cli      command tree, exit-code contract, P0 read-only commands
crates/xpst-config   config read path: YAML, masking, validate-only
crates/xpst-state    state.json v2 + schedule read path
crates/xpst-media    ffmpeg/ffprobe discovery (no execution)
xtask                parity capture/diff and release-binary size report
```

## Working on it

```bash
cargo test --workspace                 # unit + parity + read-path + contract tests
cargo clippy --workspace --all-targets -- -D warnings
cargo fmt --all -- --check
cargo audit
cargo xtask size                       # build release and report the binary size
```

The CI job `rust` in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs the
same commands. It sits alongside the Python matrix, which is unchanged.

## Command-tree parity

`crates/xpst-cli/src/parity/cli_parity.json` is generated from the Python engine's Click
tree and is the contract for the CLI surface. It records every command path, every flag
and alias, the help text and the provenance (repo, commit, source file).

* `cargo test -p xpst-cli --test parity` fails if the Rust tree is missing a command
  path or a flag, if a parent's `--help` no longer lists a child, or if a command was
  invented that the Python CLI does not have.
* `cargo xtask parity` re-captures the manifest and diffs it against the checked-in
  copy, so a new command in `src/xpst/cli.py` cannot go unnoticed. `--write` updates it;
  review the diff like any other contract change.
* CI runs the same comparison (`xtask/scripts/capture_python_cli.py` + `diff`).

To capture, the script needs the Python engine importable (`pip install -e .` in the
repo root, or `PYTHONPATH=src` inside a venv that has its dependencies).

`crates/xpst-cli/tests/fixtures/config-sample.*` are generated goldens: the masked and
raw JSON are produced by the Python engine itself
(`xtask/scripts/generate_config_goldens.py`), not hand-written, so the Rust read path is
diffed against the real implementation rather than against a guess.

## Posting is paused

`docs/DIRECTION.md` D-7 keeps the posting pause and the rate-limit safety stack in force
during the rebuild. P0 inherits that for free: it cannot post. A missing
`~/.xpst/schedule.json` is reported as "no scheduled posts (paused or unused)", not as a
fault.
