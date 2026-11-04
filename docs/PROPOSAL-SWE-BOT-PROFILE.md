# Proposal: turn this agent profile into a general software-engineering bot

**Status**: proposal only — no rename has happened.
**Date**: 2026-11-03
**Trigger**: the maintainer wants to build more than one project, and this
profile is currently named after one of them.

---

## 1. Where we are

The profile that does this work is named `xpst`. That name now describes a
*dependency of one project*, not the job. What the profile actually is, measured
today:

| Fact | Value |
|---|---|
| Profile id | `xpst` (model `DeepSeek-V4.1-Flash-1017`, provider `sparks`) |
| Skills on disk | 13 active skills across 7 categories (autonomous-ai-agents 2, email 1, productivity 1, research 1, social-media 3, software-development 4, web 1); `apple`, `creative`, `media` and `note-taking` exist only as archived copies under `.archive/` |
| Project-specific skills | 3 dedicated (`xpst-cross-posting`, `xpst-agent-concurrency`, `xpst-release-ops`) plus the general ones used daily (`dogfood`, `github`, `systematic-debugging`, `inspecting-hermes-desktop-dom`) |
| MCP servers registered | 1 (`xpst`, 41 tools, `~/.venvs/xpst/bin/xpst mcp start` via the `xpst` wrapper on `PATH`) |
| Cron jobs | none |
| Memory | ~89% full, and dominated by xpst facts (install lineage, credential status, PyPI state) |
| Peer profiles | `default`, `craigland`, `homelab`, `invincible`, `model-maintenance`, `prusa`, `schooling`, `socials` |

The capability is already general — the skills inventory is mostly generic
(software-development, research, email, web, productivity). Only the *identity*
is project-shaped, and the identity is what leaks: the profile name, the SOUL
framing ("the xpst lane"), a memory budget spent on one repo's install lineage,
and an always-on MCP server for one app.

Meanwhile `socials` exists as the product-user lane. That separation is right
(D-1 in [DIRECTION.md](DIRECTION.md)) and should stay: engineering bot writes
code, socials lane uses the app.

## 2. Target picture

One engineering profile that can hold several projects, with project knowledge
in *scoped* places instead of the identity layer:

| Layer | Contents | Lifetime |
|---|---|---|
| Profile identity (name, SOUL, description) | "general software-engineering bot": build, test, review, ship | stable |
| Lane skills, project-namespaced | `xpst-*` (cross-posting, concurrency, release-ops), future `<project>-*` | per project |
| Project context | repo docs (`AGENTS.md`, `docs/DIRECTION.md`), kanban project records, per-project briefs | per project |
| Memory | only what is true in *every* session (host facts, standing conventions, who the maintainer is) | stable, small |
| MCP servers | registered per project, enabled only when that project is being worked on | per project |

Concretely: the xpst knowledge that currently eats memory moves into
`skills/social-media/xpst-cross-posting/references/` (install lineage, deploy
procedure, credential quirks). Memory keeps host facts and standing rules.
`AGENTS.md` already carries repo-local rules — that stays the project's home.

## 3. Migration plan (agent-executed, verifiable, reversible)

Every step is a command the agent runs and proves, not a checklist for the
maintainer.

**P0 — inventory and freeze** (read-only)
- Snapshot: profile config, SOUL, skills tree with symlink targets, cron
  (`cron.db`, verified empty), memory file, kanban board rows whose `assignee`
  is `xpst`, gateway state, sessions/routing identity rows.
- Write the snapshot to `~/.hermes/profiles/xpst/backups/pre-rename-<ts>/`
  (config, SOUL.md, memory, `profile.yaml`).
- Block the board briefly: no dispatched card on `xpst` during the rename.

**P1 — choose the identity**
- Name candidates need to be short, non-project-specific and unambiguous:
  `dev`, `swe`, `builder`. Default recommendation: `dev`, with a wrapper alias
  so muscle memory and any script that calls the profile still works.
