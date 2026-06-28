import pytest

from xpst.knowledge.ingest.resolve import resolve_source, source_id


def test_resolve_existing_local_file(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"fake")
    assert resolve_source(str(f)) == f


def test_resolve_missing_local_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        resolve_source(str(tmp_path / "nope.mp4"))


def test_source_id_is_stable():
    assert source_id("http://x/v") == source_id("http://x/v")
