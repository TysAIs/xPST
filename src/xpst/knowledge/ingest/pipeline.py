"""Phase 2/4 ingestion pipeline:

  resolve -> (manifest dedup short-circuit) -> transcribe -> extract
          -> embed each -> store each -> IngestResult

Phase 4 additions:
- ``extract_mode`` selects nuggets/clips/topics extraction (4.2).
- ``source_platform`` / ``source_post_id`` provenance is stamped onto every
  built nugget so auto-ingested content carries performance history (4.1).
- A deterministic fallback runs when no LLM is configured or the configured
  LLM is unreachable, so the KB works out-of-box (4.4 / 4.5).
- :func:`ingest_text` is a search-only / no-transcription path for creators
  who already have a transcript (4.4).

Synchronous. The durable queue + worker live in ``knowledge/worker.py``.

Reliability (spec §5): a failed transcription or extraction returns an
``IngestResult`` with an empty nugget list and a reason, and NEVER writes a
partial store — store writes and the manifest record happen only after every
nugget is successfully built and embedded.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from xpst.knowledge.ingest.extract import (
    EXTRACT_NUGGETS,
    deterministic_extract,
)
from xpst.knowledge.ingest.extract import (
    extract_nuggets as _default_extract,
)
from xpst.knowledge.ingest.resolve import content_fingerprint, resolve_source, source_id
from xpst.knowledge.ingest.transcribe import Segment, Transcriber, Transcript
from xpst.knowledge.models import Nugget
from xpst.utils.logger import get_logger

if TYPE_CHECKING:
    from xpst.knowledge.llm.embeddings import Embedder
    from xpst.knowledge.manifest import Manifest
    from xpst.knowledge.store.base import KnowledgeStore

logger = get_logger(__name__)


@dataclass(frozen=True)
class IngestResult:
    nuggets: list[Nugget] = field(default_factory=list)
    skipped: bool = False
    reason: str | None = None


# Extractor signature: (transcript, llm_client) -> list[nugget dict]
Extractor = Callable[[Transcript, Any], list[dict[str, Any]]]


def _resolve_extractor(
    extractor: Extractor | None,
    llm_client: Any,
    extract_mode: str,
) -> tuple[Extractor, bool]:
    """Pick the effective extractor and whether it is the LLM-backed default.

    Phase 4.4/4.5: when no LLM client is configured, or the configured LLM
    probes unreachable, the deterministic fallback is used so ingestion never
    silently stalls on a missing local LLM. A caller-supplied ``extractor``
    always wins (tests rely on this)."""
    if extractor is not None:
        return extractor, False
    if llm_client is None:
        logger.info(
            "No extraction LLM configured; using deterministic fallback "
            "(mode=%s).", extract_mode,
        )
        return (lambda t, _c: deterministic_extract(t, mode=extract_mode)), False
    # Default LLM extractor. Guard reachability so an unreachable LLM produces
    # a clear notice + deterministic fallback rather than a stalled run.
    if hasattr(llm_client, "is_reachable") and not llm_client.is_reachable():
        logger.warning(
            "Extraction LLM is unreachable; falling back to deterministic "
            "extraction (mode=%s). Check XPST_KB_LLM_BASE_URL or set "
            "XPST_KB_LLM_ENABLED=0 to silence this.", extract_mode,
        )
        return (lambda t, _c: deterministic_extract(t, mode=extract_mode)), False
    return (lambda t, c: _default_extract(t, c, mode=extract_mode)), True


def _build_nuggets(
    transcript: Transcript,
    *,
    sid: str,
    source_url: str | None,
    embedder: Embedder,
    extractor: Extractor,
    llm_client: Any,
    source_platform: str | None = None,
    source_post_id: str | None = None,
) -> tuple[list[Nugget], int]:
    """Extract, embed, and build Nugget objects from a transcript.

    Returns (built_nuggets, embed_dim). Raises on failure so the caller's
    try/except converts it to an ``IngestResult`` reason."""
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
            source_platform=source_platform,
            source_post_id=source_post_id,
            title=raw.get("title"),
            topic=raw.get("topic"),
            tags=raw.get("tags") or (),
        ).with_embedding(vec)
        built.append(nugget)
    return built, embed_dim


def _persist(built: list[Nugget], store: KnowledgeStore, manifest: Manifest,
             *, sid: str, source_url: str | None, embedder: Embedder,
             embed_dim: int, content_sid: str | None) -> None:
    """Success path: persist nuggets, then record the source in the manifest."""
    for nugget in built:
        store.add_nugget(nugget)
    manifest.record(sid, source=source_url,
                    embed_model=getattr(embedder, "model_name", "unknown"),
                    embed_dim=embed_dim)
    if content_sid is not None and content_sid != sid:
        manifest.record_alias(content_sid, sid)


def ingest(source: str, *, store: KnowledgeStore, transcriber: Transcriber,
           manifest: Manifest, embedder: Embedder, llm_client: Any,
           extractor: Extractor | None = None,
           extract_mode: str = EXTRACT_NUGGETS,
           source_platform: str | None = None,
           source_post_id: str | None = None,
           workspace: Any = None) -> IngestResult:
    """Ingest a local file or URL: resolve -> transcribe -> extract -> embed.

    See module docstring for Phase 4 options. A failed transcription or
    extraction returns an ``IngestResult`` with an empty nugget list and a
    reason, and never writes a partial store.

    D2: If ``workspace`` is provided, transcripts are cached by content_hash
    so cross-posted videos are only transcribed once.
    """
    eff_extractor, _ = _resolve_extractor(extractor, llm_client, extract_mode)
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
        # Content-byte dedup (G33): two different source strings (e.g. share
        # URL vs canonical URL) resolving to the same media must not
        # double-ingest. The fingerprint is recorded as a manifest alias.
        content_sid = content_fingerprint(media_path)
        if manifest.has_source(content_sid):
            return IngestResult(nuggets=[], skipped=True,
                                reason=f"already ingested (content match): {content_sid}")

        # D2: Transcript dedup — check cache before transcribing
        transcript = None
        if workspace is not None:
            cached = workspace.get_transcript(content_sid)
            if cached is not None:
                logger.info("Using cached transcript for %s", content_sid)
                from xpst.knowledge.ingest.transcribe import Segment, Transcript
                segs = [Segment(start=s.get("start", 0), end=s.get("end", 0),
                                text=s.get("text", "")) for s in cached.get("segments", [])]
                transcript = Transcript(text=cached.get("text", ""), segments=segs)

        if transcript is None:
            transcript = transcriber.transcribe(media_path)
            # D2.2: Cache the transcript keyed by content_hash
            if workspace is not None:
                workspace.save_transcript(
                    content_sid,
                    text=transcript.text,
                    segments=[{"start": s.start, "end": s.end, "text": s.text}
                              for s in transcript.segments],
                )
        built, embed_dim = _build_nuggets(
            transcript, sid=sid, source_url=source_url, embedder=embedder,
            extractor=eff_extractor, llm_client=llm_client,
            source_platform=source_platform, source_post_id=source_post_id,
        )
    except Exception as exc:  # noqa: BLE001 - one bad video must not corrupt the store
        return IngestResult(nuggets=[], skipped=False, reason=str(exc))

    _persist(built, store, manifest, sid=sid, source_url=source_url,
             embedder=embedder, embed_dim=embed_dim, content_sid=content_sid)
    return IngestResult(nuggets=built, skipped=False, reason=None)


def _transcript_from_text(text: str) -> Transcript:
    """Build a synthetic :class:`Transcript` from raw text by sentence-splitting
    with sequential timestamps. Used by the search-only / no-transcription path
    (creators who already have a transcript)."""
    import re

    sent_re = re.compile(r"[^.!?\n]+[.!?]?")
    sents = [s.strip() for s in sent_re.findall(text) if s.strip()]
    if not sents:
        sents = [text.strip()] if text.strip() else []
    segments: list[Segment] = []
    t = 0.0
    for sent in sents:
        end = t + max(1.0, min(len(sent) / 15.0, 30.0))
        segments.append(Segment(start=t, end=end, text=sent))
        t = end
    return Transcript(text=" ".join(s.text for s in segments), segments=segments)


def ingest_text(source: str, text: str, *, store: KnowledgeStore,
                manifest: Manifest, embedder: Embedder,
                llm_client: Any = None,
                extractor: Extractor | None = None,
                extract_mode: str = EXTRACT_NUGGETS,
                source_platform: str | None = None,
                source_post_id: str | None = None) -> IngestResult:
    """Search-only / no-transcription ingestion (Phase 4.4).

    For creators who already have a transcript: skip media resolution and
    transcription and extract directly from ``text``. ``source`` is the
    dedup identity (URL/path/label). Same dedup, persistence, and graceful
    degradation contract as :func:`ingest`.
    """
    eff_extractor, _ = _resolve_extractor(extractor, llm_client, extract_mode)
    sid = source_id(source)

    if manifest.has_source(sid):
        return IngestResult(nuggets=[], skipped=True,
                            reason=f"already ingested: {sid}")

    is_url = source.startswith(("http://", "https://"))
    source_url = source if is_url else None

    try:
        transcript = _transcript_from_text(text)
        built, embed_dim = _build_nuggets(
            transcript, sid=sid, source_url=source_url, embedder=embedder,
            extractor=eff_extractor, llm_client=llm_client,
            source_platform=source_platform, source_post_id=source_post_id,
        )
    except Exception as exc:  # noqa: BLE001
        return IngestResult(nuggets=[], skipped=False, reason=str(exc))

    _persist(built, store, manifest, sid=sid, source_url=source_url,
             embedder=embedder, embed_dim=embed_dim, content_sid=None)
    return IngestResult(nuggets=built, skipped=False, reason=None)
