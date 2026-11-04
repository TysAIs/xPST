# Project Direction

**Status**: accepted — decisions recorded 2026-11-03.
**Scope**: binding on every lane that touches this repository. Later work does not
relitigate these decisions; it implements them or opens an explicit amendment.

This document is the source of truth for *what kind of project this is*. Concrete
plans that follow from it:

- [PROPOSAL-RUST-REBUILD.md](PROPOSAL-RUST-REBUILD.md) — how the engine becomes Rust.
- [PROPOSAL-SWE-BOT-PROFILE.md](PROPOSAL-SWE-BOT-PROFILE.md) — how the agent that
  works on this repo stops being an "xpst bot" and becomes a general engineering bot.

---

## D-1 — Separation of roles: this lane owns the code, the socials lane is the user

The engineering lane owns source, tests, CI, releases and documentation. The
product-user side (what gets posted, captions, taste, timing) is a separate
operator lane. Consequences:

- Product decisions arrive as requirements; the engineering lane does not invent
  posting strategy.
- Credentials and accounts are *operator* data. They live in user configuration
  (`~/.xpst/`), never in the repository, never in a test fixture, never in a
  default value in source.
- A defect found while posting is an engineering defect only if the engine's
  state or output was wrong; otherwise it is an operator-lane question.

## D-2 — Zero personal data anywhere in the codebase

Not "mostly clean", not "only in tests": zero. The rule covers tracked files,
tests, fixtures, documentation, config templates, commit messages, and built
artifacts (sdist, wheel, desktop bundle).

- No personal e-mail addresses, home paths, machine names, account handles,
  channel IDs, user IDs, phone numbers, or API keys of a real person.
- Fixtures use fictional identities only (`alice`, `bob`, `testuser`,
  `example.com`, synthetic `UC…` channel IDs).
- Product-level identifiers that happen to contain a person's initials (the
  bundle id) are allowed but must be *justified in place* with a comment, so a
  future reader can tell a product id from a leak. The same exemptions are
  recorded as `allow:` lines in `.public-safety-denylist`.
- Nothing is ever reported with the matched value in it: scan output carries
  paths, categories and offsets only.

**Enforcement**: `scripts/scan_public_safety.py` plus
`.github/workflows/public-safety.yml` (tree + artifact scan on every push and
PR, history scan on demand). The personal-identifier deny list is checked in at
`.public-safety-denylist` and the scanner loads it in every mode, so the gate
enforces it in CI with no environment plumbing; `XPST_PUBLIC_SAFETY_DENYLIST`
is unioned in for one-off local scans. The same file carries the
product-identifier exemptions as `allow:` lines (project URLs, the bundle id),
so the permitted identifiers sit next to the banned names and are reviewed
together. Matching is case-insensitive on word boundaries, a documented
placeholder (`example`, `demo`, ...) always wins over a deny-list token, and a
finding never echoes the matched value.

Measured 2026-11-03 with this gate: the tree scan is clean over 732 publishable
files with the deny list active, and adding a banned handle to a `src/` file
fails the same scan. `tests/test_public_safety_scan.py` covers the checked-in
file, the env-var union, comment/blank-line parsing, the placeholder allow-list
and the deny list's exemption from its own scan.

History mode stays report-only: it names the categories and commits it finds,
and a full-history rewrite remains a human decision.

## D-3 — All-Rust rebuild

The engine is rewritten in Rust. The desktop shell is already Rust (Tauri 2);
the Python engine behind it (146 files, 69,302 lines under `src/`) is not the
future.

- One Rust workspace, one `xpst` binary, subcommands instead of a Python package.
- The Tauri shell and the web UI stay; the UI is not rewritten.
- The only non-Rust runtime components permitted are external media tools
  invoked as child processes — `ffmpeg` and `yt-dlp` — and each must be
  *optional, pinned, documented and replaceable*. They are dependencies of the
  feature, not of the language.
- Python is deleted, not kept as a compatibility layer, once parity is proven.
  Until then it is the reference implementation used to generate parity fixtures.

## D-4 — Lightweight and easy

"Lightweight" is measured, not asserted. Targets for the Rust release:

