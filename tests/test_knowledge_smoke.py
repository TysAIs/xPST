"""Opt-in: actually runs faster-whisper + fastembed end to end against a local
LLM. Skips unless RUN_KB_SMOKE=1 and the extras are installed. Downloads the
'base' whisper model and the embedding model on first run.

Required env when RUN_KB_SMOKE=1:
  KB_SMOKE_CLIP  — path to a short local audio/video file
  XPST_KB_LLM_BASE_URL / XPST_KB_LLM_MODEL — an OpenAI-compatible endpoint
"""
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_KB_SMOKE") != "1",
    reason="set RUN_KB_SMOKE=1 to run the real faster-whisper smoke test",
)


def test_real_transcription_produces_nugget(tmp_path):
    faster_whisper = pytest.importorskip("faster_whisper")  # noqa: F841
    pytest.importorskip("fastembed")
    from xpst.knowledge.config import KnowledgeConfig
    from xpst.knowledge.ingest.pipeline import ingest
    from xpst.knowledge.ingest.transcribe import FasterWhisperTranscriber
    from xpst.knowledge.llm.client import LLMClient
    from xpst.knowledge.llm.embeddings import build_embedder
    from xpst.knowledge.manifest import Manifest
    from xpst.knowledge.store.json_store import JsonKnowledgeStore

    config = KnowledgeConfig.from_env()
    clip = Path(os.environ["KB_SMOKE_CLIP"])  # a short local audio/video file
    store = JsonKnowledgeStore(tmp_path / "nuggets.json")
    manifest = Manifest(tmp_path / "manifest.json")
    result = ingest(
        str(clip),
        store=store,
        transcriber=FasterWhisperTranscriber(model_size=config.whisper_model),
        manifest=manifest,
        embedder=build_embedder(config),
        llm_client=LLMClient(base_url=config.llm_base_url,
                             model=config.llm_model,
                             api_key=config.llm_api_key),
    )
    assert result.reason is None, result.reason
    assert result.nuggets
    for nugget in result.nuggets:
        assert nugget.point.strip()
        assert nugget.embedding != ()
        assert store.has_nugget(nugget.id)
