"""Knowledge store subpackage (KnowledgeStore interface + adapters)."""


def open_default_store(workspace):
    """Open the active store for a workspace.

    JSON remains the default until a LanceDB workspace exists. Migration is
    explicit via ``xpst kb migrate-store``; once that creates ``lancedb/``,
    all CLI/MCP read and write paths use the migrated backend.
    """
    if workspace.lancedb_path.exists():
        try:
            import lancedb  # noqa: F401 - probe for the optional extra
        except ImportError:
            from xpst.knowledge.store.json_store import JsonKnowledgeStore

            return JsonKnowledgeStore(workspace.nuggets_path)
        from xpst.knowledge.store.vector_lancedb import LanceDBStore

        return LanceDBStore(workspace.lancedb_path)

    from xpst.knowledge.store.json_store import JsonKnowledgeStore

    return JsonKnowledgeStore(workspace.nuggets_path)