| Metric | Today (Python) | Target |
|---|---|---|
| Engine payload | ~95 MB sidecar (PyInstaller) | ≤ 25 MB single binary, no interpreter |
| Desktop install | ~192 MB (ffmpeg 87 + engine 95 + yt-dlp 3 + ui 1) | ≤ 120 MB with media tools, ≤ 30 MB without |
| Cold boot to usable | 2.4 s median (Mac mini M4 baseline, `perf/boot-budget.json`) | ≤ 1.0 s median on the same host class |
| Install path | Python ≥ 3.10, venv or pipx, 20 runtime dependencies plus 8 extras | one binary, or one installer, no interpreter, no venv |
| Runtime dependencies | 20 runtime dependencies plus 8 optional extras | Rust crates compiled in; no runtime package resolution |

"Easy" means a stranger's first successful post takes one install and one
connect step, and every failure they see names the next action.

## D-5 — Built for open-source release

MIT / Apache-2.0 dual license stays; no telemetry; local-first. Release quality
bar: reproducible builds from a clean clone, signed artifacts, honest capability
documentation, and contributor-facing docs (architecture, ADRs, contribution
path) that do not assume private context.

## D-6 — Publishing chores are agent work, end to end

PyPI, release tags, workflow runs, secrets, and artifact verification are the
engineering lane's job. The maintainer is never handed a manual chore.

- If a step is automatable, automate it and prove it with the run it produced.
- If a step genuinely needs the human (account ownership, 2FA, a legal
  acceptance), state that *one* action explicitly — never a checklist.
- Publishing state is verified by reading the index back (`pypi.org/pypi/<name>/json`,
  release assets, tag → artifact digest), not by trusting a green workflow.

**Current state (verified 2026-11-04)**: `.github/workflows/publish-pypi.yml` on
`main` publishes with the existing `PYPI_API_TOKEN` repository secret (created
2026-09-25), gated to a `v*.*.*` tag push or a published GitHub release;
`workflow_dispatch` is build/validate only, and the publish job needs neither
`environment:` nor `id-token: write`. The OIDC trusted-publishing form is the
documented alternative — it needs a one-time pending-publisher registration
(owner `TysAIs`, repo `xPST`, workflow `publish-pypi.yml`, environment `pypi`).
Tag `v1.2.1` predates the switch and still carries the OIDC form, which is why
run 41796222963 failed `invalid-publisher`; no tagged run has exercised the token
path yet, and `pypi.org/pypi/xpst/json` still returns 404 (the name `xpst` is
unclaimed), so `pip install xpst` stays unavailable until a version tag is pushed
from a commit carrying the token path. That tag push is the one maintainer-gated
step in this chain; everything else is agent work (D-6).

## D-7 — In force until the maintainer sets a new bar

Two safety regimes are active and are not relaxed by this direction:

1. **Posting pause.** The schedule is empty (`xpst schedule list` → `entries: []`)
   and 10 entries sit parked in `~/.xpst/schedule.json.paused-*` snapshots. No
   lane resumes posting on its own initiative.
2. **Rate-limit safety stack.** The merged stack (installed `5bdc39c`, merged
   `b2c1fcd`) stays: cooldown state machine, one verdict per limit, senders that
   *refuse* rather than truncate, retry ownership contract, honest readiness.
   `xpst health` reports `rate_limit_cooldowns.source: ~/.xpst/rate_cooldowns.json`.
   No rewrite, port, or "cleanup" may drop these behaviors; the Rust rebuild
   carries them as required parity behaviors with tests.

## D-8 — Explicitly not decided here

Open questions that this document does *not* settle, listed so nobody treats a
default as a decision:

- The agent profile rename to a general engineering bot (proposal only; no
  rename has happened).
- Whether `yt-dlp` remains an external dependency long-term, and whether TikTok
  source extraction is ever reimplemented in-process.
- Whether the PyPI distribution keeps the name `xpst` through the rebuild, or
  the Rust binary ships only through GitHub Releases plus a crates.io package.
- ffmpeg distribution strategy (bundled vs system vs on-demand fetch).
- Whether the Python engine is kept installable for one release cycle after the
  Rust release ("escape hatch") or removed in the same release.

## Amending this document

A change to D-1…D-7 needs an explicit note in the pull request that changes
behavior, and an edit here in the same change. Implementation details that do
not contradict these decisions do not need an amendment.
