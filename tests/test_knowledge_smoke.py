"""Opt-in: actually runs faster-whisper. Skips unless RUN_KB_SMOKE=1 and the
extra is installed. Downloads the 'base' model on first run."""
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_KB_SMOKE") != "1",
    reason="set RUN_KB_SMOKE=1 to run the real faster-whisper smoke test",
)


def test_real_transcription_produces_nugget(tmp_path):
    faster_whisper = pytest.importorskip("faster_whisper")  # noqa: F841
    from xpst.knowledge.ingest.pipeline import ingest
    from xpst.knowledge.ingest.transcribe import FasterWhisperTranscriber
    from xpst.knowledge.store.json_store import JsonKnowledgeStore

    clip = Path(os.environ["KB_SMOKE_CLIP"])  # a short local audio/video file
    store = JsonKnowledgeStore(tmp_path / "nuggets.json")
    nugget = ingest(str(clip), store=store,
                    transcriber=FasterWhisperTranscriber(model_size="base"))
    assert nugget.point.strip()
    assert store.has_nugget(nugget.id)
