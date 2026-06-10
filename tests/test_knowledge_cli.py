from pathlib import Path

from click.testing import CliRunner

from xpst.cli import main
from xpst.knowledge.ingest.transcribe import Transcript, Segment


class _FakeTranscriber:
    def transcribe(self, media_path: Path) -> Transcript:
        return Transcript(text="cli idea", segments=[
            Segment(start=0.0, end=2.0, text="cli idea")])


def test_kb_add_then_query(tmp_path, monkeypatch):
    monkeypatch.setenv("XPST_HOME", str(tmp_path))
    monkeypatch.setattr(
        "xpst.knowledge.cli_kb._build_transcriber",
        lambda: _FakeTranscriber(),
    )
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fake")
    runner = CliRunner()

    add = runner.invoke(main, ["kb", "add", str(media)])
    assert add.exit_code == 0, add.output

    q = runner.invoke(main, ["kb", "query", "cli"])
    assert q.exit_code == 0, q.output
    assert "cli idea" in q.output
