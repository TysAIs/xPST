# xPST engineering cadence (how this gets built)

This is the operating contract for the Rust rebuild. It exists so the work is
repeatable by any agent, on any day, without re-deciding how we work.

## Roles

| Role | Owns |
|---|---|
| Product owner | What the product is, priority, and anything that spends money or touches a live account |
| Engineering lead (the coordinating agent) | Architecture, slice definition, merge decisions, red-main response, final verification |
| Implementer agents | One slice each, in an isolated clone; TDD; commit locally; never merge |
| Verifier agents | Independent re-run of the gates and adversarial review of a slice they did not write |
| Research agents | Facts with sources; no code |

An implementer's self-report is never evidence. The lead re-runs the gates.

## Phase model

`docs/PROPOSAL-RUST-REBUILD.md` defines P0–P6. We do not skip phases. Within a
phase, work is cut into **vertical slices**: each slice is one behaviour that a
user (or an agent) can exercise end to end, not a layer of plumbing.

Slice definition rules:
- One slice = one paragraph of user-visible behaviour + the tests that prove it.
- If a slice cannot be verified without a live account, it ships behind a
  dry-run or fixture path first, and the live proof is a separate, labelled step.
- No slice may leave main red. If it would, it gets split.

## The loop for every slice

1. **Brief.** Lead writes the slice brief: behaviour, non-goals, the fixture or
   parity source of truth, and the exact gate commands.
2. **Implement.** Agent clones `origin/main` into its own directory, branches,
   writes the failing test first, then the code. Venv lives outside the tree.
3. **Self-gate.** `cargo fmt --check`, `cargo clippy -D warnings`, `cargo test`,
   parity if the CLI surface moved, public-safety scan if fixtures or docs moved.
4. **Report.** Commit locally. Report: files, gate output verbatim, what is
   unverified and why.
5. **Verify.** Lead re-runs the gates in the agent's tree. Anything less is trust.
6. **PR + merge.** Lead pushes, opens the PR, and merges with `--admin` only
   when either the matrix is green or every gate has a local equivalent that
   passed and that is stated in the PR body.
7. **Re-sync.** After each merge, every other open PR that touches generated
   files (CHANGELOG, parity manifest, counts) is re-synced before its turn.
8. **Full suite.** After a merge batch, the whole suite runs on merged main.
   Red main is the highest priority in the fleet: fix before new work.

## Evidence rules

- "Tests pass" requires the command and its output in this session.
- A parity test must be proven able to fail: tamper the fixture, watch it go red,
  restore it. An unbreakable guard is decoration.
- Anything touching a real platform account needs live evidence pasted into the
  PR, with test posts marked and deleted in the same session.
- Numbers in docs are measured, never estimated. Rate limits carry a doc URL and
  the date read.

## Cost guardrails

- The app ships with **no shared platform credentials**. Users bring their own
  developer app (BYO), which is what keeps an open-source project free to run.
  Shipping one shared app makes the maintainer liable for per-call API fees and
  for hosting other people's tokens.
- No hosted backend in the default path. Anything that would require one is
  opt-in and stated as such in the installer.
- Media processing is local (ffmpeg/yt-dlp as external children only).

## Batch cadence

- At most three PRs in flight per lane; more than that and the generated-file
  conflicts cost more than the parallelism saves.
- Each batch: implement in parallel → verify serially → merge one at a time →
  re-sync the rest → full suite.
- Research runs ahead of implementation, never beside it for the same slice.

## Definition of done for a slice

- Behaviour exists in Rust and is exercised by a test that failed first.
- The Python behaviour it replaces is either matched (parity test) or the
  difference is stated and approved.
- Gates green, evidence in the PR, docs updated if a surface changed.
- Nothing in the slice requires a server, a shared credential, or a manual step
  the user cannot see.
