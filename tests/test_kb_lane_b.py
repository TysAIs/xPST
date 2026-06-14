"""Knowledge-base hardening tests (Lane B / G30-G34).

Read-only doctor, no-mkdir read paths, semantic kb_query with provenance,
store selection, atomic + corruption-tolerant JSON persistence, and
content-byte ingestion dedup.
"""

import json

import pytest

from xpst.knowledge.models import Nugget
from xpst.knowledge.queue import IngestionQueue
from xpst.knowledge.store.json_store import JsonKnowledgeStore
from xpst.knowledge.workspace import Workspace


def _nugget(point: str, embedding=(), source_url=None) -> Nugget:
    return Nugget.create(
        point=point, source_video_id="src1",
        timestamp_start=1.0, timestamp_end=2.0,
        source_url=source_url,
    ).with_embedding(embedding) if embedding else Nugget.create(
        point=point, source_video_id="src1",
        timestamp_start=1.0, timestamp_end=2.0,
        source_url=source_url,
    )


class TestDoctorReadonly:
    def test_doctor_readonly(self, tmp_path, monkeypatch):
        """G30: diagnose() must never rewrite queue.json."""
        monkeypatch.setenv("XPST_HOME", str(tmp_path))
        ws = Workspace.resolve("default")
        # A stale in_progress item is exactly what _requeue_stale rewrites.
        ws.queue_path.write_text(json.dumps({
            "items": [{
                "id": "item1", "source": "https://example.com/v",
                "status": "in_progress", "workspace": "default",
                "attempts": 1, "enqueued_at": 1.0, "updated_at": 1.0,
            }]
        }), encoding="utf-8")
        before = ws.queue_path.read_text(encoding="utf-8")

        from xpst.knowledge.doctor import diagnose
        diagnose(ws)

        assert ws.queue_path.read_text(encoding="utf-8") == before, (
            "doctor mutated queue.json"
        )

    def test_readonly_queue_does_not_requeue(self, tmp_path):
        path = tmp_path / "queue.json"
        path.write_text(json.dumps({
            "items": [{
                "id": "i1", "source": "s", "status": "in_progress",
                "workspace": "default", "attempts": 1,
                "enqueued_at": 1.0, "updated_at": 1.0,
            }]
        }), encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        IngestionQueue(path, readonly=True)
        assert path.read_text(encoding="utf-8") == before

    def test_workspace_resolve_no_mkdir_on_read(self, tmp_path, monkeypatch):
        """G30: probing a nonexistent workspace must not create it."""
        monkeypatch.setenv("XPST_HOME", str(tmp_path))
        ws = Workspace.resolve("ghost", create=False)
        assert not ws.root.exists()
        # default behavior unchanged for write paths
        ws2 = Workspace.resolve("real")
        assert ws2.root.exists()


class TestSemanticQuery:
    @pytest.fixture
    def populated_ws(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XPST_HOME", str(tmp_path))
        ws = Workspace.resolve("default")
        store = JsonKnowledgeStore(ws.nuggets_path)
        store.add_nugget(_nugget("ffmpeg scaling preserves aspect ratio",
                                 embedding=(1.0, 0.0), source_url="https://yt/v1"))
        store.add_nugget(_nugget("instagram analytics needs business account",
                                 embedding=(0.0, 1.0), source_url="https://yt/v2"))
        return ws

    def test_kb_query_semantic(self, populated_ws, monkeypatch):
        """G31: queries embed and rank by vector similarity, not substring."""
        from xpst.knowledge import query as q

        class FakeEmbedder:
            model_name = "fake"
            dim = 2

            def embed(self, texts):
                return [[1.0, 0.0] for _ in texts]

        monkeypatch.setattr(
            "xpst.knowledge.llm.embeddings.build_embedder",
            lambda config: FakeEmbedder(),
        )
        result = q.query_nuggets("video quality", k=1)
        assert result["mode"] == "semantic"
        assert result["count"] == 1
        # the [1,0] embedding (ffmpeg nugget) must rank first
        assert "ffmpeg" in result["nuggets"][0]["point"]

    def test_kb_query_provenance(self, populated_ws, monkeypatch):
        """G31: every hit carries citation + timestamps + score."""
        from xpst.knowledge import query as q

        class FakeEmbedder:
            model_name = "fake"
            dim = 2

            def embed(self, texts):
                return [[0.0, 1.0] for _ in texts]

        monkeypatch.setattr(
            "xpst.knowledge.llm.embeddings.build_embedder",
            lambda config: FakeEmbedder(),
        )
        result = q.query_nuggets("analytics", k=2)
        top = result["nuggets"][0]
        assert top["citation"].startswith("https://yt/")
        assert top["timestamp_start"] == 1.0 and top["timestamp_end"] == 2.0
        assert top["score"] is not None and 0.0 < top["score"] <= 1.0

    def test_kb_query_substring_fallback(self, populated_ws, monkeypatch):
        """No embedder available → substring fallback, honestly labeled."""
        from xpst.knowledge import query as q

        def boom(config):
            raise RuntimeError("fastembed not installed")

        monkeypatch.setattr("xpst.knowledge.llm.embeddings.build_embedder", boom)
        result = q.query_nuggets("instagram", k=5)
        assert result["mode"] == "substring"
        assert result["count"] == 1
        assert "instagram" in result["nuggets"][0]["point"]

    def test_mcp_kb_query_uses_shared_path(self, populated_ws, monkeypatch):
        from xpst.knowledge.mcp import tools

        def boom(config):
            raise RuntimeError("no embedder")

        monkeypatch.setattr("xpst.knowledge.llm.embeddings.build_embedder", boom)
        result = tools.kb_query("ffmpeg")
        assert result["mode"] == "substring"
        assert result["count"] == 1


class TestStoreDurability:
    def test_kb_store_corruption_tolerant(self, tmp_path):
        """G33: a corrupt nuggets.json is quarantined, not fatal."""
        path = tmp_path / "nuggets.json"
        path.write_text('{"truncated": ', encoding="utf-8")
        store = JsonKnowledgeStore(path)
        assert list(store.all_nuggets()) == []
        assert path.with_suffix(".json.corrupt").exists()
        # store is usable after recovery
        store.add_nugget(_nugget("fresh"))
        assert len(list(store.all_nuggets())) == 1

    def test_atomic_flush_no_tmp_residue(self, tmp_path):
        path = tmp_path / "nuggets.json"
        store = JsonKnowledgeStore(path)
        store.add_nugget(_nugget("a"))
        assert path.exists()
        assert not path.with_suffix(".json.tmp").exists()
        assert json.loads(path.read_text(encoding="utf-8"))

    def test_replace_nugget_updates_embedding(self, tmp_path):
        path = tmp_path / "nuggets.json"
        store = JsonKnowledgeStore(path)
        n = _nugget("point", embedding=(1.0, 0.0))
        store.add_nugget(n)
        store.replace_nugget(n.with_embedding([0.5, 0.5]))
        stored = store.get_nugget(n.id)
        assert stored is not None
        assert stored.embedding == (0.5, 0.5)
        assert len(list(store.all_nuggets())) == 1


class TestStoreSelection:
    def test_json_store_kept_when_json_data_exists(self, tmp_path, monkeypatch):
        """G32: existing JSON data is never silently stranded."""
        monkeypatch.setenv("XPST_HOME", str(tmp_path))
        ws = Workspace.resolve("default")
        JsonKnowledgeStore(ws.nuggets_path).add_nugget(_nugget("data"))

        from xpst.knowledge.store import open_default_store
        store = open_default_store(ws)
        assert isinstance(store, JsonKnowledgeStore)

    def test_json_store_default_for_fresh_workspace_even_when_lancedb_installed(
        self, tmp_path, monkeypatch
    ):
        pytest.importorskip("lancedb")
        monkeypatch.setenv("XPST_HOME", str(tmp_path))
        ws = Workspace.resolve("fresh")

        from xpst.knowledge.store import open_default_store
        store = open_default_store(ws)

        assert isinstance(store, JsonKnowledgeStore)
        assert not ws.lancedb_path.exists()

    def test_query_missing_workspace_does_not_create_lancedb_or_json_paths(
        self, tmp_path, monkeypatch
    ):
        pytest.importorskip("lancedb")
        monkeypatch.setenv("XPST_HOME", str(tmp_path))

        from xpst.knowledge.query import query_nuggets

        result = query_nuggets("nothing", workspace="ghost")

        assert result["count"] == 0
        assert not (tmp_path / "knowledge" / "ghost").exists()


class TestContentDedup:
    def test_kb_content_hash_dedup(self, tmp_path):
        """G33: two source strings resolving to identical bytes ingest once."""
        from xpst.knowledge.ingest.pipeline import ingest
        from xpst.knowledge.manifest import Manifest

        media_a = tmp_path / "a.mp4"
        media_b = tmp_path / "b.mp4"
        media_a.write_bytes(b"identical media bytes" * 100)
        media_b.write_bytes(b"identical media bytes" * 100)

        store = JsonKnowledgeStore(tmp_path / "nuggets.json")
        manifest = Manifest(tmp_path / "manifest.json")

        class FakeTranscriber:
            def transcribe(self, path):
                from xpst.knowledge.ingest.transcribe import Transcript
                return Transcript(text="hello world", segments=[])

        class FakeEmbedder:
            model_name = "fake"
            dim = 2

            def embed(self, texts):
                return [[0.1, 0.2] for _ in texts]

        def fake_extract(transcript, llm):
            return [{"point": "p", "timestamp_start": 0, "timestamp_end": 1}]

        first = ingest(str(media_a), store=store, transcriber=FakeTranscriber(),
                       manifest=manifest, embedder=FakeEmbedder(), llm_client=None,
                       extractor=fake_extract)
        second = ingest(str(media_b), store=store, transcriber=FakeTranscriber(),
                        manifest=manifest, embedder=FakeEmbedder(), llm_client=None,
                        extractor=fake_extract)

        assert not first.skipped
        assert second.skipped
        assert "content match" in (second.reason or "")

    def test_content_fingerprint_ignores_filename(self, tmp_path):
        from xpst.knowledge.ingest.resolve import content_fingerprint

        a = tmp_path / "one.mp4"
        b = tmp_path / "two.mp4"
        a.write_bytes(b"same" * 1000)
        b.write_bytes(b"same" * 1000)
        assert content_fingerprint(a) == content_fingerprint(b)
        c = tmp_path / "three.mp4"
        c.write_bytes(b"diff" * 1000)
        assert content_fingerprint(a) != content_fingerprint(c)
