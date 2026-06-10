"""Phase 1 ingestion pipeline: resolve -> transcribe -> one nugget -> store.
Synchronous. The durable queue + worker arrive in Phase 5."""
from __future__ import annotations

from xpst.knowledge.ingest.resolve import resolve_source, source_id
from xpst.knowledge.ingest.transcribe import Transcriber
from xpst.knowledge.models import Nugget
from xpst.knowledge.store.base import KnowledgeStore


def ingest(source: str, *, store: KnowledgeStore,
           transcriber: Transcriber) -> Nugget:
    media_path = resolve_source(source)
    transcript = transcriber.transcribe(media_path)
    is_url = source.startswith(("http://", "https://"))
    nugget = Nugget.create(
        point=transcript.text,
        source_video_id=source_id(source),
        timestamp_start=transcript.start,
        timestamp_end=transcript.end,
        source_url=source if is_url else None,
    )
    store.add_nugget(nugget)
    return nugget
