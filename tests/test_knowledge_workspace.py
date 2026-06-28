from xpst.knowledge.workspace import Workspace


def test_workspace_creates_isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("XPST_HOME", str(tmp_path))
    ws = Workspace.resolve("default")
    assert ws.root.exists()
    assert ws.nuggets_path.parent == ws.root
    assert ws.name == "default"


def test_workspaces_are_isolated_by_name(tmp_path, monkeypatch):
    monkeypatch.setenv("XPST_HOME", str(tmp_path))
    a = Workspace.resolve("alice")
    b = Workspace.resolve("bob")
    assert a.root != b.root


def test_workspace_lancedb_path(tmp_path, monkeypatch):
    monkeypatch.setenv("XPST_HOME", str(tmp_path))
    ws = Workspace.resolve("default")
    assert ws.lancedb_path == ws.root / "lancedb"
    # isolated per workspace
    other = Workspace.resolve("other")
    assert ws.lancedb_path != other.lancedb_path
