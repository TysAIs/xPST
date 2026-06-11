ultracode — Ship xPST v0.1.0-rc by Sat 2026-06-14 (honest slip to Sun/Mon allowed; never a false tag).

DONE CONDITION: ISA.md (repo root) — all 195 binary ISCs either [x] with the named probe's quoted output in ## Verification, or [DEFERRED-VERIFY] with a roadmap entry + my sign-off. P0 (blocks the RC tag) = every ISC tied to a CRIT/HIGH row in the docs/XPST-NORTH-STAR.md §5 gap register (G01-G55) plus the Day-0 CI gate. The ISA is the done condition — not your judgment, not an agent's.

BINDING PLAYBOOK: docs/SHIP-WEEK-GOAL-PROMPT.md governs in full (execution order, working agreements, scope control) — read it first and obey it even where this summary is silent. docs/XPST-NORTH-STAR.md §6 is the build order.

ORDER:
0. DAY-0 GATE — CI green before any code fix. I fix GitHub Actions billing (prompt me NOW); you delete test.yml, add the feat/knowledge-base trigger, and prove one real completed run via gh run list. No lane starts until CI executes.
1. F2 video fidelity (G12-G16): orientation-aware scaling, IG 1080p profile, drop forced -r 30, yt-dlp bv*+ba merge. Proof = before/after ffprobe on real 9:16/16:9/1:1/60fps assets. Budget the re-test fan-out: every upload path re-verifies after scaling lands.
2. F3 analytics (G18, G22): fix the nonexistent instagrapi calls, de-mock the tests that hid them, add per-(platform,post,timestamp) persistence with a KB-join-ready schema (nugget source post resolves to metric snapshots by platform+post_id) co-designed with the Nugget model NOW.
3. F4 engine correctness (G01-G04): close every double-post path via a persistence-layer idempotency guard with durable-row evidence; races and retries tested.
4. Parallel lanes A-E per spec §6: analytics+agent surface; KB (read-only doctor, SEMANTIC kb_query with provenance, LanceDB default, atomic writes); docs front door (README rewrite, capability matrix, ToS disclosure, kill the TikTok-destination claim — 3+ sites); UI (glyph/geometry fixes + micro-motion + splash); integrations/safety (sys.frozen guard, constrained updates + rollback, pins, state backup/export, failures list/retry, MCP read-only/require-confirm flag — G52 is NOT satisfiable by docs alone; config_show hash leak = rotate credential AND masking regression test). ISC-7 effectiveness = G32 LanceDB search replacing the O(n) scan + one hot-path profiling pass.
5. RC: tag v0.1.0-rc on real CI, build artifacts, hand me macOS + Windows smoke checklists.

AGREEMENTS (full list in the playbook file): implementers return plain text (schemas only on light scouts) and work on the CURRENT base; completion judged by the primary against ISC probes, never by the implementer; pytest/ruff/mypy/import-linter/pip-audit green at every commit; LIVE-PROBE mandate for analytics (real client objects; one real fetch where a session exists); adversarial review per lane before commit; descoped features stay descoped AND inert (no TikTok destination, no story-reposts, no weighted retrieval, no multi-account — document G54; remove half-wired scaffolding); no new dependency without a weight/benefit note in ISA Decisions; conventional commits, push feat/knowledge-base, keep PR #4 current.

OWNER-GATED (prompt me, don't wait): Actions billing; mac/win smoke checks Fri-Sat (give me per-OS checklists); signing decision; single-vs-multi-account statement.

CADENCE: end-of-day burn-down (passed/deferred/remaining). If Friday shows P0 incomplete, the RC slips and you tell me plainly — d5c9224 must not repeat. Work until the ISA is the only thing that says we're done.