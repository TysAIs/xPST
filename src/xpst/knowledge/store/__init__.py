"""Knowledge store subpackage (KnowledgeStore interface + adapters)."""


def open_default_store(workspace):
    """Open the right store for a workspace (G32).

    LanceDB is the default when the ``knowledge`` extra is installed AND
    either a LanceDB table already exists or no JSON data exists yet — an
    existing JSON store is never silently stranded (data-coupled deps break
    on UPDATE, so migration is explicit via ``xpst kb migrate-store``).
    """
    json_has_data = workspace.nuggets_path.exists()
    try:
        import lancedb  # noqa: F401 — probe for the optional extra
    except ImportError:
        from xpst.knowledge.store.json_store import JsonKnowledgeStore
        return JsonKnowledgeStore(workspace.nuggets_path)
    if workspace.lancedb_path.exists() or not json_has_data:
        from xpst.knowledge.store.vector_lancedb import LanceDBStore
        return LanceDBStore(workspace.lancedb_path)
    from xpst.knowledge.store.json_store import JsonKnowledgeStore
    return JsonKnowledgeStore(workspace.nuggets_path)
