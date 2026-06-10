from pathlib import Path

from click.testing import CliRunner

from xpst.cli import main
from xpst.knowledge.ingest.transcribe import Segment, Transcript


class _FakeTranscriber:
    def transcribe(self, media_path: Path) -> Transcript:
        return Transcript(text="cli idea", segments=[
            Segment(start=0.0, end=2.0, text="cli idea")])


class _FakeEmbedder:
    dim = 2
    model_name = "fake"

    def embed(self, texts):
        return [[float(len(t)), 1.0] for t in texts]


class _FakeExtractorClient:
    """Stands in for both the llm client and the extractor result."""

    def chat_json(self, messages):  # not used: extractor is monkeypatched
        return {"nuggets": []}


def _patch_kb(monkeypatch):
    monkeypatch.setattr(
        "xpst.knowledge.cli_kb._build_transcriber",
        lambda config: _FakeTranscriber(),
    )
    monkeypatch.setattr(
        "xpst.knowledge.cli_kb._build_embedder",
        lambda config: _FakeEmbedder(),
    )
    monkeypatch.setattr(
        "xpst.knowledge.cli_kb._build_llm_client",
        lambda config: _FakeExtractorClient(),
    )

    def _fake_extract(transcript, client):
        return [{"point": "cli idea", "timestamp_start": 0.0,
                 "timestamp_end": 2.0}]

    # The pipeline imports extract_nuggets as its default extractor.
    monkeypatch.setattr(
        "xpst.knowledge.ingest.pipeline._default_extract", _fake_extract
    )


def test_kb_add_then_query(tmp_path, monkeypatch):
    monkeypatch.setenv("XPST_HOME", str(tmp_path))
    _patch_kb(monkeypatch)
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fake")
    runner = CliRunner()

    add = runner.invoke(main, ["kb", "add", str(media)])
    assert add.exit_code == 0, add.output
    assert "Ingested" in add.output

    q = runner.invoke(main, ["kb", "query", "cli"])
    assert q.exit_code == 0, q.output
    assert "cli idea" in q.output


def test_kb_add_twice_reports_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("XPST_HOME", str(tmp_path))
    _patch_kb(monkeypatch)
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fake")
    runner = CliRunner()

    first = runner.invoke(main, ["kb", "add", str(media)])
    assert first.exit_code == 0, first.output

    second = runner.invoke(main, ["kb", "add", str(media)])
    assert second.exit_code == 0, second.output
    assert "Skipped" in second.output
