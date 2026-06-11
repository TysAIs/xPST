# xPST Knowledge Base Plugin — Specification & Build Plan

> **For agentic workers:** This is a spec, not a task-by-task implementation plan. When a phase is greenlit, expand it into a TDD plan using `superpowers:writing-plans`, then execute with `superpowers:subagent-driven-development` or `superpowers:executing-plans`. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional, self-contained Knowledge Base to xPST that ingests videos (any source), transcribes them, extracts key points, auto-organizes them into topic areas ordered beginner→advanced, and exposes the result over MCP/CLI so any plugged-in AI can build a course from it.

**Architecture:** A walled-off subsystem (`src/xpst/knowledge/`) shipped as an optional dependency extra (`xpst[knowledge]`). The cross-poster core never imports it; it is wired into the CLI/MCP through lazy imports guarded by the extra, the same pattern xPST already uses for its `mcp`, `desktop`, and `dashboard` features. (xPST's sandboxed `PluginManager` is for untrusted third-party uploaders and blocks `os`/`subprocess`; the KB is first-party and needs those, so it does not ride that loader.) The organizing intelligence lives in the pipeline (embeddings route, clustering discovers areas, code sorts difficulty), so a small ~12B local model is sufficient. All volatile dependencies sit behind stable adapter interfaces.

**Tech Stack:** Python (matches xPST), faster-whisper (transcription), LanceDB (embedded vector store), Graphify (area clustering — already in the owner's stack), an OpenAI-compatible LLM client (model-agnostic), a small local embedding model. No required cloud keys.

---

## 1. Non-Negotiable Design Principles

These came directly out of the design conversation. Every phase is checked against them.

1. **Multi-tenant by design — zero hardcoded identity.** No user-specific constants anywhere in the code. The KB is empty on install and becomes "yours" purely from the corpus you feed it. Same code for every user, tailored output per user, because tailoring is a function of *data*, not constants. Each install/workspace gets its own isolated data directory.
2. **Anti-bloat — the core cannot get heavier.** Ships as an optional extra and is wired into CLI/MCP through lazy imports guarded by the extra (the pattern xPST already uses for `mcp`/`desktop`/`dashboard`), so it activates at runtime *only if installed*. The cross-poster core (`engine`, `platforms`, `sources` consumers) never imports the KB. Dependency direction is one-way: KB → xPST `sources/`, never the reverse. `pip install xpst` = pure cross-poster with zero new deps on disk. (The KB is first-party and needs `os`/`subprocess` for ffmpeg/whisper, so it deliberately does not ride the sandboxed `PluginManager`.)
3. **Small-model-friendly — intelligence in the pipeline, not the model.** The LLM only does narrow, schema-bound transforms (extract nuggets, name an area, tag difficulty, write final prose). Routing is embedding similarity. Area discovery is clustering. Ordering is deterministic code. A 12B model must be able to run the whole thing.
4. **Agnostic to any AI.** The LLM and embedding backends are reached through an OpenAI-compatible interface (`base_url` + `model`). Local Qwen, a small Gemma, Claude via shim, OpenAI — all the same code path. The consumption surface is MCP + CLI so any agent can drive it.
5. **Reliability first — it must WORK.** Every phase has a concrete acceptance test that proves behavior on real input. Ingestion is idempotent (content-hash dedup). A bad video fails its queue item and never corrupts the store. Everything runs offline and $0 by default.
6. **Fully automatic and intuitive.** Areas are discovered, not seeded. Difficulty and ordering are assigned automatically. The user action is "drop a link/file" and nothing else.

---

## 2. System Architecture

### 2.1 Pipeline (end to end)

```
link/file dropped into a workspace
        │
        ▼
  ingestion queue        ← reuse xPST's durable queue + worker pattern
        │
        ▼
  source resolver        ← reuse xPST sources/ (tiktok, x, youtube, instagram, local)
        │
        ▼
  faster-whisper         ← transcript + word/segment timestamps
        │
        ▼
  LLM extract            ← small model fills a strict JSON nugget schema
        │
        ▼
  embed                  ← small local embedding model
        │
        ├──► route        ← nearest existing area by vector similarity
        │                   (clustering grows NEW areas when nothing fits)
        ▼
  KnowledgeStore         ← LanceDB (vectors) + Graphify (areas/graph) behind one interface
        │
        ▼
  MCP tools + CLI        ← any plugged-in AI assembles a course from
                            pre-ordered, cited nuggets
```

### 2.2 Module layout (the wall)

```
src/xpst/knowledge/              # the entire KB plugin — removable, optional
  __init__.py                    # public KB API; imported lazily by CLI/MCP only when the extra is installed
  config.py                      # KnowledgeConfig — separate from the XPSTConfig god node
  models.py                      # Nugget, Area, Workspace, QueueItem schemas
  workspace.py                   # per-tenant data dir resolution + isolation
  queue.py                       # ingestion queue (mirrors xPST's posting queue)
  ingest/
    pipeline.py                  # orchestrates resolve → transcribe → extract → embed → store
    transcribe.py                # faster-whisper adapter (behind a Transcriber interface)
    extract.py                   # LLM nugget extraction (strict JSON schema)
  store/
    base.py                      # KnowledgeStore ABC — the stable interface
    vector_lancedb.py            # LanceDB adapter
    graph_graphify.py            # Graphify adapter (clustering + area graph)
    composite.py                 # delegates to vector + graph behind one KnowledgeStore
  organize/
    router.py                    # embedding-based routing of a nugget into an area
    cluster.py                   # area discovery (clustering) + auto-labeling
    difficulty.py                # difficulty tagging + deterministic ordering
  llm/
    client.py                    # OpenAI-compatible chat client (agnostic)
    embeddings.py                # embedding model adapter (agnostic)
  course/
    assemble.py                  # query → ordered, cited nuggets → hand to plugged-in AI
  mcp/
    tools.py                     # KB tools registered into the existing mcp/server.py
  cli.py                         # `xpst kb ...` subcommands
```

**Dependency direction is enforced by review and an import-linter check:** nothing under `src/xpst/` outside `knowledge/` may import `knowledge`. `knowledge` may import `xpst.sources` and shared utils only through their public interfaces.

### 2.3 Multi-tenant data model

Each workspace is a self-contained directory. Default workspace is `default`. No identity is encoded in code — a workspace is just a name and a folder.

```
~/.xpst/knowledge/<workspace>/
  lancedb/            # vector store
  graph/              # Graphify area artifacts
  queue.db            # durable ingestion queue (sqlite)
  manifest.json       # ingested sources, content hashes, versions
```

For the original author, the `default` workspace fills with their videos and the KB tailors to him. For anyone else, theirs. Identical code, isolated data.

---

## 3. Data Contracts

These are design contracts, not implementations. They lock the interfaces every phase depends on.

### 3.1 Nugget

```
Nugget:
  id: str                       # content hash of (source_video_id + point) — dedup key
  point: str                    # the key idea, 1–3 sentences
  area_id: str | None           # assigned by router; None until organized
  difficulty: "beginner" | "intermediate" | "advanced"
  prerequisites: list[str]      # concept labels this point assumes
  source_video_id: str          # which video it came from
  source_url: str | None        # original link, for citation
  timestamp_start: float        # seconds into the video — citation back to the clip
  timestamp_end: float
  embedding: list[float]        # for routing + semantic recall
  created_at: float
```

### 3.2 Area (a course module — discovered, not seeded)

```
Area:
  id: str
  label: str                    # auto-named from the cluster's top nuggets
  centroid: list[float]         # cluster center — used to route new nuggets
  nugget_ids: list[str]
  order_index: int              # area-level ordering (intro areas first)
```

### 3.3 KnowledgeStore interface (the stable port)

```
class KnowledgeStore(ABC):
  def add_nugget(nugget: Nugget) -> None
  def get_nugget(nugget_id: str) -> Nugget | None
  def has_nugget(nugget_id: str) -> bool          # idempotency check
  def search(embedding: list[float], k: int) -> list[Nugget]   # vector recall
  def all_nuggets() -> Iterable[Nugget]
  def upsert_area(area: Area) -> None
  def areas() -> list[Area]
  def assign(nugget_id: str, area_id: str) -> None
```

`composite.py` implements this by delegating vector ops to LanceDB and area/cluster ops to Graphify. Swapping either backend means rewriting one adapter, not the pipeline.

### 3.4 Agnostic LLM + embedding config

```
KnowledgeConfig:
  llm_base_url: str             # e.g. http://127.0.0.1:8000/v1  (any OpenAI-compatible)
  llm_model: str                # e.g. qwen3.6-35b-a3b OR gemma-12b OR a cloud model
  llm_api_key: str | None       # optional; local needs none
  embed_backend: str = "fastembed"             # "fastembed" (in-process ONNX/CPU, default) or "endpoint"
  embed_model: str = "nomic-ai/nomic-embed-text-v1.5"   # CPU/RAM only, ~100MB, 8192-token context
  embed_base_url: str | None = None            # only used when embed_backend == "endpoint"
  workspace: str = "default"
  whisper_model: str = "base"   # faster-whisper size; tunable per machine
```

---

## 4. Consumption Surface (MCP + CLI)

The KB is driven entirely through these. No GUI is required for it to work.

**CLI (`xpst kb ...`):**
- `xpst kb add <url|path> [--workspace W]` — enqueue a video for ingestion.
- `xpst kb status` — queue + ingestion progress.
- `xpst kb areas` — list discovered areas in course order.
- `xpst kb query "<question>" [--area A]` — return ordered, cited nuggets.
- `xpst kb course [--area A]` — emit the organized outline (areas → ordered nuggets with citations) for an AI to write from.
- `xpst kb doctor` — health check (deps present, store reachable, queue sane).

**MCP tools (registered into the existing `mcp/server.py`):**
- `kb_add(source)` · `kb_status()` · `kb_areas()` · `kb_query(question, area?)` · `kb_course(area?)`

`kb_course` is the key one: it hands the plugged-in AI a pre-organized, pre-ordered, cited structure so even a small model only has to write prose, never invent the organization.

---

## 5. Reliability Strategy (the "must WORK" core)

- **Golden eval corpus.** A fixture set of short pre-transcribed inputs with expected nugget counts and expected area groupings. A test asserts the pipeline produces sane, stable output. This is the standing proof that it works.
- **Idempotent ingestion.** Content-hash dedup on `Nugget.id` and on source manifest. Re-adding a video produces no duplicates. (Reuse xPST's existing composite-key dedup pattern from state management.)
- **Graceful degradation.** A failed transcription or extraction marks the queue item `failed` with a reason and moves on. One bad video can never corrupt the store or block the queue.
- **Offline + $0 by default.** Local whisper + local embeddings + local LLM. Cloud is opt-in via config, never required.
- **Fresh-machine portability test.** Part of Phase 5 acceptance: a clean install of `xpst[knowledge]` with default config ingests and organizes with nothing pre-configured.

---

## 6. Five-Phase Build Plan

Each phase produces working, testable software on its own and has a hard acceptance test.

### Phase 1 — Walled skeleton + one video end-to-end (the proof)
**Build:** `xpst[knowledge]` optional extra wired in `pyproject.toml`; `kb` CLI group attached via lazy import (friendly message if the extra is absent); workspace data dir; `KnowledgeConfig`; a minimal pipeline (local file or URL → faster-whisper → store one whole-transcript "nugget" in a simple JSON store behind the `KnowledgeStore` interface → `xpst kb query` returns it with citation). LanceDB + embeddings are deferred to Phase 2; no areas, no difficulty, no LLM extraction yet.
**Acceptance:** feed one known short video, run `xpst kb query`, get back a citeable result with correct source + timestamp. Core xPST test suite still green with the extra *not* installed.

### Phase 2 — Real extraction + embeddings + dedup
**Build:** LLM nugget extraction over the agnostic endpoint with a strict JSON schema; embedding adapter; content-hash idempotency; manifest tracking.
**Acceptance:** golden test on a fixture transcript yields sensible nuggets matching the schema; ingesting the same video twice adds zero duplicates; runs against a small local model.

### Phase 3 — Auto-organization (areas + difficulty)
**Build:** embedding router (nearest area); clustering for area discovery; auto-labeling of areas; difficulty tagging; deterministic beginner→advanced ordering within and across areas.
**Acceptance:** feed a fixture of videos spanning 2–3 topics, the system discovers approximately the right areas, names them sanely, and orders content beginner→advanced. Asserted against the golden corpus.

### Phase 4 — Consumption surface (MCP + CLI) and the agnostic AI hookup
**Build:** all `xpst kb` CLI subcommands; KB MCP tools registered into the existing server; `kb_course` assembly that returns organized, ordered, cited structure.
**Acceptance:** from an MCP client (Claude Code or any agent) and with a *small* local model as the brain, query the KB and a build a course outline from real ingested videos, with citations intact.

### Phase 5 — Queue UX + hardening + portability
**Build:** "drop a link/file into a workspace" intake (CLI-first; optional desktop-app hook); background worker; `kb doctor`; failure-path hardening; import-linter rule enforcing the wall.
**Acceptance:** on a clean machine, `pip install xpst[knowledge]`, drop 3 links, walk away, return to an organized multi-area course ordered by difficulty. Reliability eval green. Core cross-poster unaffected throughout.

---

## 7. Dependency / Fragility Strategy

- KB deps live only in the `[knowledge]` optional extra. The core never imports them.
- Every volatile library sits behind an adapter (`Transcriber`, `KnowledgeStore`, LLM/embedding clients). A breaking upstream change is contained to one file.
- Pin everything via `uv.lock`. Add a per-adapter smoke test to CI so a bad dependency update is caught before it ships.
- `LightRAG`, `VideoRAG`, and `Understand-Anything` are **pattern references, not runtime dependencies.** They are days-old / research-grade and would import their own storage + LLM orchestration. We borrow their approach (chunking, extraction, agent-queryable graph) and implement thin versions against stable primitives we control.
- The graph/area layer reuses **Graphify** (an in-house tool, controlled update cadence) rather than adopting an external KG framework.

---

## 8. Open Questions (decide before Phase 2/3)

1. **Embedding model — RESOLVED 2026-06-10.** `nomic-ai/nomic-embed-text-v1.5` (fastembed's canonical id; the bare `nomic-embed-text-v1.5` is not recognized by `TextEmbedding`), run in-process via `fastembed` (ONNX on CPU, no PyTorch). RAM-only, ~200–500MB while actively embedding, ~100MB on disk, 8192-token context for long transcript chunks. Loaded lazily by the ingestion worker only when embedding or querying — not always-on, never loaded by the core cross-poster. Power users can override to any OpenAI-compatible embedding endpoint via `embed_backend="endpoint"`. The model + dimension are recorded in `manifest.json`; changing the model triggers a full re-embed of the workspace.
2. **Graph layer integration.** Graphify invoked as a subprocess on the nugget set, vs a light in-process clustering (e.g. HDBSCAN) with Graphify only for the human-facing graph view. Driver: keep it simple and small-model-independent. (Needed at Phase 3.)
3. **Desktop "areas" UI scope.** CLI-first now and add the drag-a-link-into-an-area UI later, vs build a minimal desktop intake in Phase 5. Driver: how soon the owner wants the visual workflow vs proving the engine first.

---

## 9. Self-Review Against the Spec

- Multi-tenant (§1.1) → workspace model §2.3, acceptance in Phase 5. ✔
- Anti-bloat (§1.2) → optional extra + plugin + one-way imports §2.2, import-linter Phase 5, Phase 1 acceptance checks core unaffected. ✔
- Small-model (§1.3) → pipeline-not-model table §2.1 / §3, `kb_course` hands pre-organized structure §4. ✔
- Agnostic AI (§1.4) → OpenAI-compatible config §3.4, MCP+CLI §4, Phase 4 acceptance uses a small model. ✔
- Reliability (§1.5) → §5 + an acceptance test on every phase. ✔
- Fully automatic (§1.6) → discovered areas §6 Phase 3, "drop a link" intake §6 Phase 5. ✔
- No fabricated dependencies: every named tool (faster-whisper, LanceDB, Graphify, OpenAI-compatible client) is real and either in-stack or stable. ✔
