# xPST Ship-Week Goal Prompt

> Owner pastes the block below into a fresh Claude Code session (repo `~/XPST`, branch `feat/knowledge-base`) to start the build. Compiled 2026-06-11 from `ISA.md` (195 ISCs) + `docs/XPST-NORTH-STAR.md` (gap register G01–G55, build order §6).

---

/goal ultracode — Ship xPST v0.1.0-rc by Saturday 2026-06-14.

GOAL: Take xPST from HEAD of feat/knowledge-base to a verified release candidate: every CRIT/HIGH gap in docs/XPST-NORTH-STAR.md §5 fixed, the foundations landed before dependents, all 195 criteria in ISA.md passing with quoted probe evidence or explicitly deferred with my sign-off, and the repo front door rewritten to enterprise-evaluator grade. ISA.md is the done condition — not your judgment, not an agent's.

SYSTEM OF RECORD:
- ISA.md — 195 binary ISCs. Mark [x] only with the named probe's quoted output in ## Verification.
- docs/XPST-NORTH-STAR.md — identity, target journeys, per-subsystem current-vs-ideal (file:line cited), gap register G01–G55, build order §6, honest descopes.

EXECUTION ORDER (spec §6 — foundations gate everything):
0. DAY-0 HARD GATE — CI green before any code fix: I fix GitHub Actions billing first (my action — prompt me immediately); you delete test.yml, add the branch trigger, and prove one real completed run (gh run list). No lane starts until CI executes. A week of fixes without a verification gate repeats the exact sin this audit found.
1. F2 video fidelity: G12 orientation-aware scaling (video.py:213,268,323 — the root cause of my quality gripe), G13 IG 1080p profile, G14 drop forced -r 30, G16 yt-dlp bv*+ba merge. Verified with before/after ffprobe on real assets across multiple aspect ratios (9:16, 16:9, 1:1, 60fps) — a root-cause story is not a fix.
2. F3 analytics foundation: G18 fix nonexistent instagrapi calls + de-mock the tests that hid it, G22 per-(platform,post,timestamp) persistence — prerequisite for trends and future KB weighting. The persistence schema MUST be join-ready to the KB: keyed so a nugget's source post resolves to its metric snapshots by (platform, post_id) — co-design with the Nugget model NOW (Cato finding: descoping weighted retrieval while designing these separately fossilizes a non-weightable data model).
3. F4 engine correctness: G01 source hardcode, G02 DLQ deletes posted history, G03/G04 cross-flow dedup + content-hash keys. Double-posting is the cardinal sin — close every path.
4. Then parallel lanes per spec §6: A analytics+agent surface (G19–G21, G24–G28), B knowledge base (G30–G34: read-only doctor, SEMANTIC kb_query with provenance, LanceDB default, atomic writes), C docs front door (G49–G50: README rewrite with KB story + capability matrix + ToS disclosure, kill the TikTok-destination claim, fix tool counts), D UI (G38–G40 + micro-motion + splash polish), E integrations/safety (G44 sys.frozen guard, G45 constrained updates + rollback, G48 pins, G51 state backup/export, G52 MCP guardrails incl. the config_show password-hash leak, G55 failures list/retry). G52 is NOT satisfiable by documentation alone (Cato): a minimal implemented guardrail — read-only/require-confirm mode flag on the MCP server — ships this week; an unauthenticated surface that can post to real accounts cannot pass on a doc note. ISC-7 (effectiveness) is satisfied through G32: LanceDB-backed search replaces the O(n) full-scan JsonKnowledgeStore query path, plus one profiling pass over upload/analytics/KB-ingest hot paths recorded in the AUDIT doc.
5. RC: tag v0.1.0-rc on real CI, build artifacts, hand me the macOS + Windows smoke checklists.

WORKING AGREEMENTS (binding — from 2026-06-10/11 session lessons):
- Implementer agents return plain text; StructuredOutput schemas only on light scouts.
- Implementers work on the CURRENT base — never a stale worktree.
- Completion is judged against ISC probes by the primary, never by the implementing agent.
- Gates green at every commit: pytest, ruff, mypy, import-linter, pip-audit.
- LIVE-PROBE MANDATE for analytics: mock-passing tests are how the nonexistent-instagrapi-API bug survived. Analytics ISCs verify against real client objects (import the real instagrapi and assert the methods exist) and, where an authenticated session is available, one real fetch. De-mock the tests that fabricate APIs (G18) before marking anything analytics-related done.
- SECURITY INCIDENTS get rotation + regression test, not redaction-only: the MCP config_show password-hash leak (G26) means the dashboard credential is rotated AND a test asserts masked output. Same standard for any other credential exposure found.
- Double-post fixes land as a persistence-layer idempotency guard with durable-row evidence (same exactly-once standard as prior X1 work), not just deleted call sites; races and retries must be covered by tests.
- Descoped features must be INERT: half-wired scaffolding for descoped items (multi-account branches, weighted-retrieval stubs) is removed or proven load-bearing-free, not left as hazards.
- Adversarial review on every lane before commit; reviewer findings fixed before merge.
- Conventional commits; push feat/knowledge-base; keep PR #4 current.
- Honest descopes STAY descoped this week (spec §6): no TikTok-as-destination, no story-reposts (platform-impossible), no analytics-weighted retrieval yet (ship semantic search + persistence; weighting is the roadmap's first item), no multi-account implementation (document the decision, G54).
- Anti-bloat: no new dependency without a weight/benefit note in ISA ## Decisions.

OWNER-GATED (mine — prompt me, don't wait silently):
- GitHub Actions billing fix — gates ALL CI proof. I do this in GitHub settings first.
- macOS + Windows manual smoke + visual checks (Fri/Sat) — give me a checklist per OS.
- Signing decision: ship RC unsigned with SmartScreen/Gatekeeper notes vs wait for certs.
- Single vs multi-account scope statement (G54, document-only).

SCOPE CONTROL (195 ISCs is the project's done-condition, not one week's):
- P0 = every ISC tied to a CRIT/HIGH gap-register row plus the Day-0 CI gate — these block the RC tag.
- Descopes map to named ISCs marked [DEFERRED-VERIFY] with a roadmap entry (TikTok destination, story-reposts, weighted retrieval, multi-account, KB desktop page, full keyboard a11y) — the "OR roadmap-documented" probes in ISC-65+ already encode this.
- Post a per-day burn-down at end of day: ISCs passed / deferred / remaining, so I can re-scope Friday if the curve says Saturday slips.
- NO PREMATURE TAG (Cato: ~30 CRIT/HIGH gaps in a Thu–Sat window is over-committed if anything stalls): if the Friday burn-down shows P0 incomplete, the RC slips to Sunday/Monday and you tell me plainly — a slipped honest RC beats a repeat of the d5c9224 false ship-ready. Budget for the F2 re-test fan-out (every upload path re-verifies after scaling lands) and merge contention on engine/state files.

Work until the ISA is the only thing that says we're done.
