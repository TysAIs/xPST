CRITICAL PROCESS RULE — READ BEFORE TOUCHING ~/xPST (posted 2026-08-27 after a shared-checkout race destroyed work twice in one day):

The ~/xPST checkout is SHARED. Multiple bots and subagents have worked in it concurrently, switched branches under each other, and built stale code into dist/. Result: Tyler lost a shipped UI fix TWICE and saw an old splash icon. NEVER do any of the following directly in ~/xPST again:

1. Do NOT `git checkout` to another branch in ~/xPST.
2. Do NOT `git reset/restore/clean` in ~/xPST.
3. Do NOT run ./build.sh from ~/xPST.

INSTEAD:
- Make a throwaway clone for your task: `git clone ~/xPST /tmp/xpst-work-<task> && cd /tmp/xpst-work-<task> && git checkout -b <your-branch>`
- Commit + push your branch from there; open a PR via `gh pr create`.
- Builds that must produce dist/xPST.app happen ONLY by coordinator decision, from a pinned commit in an isolated worktree (`git worktree add /tmp/xpst-release <sha>`), so dist/ always matches a known-good tree.
- Your disposable clones live under /tmp and can be deleted when merged.
