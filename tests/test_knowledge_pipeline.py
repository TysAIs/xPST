from pathlib import Path

from xpst.knowledge.ingest.pipeline import IngestResult, ingest
from xpst.knowledge.ingest.transcribe import Segment, Transcript
from xpst.knowledge.manifest import Manifest
from xpst.knowledge.store.json_store import JsonKnowledgeStore


class FakeTranscriber:
    def transcribe(self, media_path: Path) -> Transcript:
        return Transcript(text="key idea here. second idea.", segments=[
            Segment(start=0.0, end=3.5, text="key idea here."),
            Segment(start=3.5, end=6.0, text="second idea."),
        ])


class _ExplodingTranscriber:
    def transcribe(self, media_path: Path) -> Transcript:
        raise RuntimeError("boom: corrupt media")


class FakeExtractor:
    """Returns two nugget dicts within transcript bounds."""

    def __init__(self):
        self.calls = 0

    def __call__(self, transcript, client):
        self.calls += 1
        return [
            {"point": "key idea here", "timestamp_start": 0.0,
             "timestamp_end": 3.5},
            {"point": "second idea", "timestamp_start": 3.5,
             "timestamp_end": 6.0},
        ]


class _ExplodingExtractor:
    def __call__(self, transcript, client):
        raise RuntimeError("extraction failed")


class FakeEmbedder:
    dim = 2

    def embed(self, texts):
        return [[float(len(t)), 1.0] for t in texts]


def _ingest(tmp_path, *, transcriber=None, extractor=None, embedder=None):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fake")
    store = JsonKnowledgeStore(tmp_path / "nuggets.json")
    manifest = Manifest(tmp_path / "manifest.json")
    return ingest(
        str(media),
        store=store,
        transcriber=transcriber or FakeTranscriber(),
        manifest=manifest,
        embedder=embedder or FakeEmbedder(),
        extractor=extractor or FakeExtractor(),
        llm_client=object(),
    ), store, manifest


def test_ingest_produces_embedded_nuggets(tmp_path):
    result, store, _m = _ingest(tmp_path)
    assert isinstance(result, IngestResult)
    assert result.skipped is False
    assert result.reason is None
    assert len(result.nuggets) == 2
    # each stored nugget carries an embedding
    for n in store.all_nuggets():
        assert n.embedding != ()
        assert len(n.embedding) == 2


def test_ingest_records_source_in_manifest(tmp_path):
    _result, _store, manifest = _ingest(tmp_path)
    # reload manifest from disk to prove persistence
    reloaded = Manifest(manifest.path)
    sources = list(reloaded._data["sources"].keys())
    assert len(sources) == 1


def test_ingest_dedup_short_circuits_on_second_run(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fake")
    store = JsonKnowledgeStore(tmp_path / "nuggets.json")
    manifest = Manifest(tmp_path / "manifest.json")
    extractor = FakeExtractor()
    common = dict(store=store, transcriber=FakeTranscriber(),
                  manifest=manifest, embedder=FakeEmbedder(),
                  extractor=extractor, llm_client=object())
    first = ingest(str(media), **common)
    assert first.skipped is False
    second = ingest(str(media), **common)
    assert second.skipped is True
    assert second.reason
    # extractor only ran on the first pass
    assert extractor.calls == 1
    assert len(list(store.all_nuggets())) == 2


def test_ingest_transcribe_failure_returns_empty_no_partial_store(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fake")
    store = JsonKnowledgeStore(tmp_path / "nuggets.json")
    manifest = Manifest(tmp_path / "manifest.json")
    result = ingest(str(media), store=store,
                    transcriber=_ExplodingTranscriber(),
                    manifest=manifest, embedder=FakeEmbedder(),
                    extractor=FakeExtractor(), llm_client=object())
    assert result.skipped is False
    assert result.nuggets == []
    assert result.reason
    assert "boom" in result.reason
    # nothing written, source not recorded
    assert list(store.all_nuggets()) == []
    assert len(Manifest(manifest.path)._data["sources"]) == 0


def test_ingest_extract_failure_returns_empty_no_partial_store(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fake")
    store = JsonKnowledgeStore(tmp_path / "nuggets.json")
    manifest = Manifest(tmp_path / "manifest.json")
    result = ingest(str(media), store=store, transcriber=FakeTranscriber(),
                    manifest=manifest, embedder=FakeEmbedder(),
                    extractor=_ExplodingExtractor(), llm_client=object())
    assert result.nuggets == []
    assert result.reason
    assert list(store.all_nuggets()) == []
    assert len(Manifest(manifest.path)._data["sources"]) == 0
