"""Knowledge store subpackage (KnowledgeStore interface + adapters).

Phase D3: sqlite-vec is now the default vector backend (<1MB vs LanceDB's
~50MB). LanceDB remains available as a legacy backend when explicitly
configured via ``vector_backend: "lancedb"``.
"""


def open_default_store(workspace):
    """Open the right store for a workspace.

    Resolution order (D3):
    1. If ``vector_backend`` config says ``"lancedb"``, use LanceDBStore
    2. If existing JSON nuggets are present, keep JsonKnowledgeStore —
       existing data is never silently stranded; migration is explicit
       via ``xpst kb migrate-store``.
    3. If sqlite-vec is installed AND its vec0 extension actually loads,
       use SQLiteVecStore (default, new)
    4. If lancedb is available, use LanceDBStore (legacy)
    5. Fall back to JsonKnowledgeStore (no vectors, substring search only)
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

    # Existing JSON data is never silently stranded.
    if json_has_data:
        from xpst.knowledge.store.json_store import JsonKnowledgeStore
        return JsonKnowledgeStore(workspace.nuggets_path)

    # Default: sqlite-vec — package present is not enough; the C extension
    # must actually load (some wheels import fine but fail at vec0 init).
    if backend == "sqlite-vec":
        try:
            from xpst.knowledge.store.vector_sqlite import (
                SQLiteVecStore,
                vec0_available,
            )

            if not vec0_available():
                raise RuntimeError("sqlite-vec extension unavailable (vec0)")
            return SQLiteVecStore(workspace.root / "vectors.db")
        except (ImportError, RuntimeError):
            pass  # fall through to lancedb or json

    # Legacy: lancedb if available
    try:
        import lancedb  # noqa: F401

        from xpst.knowledge.store.vector_lancedb import LanceDBStore
        return LanceDBStore(workspace.lancedb_path)
    except ImportError:
        pass

    # Fallback: JSON store (no vector search)
    from xpst.knowledge.store.json_store import JsonKnowledgeStore
    return JsonKnowledgeStore(workspace.nuggets_path)
