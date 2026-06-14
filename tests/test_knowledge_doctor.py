"""kb doctor: read-only health diagnostics over a workspace.

Covers the happy path (clean store, deps probed) and degraded paths (mixed
embedding widths, orphaned nuggets, dangling area membership, failed queue
items), plus the CLI surface.
"""
from __future__ import annotations

from click.testing import CliRunner

from xpst.cli import main
from xpst.knowledge.doctor import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    diagnose,
)
from xpst.knowledge.models import Area, Nugget
from xpst.knowledge.queue import IngestionQueue
from xpst.knowledge.store.json_store import JsonKnowledgeStore
from xpst.knowledge.workspace import Workspace


def _nugget(point: str, *, embedding=(0.1, 0.2), area_id=None) -> Nugget:
    n = Nugget.create(
        point=point,
        source_video_id="vid",
        timestamp_start=0.0,
        timestamp_end=1.0,
        source_url="https://x/v",
    )
    if embedding is not None:
        n = n.with_embedding(embedding)
    if area_id is not None:
        n = n.with_area(area_id)
    return n


def _ws(tmp_path, monkeypatch) -> Workspace:
    monkeypatch.setenv("XPST_HOME", str(tmp_path))
    return Workspace.resolve("default")


def _findings_by_severity(report, severity):
    return [f for f in report.findings if f.severity == severity]


def test_empty_workspace_is_ok_with_warnings(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    report = diagnose(ws)
    # An empty, never-used workspace is not an ERROR — it just has warnings
    # (store empty, maybe deps absent).
    assert report.ok
    assert report.error_count == 0
    checks = {f.check for f in report.findings}
    assert {"store", "queue", "manifest", "embeddings", "orphans",
            "dependencies"} & checks


def test_healthy_organized_workspace_ok(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    store = JsonKnowledgeStore(ws.nuggets_path)
    n = _nugget("a point", embedding=(0.1, 0.2))
    store.add_nugget(n)
    store.upsert_area(Area.create(label="A", nugget_ids=[n.id], order_index=0))

    report = diagnose(ws)
    assert report.ok
    assert report.error_count == 0


def test_diagnose_uses_active_store_backend(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    nugget = _nugget("active backend point", embedding=(0.1, 0.2))
    area = Area.create(label="A", nugget_ids=[nugget.id], order_index=0)
    captured = {}

    class FakeStore:
        def all_nuggets(self):
            return [nugget]

        def areas(self):
            return [area]

    def fake_open_default_store(workspace):
        captured["workspace"] = workspace
        return FakeStore()

    monkeypatch.setattr("xpst.knowledge.doctor.open_default_store", fake_open_default_store)

    report = diagnose(ws)

    assert captured["workspace"] is ws
    assert report.ok


def test_mixed_embedding_widths_is_error(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    store = JsonKnowledgeStore(ws.nuggets_path)
    store.add_nugget(_nugget("two dims", embedding=(0.1, 0.2)))
    store.add_nugget(_nugget("three dims", embedding=(0.1, 0.2, 0.3)))

    report = diagnose(ws)
    assert not report.ok
    errors = _findings_by_severity(report, SEVERITY_ERROR)
    assert any(f.check == "embeddings" for f in errors)


def test_orphaned_nugget_pointing_at_missing_area_is_error(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    store = JsonKnowledgeStore(ws.nuggets_path)
    store.add_nugget(_nugget("orphan", area_id="ghost-area"))

    report = diagnose(ws)
    assert not report.ok
    errors = _findings_by_severity(report, SEVERITY_ERROR)
    assert any(f.check == "orphaned_nuggets" for f in errors)


def test_dangling_area_membership_is_error(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    store = JsonKnowledgeStore(ws.nuggets_path)
    # Area references a nugget id that was never stored.
    store.upsert_area(Area.create(label="A", nugget_ids=["nonexistent"], order_index=0))

    report = diagnose(ws)
    assert not report.ok
    errors = _findings_by_severity(report, SEVERITY_ERROR)
    assert any(f.check == "orphaned_areas" for f in errors)


def test_failed_queue_item_is_warning_not_error(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    q = IngestionQueue(ws.queue_path)
    item = q.enqueue("https://x/bad")
    q.dequeue()
    q.mark_failed(item.id, "transcription error")

    report = diagnose(ws)
    # A failed item is operational signal, not store corruption -> warning.
    assert report.ok
    warnings = _findings_by_severity(report, SEVERITY_WARNING)
    assert any(f.check == "queue" and "failed" in f.message for f in warnings)


def test_diagnose_does_not_mutate_state(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    store = JsonKnowledgeStore(ws.nuggets_path)
    n = _nugget("a point")
    store.add_nugget(n)
    nuggets_before = ws.nuggets_path.read_text(encoding="utf-8")

    diagnose(ws)

    assert ws.nuggets_path.read_text(encoding="utf-8") == nuggets_before


# ── CLI surface ──

def test_cli_doctor_happy_path_exit_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("XPST_HOME", str(tmp_path))
    ws = Workspace.resolve("default")
    store = JsonKnowledgeStore(ws.nuggets_path)
    n = _nugget("ok point", embedding=(0.1, 0.2))
    store.add_nugget(n)
    store.upsert_area(Area.create(label="A", nugget_ids=[n.id], order_index=0))

    out = CliRunner().invoke(main, ["kb", "doctor"])
    assert out.exit_code == 0, out.output
    assert "OK" in out.output


def test_cli_doctor_degraded_exit_one(tmp_path, monkeypatch):
    monkeypatch.setenv("XPST_HOME", str(tmp_path))
    ws = Workspace.resolve("default")
    store = JsonKnowledgeStore(ws.nuggets_path)
    store.add_nugget(_nugget("two", embedding=(0.1, 0.2)))
    store.add_nugget(_nugget("three", embedding=(0.1, 0.2, 0.3)))

    out = CliRunner().invoke(main, ["kb", "doctor"])
    assert out.exit_code == 1, out.output
    assert "PROBLEMS FOUND" in out.output
