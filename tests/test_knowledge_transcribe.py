from xpst.knowledge.ingest.transcribe import Transcript, Segment


def test_transcript_full_span():
    t = Transcript(text="hello world", segments=[
        Segment(start=0.0, end=2.0, text="hello"),
        Segment(start=2.0, end=4.0, text="world"),
    ])
    assert t.start == 0.0
    assert t.end == 4.0


def test_transcript_empty_span_is_zero():
    t = Transcript(text="", segments=[])
    assert t.start == 0.0
    assert t.end == 0.0
