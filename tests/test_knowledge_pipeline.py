from pathlib import Path

from xpst.knowledge.ingest.pipeline import ingest
from xpst.knowledge.ingest.transcribe import Transcript, Segment
from xpst.knowledge.store.json_store import JsonKnowledgeStore


class FakeTranscriber:
    def transcribe(self, media_path: Path) -> Transcript:
        return Transcript(text="key idea here", segments=[
            Segment(start=0.0, end=3.5, text="key idea here"),
        ])


def test_ingest_produces_one_cited_nugget(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fake")
    store = JsonKnowledgeStore(tmp_path / "nuggets.json")
    nugget = ingest(str(media), store=store, transcriber=FakeTranscriber())
    assert nugget.point == "key idea here"
    assert nugget.timestamp_start == 0.0
    assert nugget.timestamp_end == 3.5
    assert nugget.source_url is None
    assert store.has_nugget(nugget.id)


def test_ingest_is_idempotent(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fake")
    store = JsonKnowledgeStore(tmp_path / "nuggets.json")
    ingest(str(media), store=store, transcriber=FakeTranscriber())
    ingest(str(media), store=store, transcriber=FakeTranscriber())
    assert len(list(store.all_nuggets())) == 1