- New description for `hermes profile describe`: "General software-engineering
  bot — implements, tests, reviews and ships changes across the maintainer's
  projects; project knowledge lives in per-project lane skills and repo docs."
  (The kanban orchestrator reads this to route work, so it must not name one
  project.)

**P2 — rename**
- `hermes profile rename xpst dev` (this is a real command; it moves the profile
  and migrates session/routing identity — `hermes profile migrate-identity`
  retries the identity part if it does not settle).
- Verify: `hermes profile list` shows the new id with the same model/provider;
  `~/.hermes/profiles/<new>/config.yaml` and `SOUL.md` are present; a new
  one-shot session answers under the new profile.

**P3 — identity rewrite**
- SOUL.md: replace the xpst-specific framing with the engineering-bot identity;
  keep the host/computer-control block untouched (it is generated).
- Add the standing rule that the socials lane is the user (D-1) and that
  publishing chores are agent work (D-6) — both are profile-level truths, not
  repo-local ones.
- Memory: move xpst install/deploy facts into the `xpst-cross-posting` skill's
  references; keep host facts, lane-skill locations, GitHub identity, and the
  PyPI state (which is project-ish — move it too, or keep one line).

**P4 — project wiring**
- Keep the `xpst` MCP server registered but mark it project-scoped: enabled
  while working on xPST, disabled otherwise (a per-session toggle), so a session
  about another project does not spawn a 41-tool server it never uses.
- Rename lane skills to the `<project>-<lane>` convention where they are not
  already (`xpst-cross-posting` etc. already comply).
- Kanban: update open cards' `assignee` to the new profile; confirm the
  dispatcher spawns the renamed profile for a trivial smoke card.

**P5 — verify and report**
- Smoke card completed by the renamed profile; `hermes mcp test xpst` still
  connects; cron list empty; memory under budget with xpst facts removed;
  `xpst` wrapper alias resolves to the new profile.
- Report the diff of what moved where (config, memory, skills) — with the backup
  path — in the completion note.

**Rollback**: `hermes profile rename <new> xpst`, restore the P0 backup files,
update kanban assignees back. The rename is reversible until the old id is
reused by something else, so no other profile may claim `xpst` in the interim.

## 4. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Kanban cards with `assignee: xpst` are silently dropped by the dispatcher | cards sit in `ready` forever | update assignees in the same change, then prove with a smoke card |
| Session/routing identity migration fails partway | lost session history lookups | `hermes profile migrate-identity` retry, backup taken first, verification step is mandatory |
| Memory loses the xpst install lineage | a future session deploys from the wrong commit | move it into the lane skill *before* deleting it from memory |
| Skills directories symlinked across profiles | rename breaks the links | re-point symlinks after rename and verify each skill loads (`skill_view` on one per category) |
| Docs/scripts that say "the xpst lane" | confusion after rename | grep the repo + profile for `xpst` identity references; repo's own name is exempt |
| Two engineering profiles if a new one is created instead of renamed | split memory, duplicated skills, two dispatchers | rename, do not fork |

**Why not create a second profile instead**: a new `dev` profile would duplicate
the 13 skills, split memory, and leave two engineering dispatchers competing for
the same board. Rename keeps one identity with project-scoped knowledge.

## 5. Decisions requested

1. **Name**: `dev` (recommended), `swe`, `builder`, or another.
2. **Timing**: now (cheap, nothing else in flight), or after Rust-rebuild phase
   P1 when the lane's habits are settled. Recommendation: now — the rename gets
   more expensive every week the memory fills with one project.
3. **xpst MCP server**: keep registered project-scoped (recommended) or always
   on.
4. **Memory split**: confirm that install/deploy lineage belongs in the lane
   skill rather than profile memory (recommended), understanding that a fresh
   session will read it from the skill instead of memory.

Nothing here blocks current work: the profile keeps doing xpst engineering until
the rename is approved.
