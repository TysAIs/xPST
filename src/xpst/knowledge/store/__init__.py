"""Knowledge store subpackage (KnowledgeStore interface + adapters).

Phase D3: sqlite-vec is now the default vector backend (<1MB vs LanceDB's
~50MB). LanceDB remains available as a legacy backend when explicitly
configured via ``vector_backend: "lancedb"``.
"""


def open_default_store(workspace):
    """Open the right store for a workspace.

    Resolution order (D3):
    1. If ``vector_backend`` config says ``"lancedb"``, use LanceDBStore
    2. If sqlite-vec is available, use SQLiteVecStore (default, new)
    3. If sqlite-vec not installed but lancedb is, use LanceDBStore (legacy)
    4. Fall back to JsonKnowledgeStore (no vectors, substring search only)

    An existing JSON store is never silently stranded — migration is
    explicit via ``xpst kb migrate-store``.
    """
    import os

    json_has_data = workspace.nuggets_path.exists()
    backend = os.environ.get("XPST_KB_VECTOR_BACKEND", "sqlite-vec")

    # Explicit LanceDB request
    if backend == "lancedb":
        try:
            import lancedb  # noqa: F401

            from xpst.knowledge.store.vector_lancedb import LanceDBStore
            return LanceDBStore(workspace.lancedb_path)
        except ImportError:
            pass

    # Default: sqlite-vec
    if backend == "sqlite-vec":
        try:
            import sqlite_vec  # noqa: F401

            from xpst.knowledge.store.vector_sqlite import SQLiteVecStore
            vec_path = workspace.root / "vectors.db"
            return SQLiteVecStore(vec_path)
        except ImportError:
            pass  # fall through to lancedb or json

    # Legacy: lancedb if available and data exists
    try:
        import lancedb  # noqa: F401
        if workspace.lancedb_path.exists() or not json_has_data:
            from xpst.knowledge.store.vector_lancedb import LanceDBStore
            return LanceDBStore(workspace.lancedb_path)
    except ImportError:
        pass

    # Fallback: JSON store (no vector search)
    from xpst.knowledge.store.json_store import JsonKnowledgeStore
    return JsonKnowledgeStore(workspace.nuggets_path)
