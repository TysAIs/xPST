from xpst.knowledge.manifest import Manifest


def test_record_and_has_source(tmp_path):
    m = Manifest(tmp_path / "manifest.json")
    assert not m.has_source("vid1")
    m.record("vid1", source="http://x/v", embed_model="nomic", embed_dim=768)
    assert m.has_source("vid1")


def test_persists_across_instances(tmp_path):
    path = tmp_path / "manifest.json"
    Manifest(path).record("vid1", source="s", embed_model="m", embed_dim=2)
    reloaded = Manifest(path)
    assert reloaded.has_source("vid1")


def test_schema_version_written(tmp_path):
    import json

    path = tmp_path / "manifest.json"
    m = Manifest(path)
    m.record("vid1", source="s", embed_model="m", embed_dim=2)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == Manifest.SCHEMA_VERSION
    assert "vid1" in data["sources"]


def test_record_is_idempotent(tmp_path):
    path = tmp_path / "manifest.json"
    m = Manifest(path)
    m.record("vid1", source="s", embed_model="m", embed_dim=2)
    m.record("vid1", source="s", embed_model="m", embed_dim=2)
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    assert list(data["sources"].keys()) == ["vid1"]


def test_atomic_write_leaves_no_temp_files(tmp_path):
    path = tmp_path / "manifest.json"
    m = Manifest(path)
    m.record("vid1", source="s", embed_model="m", embed_dim=2)
    leftovers = list(tmp_path.glob("*.tmp.*"))
    assert leftovers == []
    assert path.exists()


def test_corrupt_manifest_recovers_to_empty(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text("{ not json", encoding="utf-8")
    m = Manifest(path)  # must not raise
    assert not m.has_source("anything")
    m.record("vid1", source="s", embed_model="m", embed_dim=2)
    assert m.has_source("vid1")
