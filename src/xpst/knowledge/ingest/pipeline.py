"""Phase 2 ingestion pipeline:

  resolve -> (manifest dedup short-circuit) -> transcribe -> extract_nuggets
          -> embed each -> store each -> IngestResult

Synchronous. The durable queue + worker arrive in Phase 5.

Reliability (spec §5): a failed transcription or extraction returns an
``IngestResult`` with an empty nugget list and a reason, and NEVER writes a
partial store — store writes and the manifest record happen only after every
nugget is successfully built and embedded.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from xpst.knowledge.ingest.extract import extract_nuggets as _default_extract
from xpst.knowledge.ingest.resolve import resolve_source, source_id
from xpst.knowledge.ingest.transcribe import Transcriber, Transcript
from xpst.knowledge.models import Nugget

if TYPE_CHECKING:
    from xpst.knowledge.llm.embeddings import Embedder
    from xpst.knowledge.manifest import Manifest
    from xpst.knowledge.store.base import KnowledgeStore


@dataclass(frozen=True)
class IngestResult:
    nuggets: list[Nugget] = field(default_factory=list)
    skipped: bool = False
    reason: str | None = None


# Extractor signature: (transcript, llm_client) -> list[nugget dict]
Extractor = Callable[[Transcript, Any], list[dict[str, Any]]]


def ingest(source: str, *, store: KnowledgeStore, transcriber: Transcriber,
           manifest: Manifest, embedder: Embedder, llm_client: Any,
           extractor: Extractor | None = None) -> IngestResult:
    extractor = extractor or _default_extract
    sid = source_id(source)

    # Dedup short-circuit: a source recorded in the manifest is never re-ingested.
    if manifest.has_source(sid):
        return IngestResult(nuggets=[], skipped=True,
                            reason=f"already ingested: {sid}")

    is_url = source.startswith(("http://", "https://"))
    source_url = source if is_url else None

    # Build everything in memory first; only persist on full success so a bad
    # video can never corrupt the store (spec §5: graceful degradation).
    try:
        media_path = resolve_source(source)
        transcript = transcriber.transcribe(media_path)
        raw_nuggets = extractor(transcript, llm_client)
        points = [r["point"] for r in raw_nuggets]
        vectors = embedder.embed(points) if points else []
        embed_dim = embedder.dim if points else 0
        built: list[Nugget] = []
        for raw, vec in zip(raw_nuggets, vectors, strict=False):
            nugget = Nugget.create(
                point=raw["point"],
                source_video_id=sid,
                timestamp_start=float(raw["timestamp_start"]),
                timestamp_end=float(raw["timestamp_end"]),
                source_url=source_url,
            ).with_embedding(vec)
            built.append(nugget)
    except Exception as exc:  # noqa: BLE001 - one bad video must not corrupt the store
        return IngestResult(nuggets=[], skipped=False, reason=str(exc))

    # Success path: persist nuggets, then record the source in the manifest.
    for nugget in built:
        store.add_nugget(nugget)
    manifest.record(sid, source=source_url,
                    embed_model=getattr(embedder, "model_name", "unknown"),
                    embed_dim=embed_dim)
    return IngestResult(nuggets=built, skipped=False, reason=None)
